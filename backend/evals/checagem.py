"""Eval dos vereditos da checagem: mede falso "Não verificável" e falso "Falso".

Uso (em backend/):
  python -m evals.checagem --replay evals/gravacoes/<arquivo>.json
      Reavalia respostas JÁ gravadas com a pós-validação atual. Não chama nada.
  CHECAFATOS_EVAL_REAL=1 python -m evals.checagem --real --casos id1,id2,id3
      Chama o Gemini de verdade. Regra do projeto: só com autorização do usuário, no máximo
      3 chamadas por rodada (travado aqui: 3 casos, 1 modelo, sem novas tentativas, e uma
      reserva por chamada). Grava as respostas em evals/gravacoes/ para replay depois.

Casos em evals/casos_checagem.json. Não vai para a imagem (o Dockerfile copia só app/).
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import get_args

from app.services.factcheck import AgencyCheck
from app.services.gemini import (
    BOA_VISTA,
    AnalysisInput,
    GeminiAnalyzer,
    Generated,
    InputKind,
    Veredito,
    finalize,
    is_false_future_claim,
    parse_verdict,
)
from app.services.url_guard import BlockedAddressError, InvalidUrlError, normalize_url

MAX_REAL_CALLS = 3
REAL_RUN_FLAG = "CHECAFATOS_EVAL_REAL"
CATEGORIES = frozenset({"noticia_real", "boato_checado", "whatsapp"})
VEREDITOS = frozenset(get_args(Veredito))
HERE = Path(__file__).parent
# Dúvida sobre QUEM publicou, num link que o servidor baixou daquele domínio (regra 10).
# Alarme heurístico: dúvida sobre FATO ("não é possível confirmar se o TSE...") e "suposto"
# marcando alegação ("suposto autor do crime") não contam.
_OUTLET = (
    r"(?:uma?\s+)?(?:mat[ée]ria|reportagem|not[íi]cia|p[áa]gina|publica[çc][ãa]o|texto|"
    r"conte[úu]do|portal|ve[íi]culo|site)"
)
_AUTHORSHIP_DOUBT = re.compile(
    r"\bformatad[ao]s?\s+como\b"
    rf"|\bsupost[ao]s?\s+{_OUTLET}\b"
    rf"|\bse\s+(?:apresentam?|passa)\s+(?:como|por)\s+{_OUTLET}\b"
    rf"|\bapresentando-se\s+como\s+{_OUTLET}\b"
    rf"|\bautenticidade\s+d[ao]\s+{_OUTLET}"
    r"|\bpode\s+ser\s+(?:uma\s+)?montagem\b"
    rf"|\b(?:confirmar|garantir)\s+(?:que\s+)?(?:a|o)\s+{_OUTLET}\s+(?:seja|[ée]|foi)\b"
    r"|\b(?:confirmar|garantir)\s+a\s+(?:autoria|origem)\b",
    re.IGNORECASE,
)
_SUPOSTO = re.compile(r"\bsupost[ao]s?\b", re.IGNORECASE)
_FUTURE_WORDS = re.compile(r"futur|ainda n[ãa]o (?:ocorr|acontec)", re.IGNORECASE)


class RealRunRefusedError(RuntimeError):
    """Chamada real ao Gemini sem autorização explícita ou acima do teto."""


@dataclass(frozen=True)
class Case:
    id: str
    categoria: str
    origem: str  # de onde veio o caso ("sintético" ou a fonte real)
    kind: InputKind
    url: str | None
    titulo: str | None
    conteudo: str
    publicado_em: date | None
    checagens: list[AgencyCheck]
    aceitos: list[str]  # vereditos aceitos como acerto
    # Link do próprio veículo (False = domínio que imita: a IA DEVE desconfiar da autoria).
    autoria_confirmada: bool = True

    def to_input(self) -> AnalysisInput:
        return AnalysisInput(
            kind=self.kind,
            content=self.conteudo,
            title=self.titulo,
            url=self.url,
            agency_checks=self.checagens,
            content_urls=[],
            published_at=self.publicado_em,
            # Link do eval = página obtida daquele domínio (como no serviço, depois do fetch).
            source_domain=normalize_url(self.url).domain
            if self.kind == "link" and self.url
            else None,
        )


@dataclass(frozen=True)
class CaseResult:
    case: Case
    veredito: str
    veredito_ia: str
    pontos: list[str]  # depois da pós-validação (o que o usuário vê)
    pontos_ia: list[str] = field(default_factory=list)  # resposta crua do modelo
    resumo: str = ""
    contexto: str = ""  # contexto_faltante (o usuário também vê)


@dataclass(frozen=True)
class Report:
    total: int
    acertos: int
    # Esperado era um veredito firme, mas veio "Não verificável" (o bug de 09/10/2026).
    falso_nao_verificavel: list[str] = field(default_factory=list)
    # "Falso" onde não deveria (o erro mais grave para notícia real).
    falso_falso: list[str] = field(default_factory=list)
    # A IA ainda cometeu o erro de data (resposta crua): mede o PROMPT, não o filtro.
    erro_de_data_ia: list[str] = field(default_factory=list)
    # Pontos com "futuro"/"ainda não ocorreu" que o filtro MANTEVE: revisão humana.
    revisar_futuro: list[str] = field(default_factory=list)
    # Link com origem confirmada em que a IA questionou a AUTORIA ("formatada como", "suposta").
    autoria_questionada: list[str] = field(default_factory=list)
    # Link confirmado com "suposto/suposta": revisão humana (pode ser alegação legítima).
    revisar_suposto: list[str] = field(default_factory=list)


def load_cases(path: Path) -> list[Case]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases: list[Case] = []
    for item in raw["casos"]:
        accepted = list(item["aceitos"])
        if not accepted or not set(accepted) <= VEREDITOS:
            raise ValueError(f"caso {item['id']}: aceitos inválidos {accepted}")
        if item["categoria"] not in CATEGORIES:
            raise ValueError(f"caso {item['id']}: categoria inválida {item['categoria']}")
        if item["kind"] not in get_args(InputKind):
            raise ValueError(f"caso {item['id']}: kind inválido {item['kind']}")
        published = item.get("publicado_em")
        if item.get("url"):
            try:
                normalize_url(item["url"])
            except (InvalidUrlError, BlockedAddressError) as exc:
                raise ValueError(f"caso {item['id']}: url inválida ({exc})") from exc
        cases.append(
            Case(
                id=item["id"],
                categoria=item["categoria"],
                origem=item["origem"],
                kind=item["kind"],
                url=item.get("url"),
                titulo=item.get("titulo"),
                conteudo=item["conteudo"],
                publicado_em=date.fromisoformat(published) if published else None,
                checagens=[AgencyCheck(**c) for c in item.get("checagens", [])],
                aceitos=accepted,
                autoria_confirmada=bool(item.get("autoria_confirmada", True)),
            )
        )
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("ids de caso repetidos")
    return cases


def _matches(pattern: re.Pattern[str], r: CaseResult) -> bool:
    texts = [r.resumo, r.contexto, *r.pontos, *r.pontos_ia]
    return any(pattern.search(t) for t in texts if t)


def grade(results: list[CaseResult], today: date | None = None) -> Report:
    today = today or datetime.now(BOA_VISTA).date()
    false_unverifiable = [
        r.case.id
        for r in results
        if r.veredito == "nao_verificavel" and "nao_verificavel" not in r.case.aceitos
    ]
    false_false = [
        r.case.id for r in results if r.veredito == "falso" and "falso" not in r.case.aceitos
    ]
    date_mistake = [
        r.case.id
        for r in results
        if any(is_false_future_claim(p, r.case.to_input(), today) for p in r.pontos_ia)
    ]
    # Detector largo e independente do filtro: o que sobrou com cara de "futuro" é revisado
    # por uma pessoa (pode ser aviso legítimo ou um erro que o filtro deixou passar).
    review = [r.case.id for r in results if any(_FUTURE_WORDS.search(p) for p in r.pontos)]
    # Links do próprio veículo: ali a IA não deve duvidar de quem publicou (regra 10).
    confirmed = [r for r in results if r.case.kind == "link" and r.case.autoria_confirmada]
    return Report(
        total=len(results),
        acertos=sum(r.veredito in r.case.aceitos for r in results),
        falso_nao_verificavel=false_unverifiable,
        falso_falso=false_false,
        erro_de_data_ia=date_mistake,
        revisar_futuro=review,
        autoria_questionada=[r.case.id for r in confirmed if _matches(_AUTHORSHIP_DOUBT, r)],
        revisar_suposto=[r.case.id for r in confirmed if _matches(_SUPOSTO, r)],
    )


async def replay(cases: list[Case], recorded: dict[str, str], *, today: date) -> list[CaseResult]:
    """Reaplica a pós-validação ATUAL a respostas gravadas. Nenhuma chamada externa."""
    results: list[CaseResult] = []
    for c in cases:
        if c.id not in recorded:
            continue
        verdict = parse_verdict(recorded[c.id])
        if verdict is None:
            results.append(CaseResult(c, "nao_verificavel", "fora_do_schema", []))
            continue
        checked = finalize(verdict, c.to_input(), today)
        results.append(
            CaseResult(
                c,
                checked.veredito,
                checked.veredito_ia,
                checked.pontos_suspeitos,
                list(verdict.pontos_suspeitos),
                checked.resumo,
                checked.contexto_faltante or "",
            )
        )
    return results


UNKNOWN_MODEL = "(modelo não gravado)"


async def replay_by_model(
    cases: list[Case], recording: dict[str, object], *, today: date
) -> dict[str, list[CaseResult]]:
    """Replay de cada modelo do pool em separado (gravações com "respostas_por_modelo",
    12/10/2026). Gravações antigas (só "respostas") viram um grupo sem modelo."""
    by_model = recording.get("respostas_por_modelo")
    if not isinstance(by_model, dict):
        answers = recording.get("respostas")
        by_model = {UNKNOWN_MODEL: answers if isinstance(answers, dict) else {}}
    return {
        model: await replay(cases, answers, today=today)
        for model, answers in by_model.items()
        if isinstance(answers, dict)
    }


def require_real_run(case_ids: list[str]) -> None:
    if os.environ.get(REAL_RUN_FLAG) != "1":
        raise RealRunRefusedError(
            f"Chamada real ao Gemini exige autorização do usuário e {REAL_RUN_FLAG}=1."
        )
    if not case_ids or len(case_ids) > MAX_REAL_CALLS:
        raise RealRunRefusedError(f"Escolha de 1 a {MAX_REAL_CALLS} casos (regra do projeto).")
    if len(set(case_ids)) != len(case_ids):
        raise RealRunRefusedError("Ids de caso repetidos.")


def recording_day(recording: dict[str, object], fallback: date) -> date:
    """O "hoje" da gravação: o replay fica reproduzível em qualquer dia."""
    day = recording.get("data")
    return date.fromisoformat(day) if isinstance(day, str) else fallback


def budget_reserve(limit: int) -> tuple[Callable[[], Awaitable[bool]], Callable[[], int]]:
    """`reserve` do analisador que libera no máximo `limit` chamadas ao modelo."""
    used = 0

    async def reserve() -> bool:
        nonlocal used
        if used >= limit:
            return False
        used += 1
        return True

    return reserve, lambda: used


async def run_real(cases: list[Case]) -> tuple[list[CaseResult], dict[str, str], int]:
    # A trava fica aqui (não só na CLI): quem chamar run_real direto também passa por ela.
    require_real_run([c.id for c in cases])
    from app.config import Settings
    from app.services.gemini import google_generate

    settings = Settings()  # nunca imprimir: carrega a chave
    if not settings.gemini_api_key:
        raise RealRunRefusedError("GEMINI_API_KEY ausente.")
    chain = google_generate(
        settings.gemini_api_key,
        [settings.gemini_models[0]],  # um modelo só: o fallback faria outra chamada
        thinking_budget=settings.gemini_thinking_budget,
        thinking_level=settings.gemini_thinking_level,
        attempt_timeout=settings.gemini_attempt_timeout_seconds,
        max_retries=0,  # cada reserva = exatamente uma chamada
        budget_seconds=settings.gemini_budget_seconds,
    )
    last_answer = {"text": ""}

    async def recording(prompt: str) -> Generated:
        answer = await chain(prompt)
        last_answer["text"] = answer.text or ""
        return answer

    analyzer = GeminiAnalyzer(recording)
    reserve, used = budget_reserve(MAX_REAL_CALLS)
    results: list[CaseResult] = []
    answers: dict[str, str] = {}
    for c in cases:
        last_answer["text"] = ""
        try:
            checked = await analyzer.analyze(c.to_input(), reserve)
        except Exception as exc:  # registra e segue: o relatório mostra só os que responderam
            print(f"{c.id}: erro {type(exc).__name__}", file=sys.stderr)
            continue
        answers[c.id] = last_answer["text"]
        raw = parse_verdict(last_answer["text"])
        results.append(
            CaseResult(
                c,
                checked.veredito,
                checked.veredito_ia,
                checked.pontos_suspeitos,
                list(raw.pontos_suspeitos) if raw else [],
                checked.resumo,
                checked.contexto_faltante or "",
            )
        )
    return results, answers, used()


def _print_report(results: list[CaseResult], report: Report) -> None:
    for r in results:
        ok = "ok  " if r.veredito in r.case.aceitos else "ERRO"
        print(
            f"{ok} {r.case.id:<28} {r.veredito:<16} (IA: {r.veredito_ia}) aceitos={r.case.aceitos}"
        )
    print()
    print(f"acertos: {report.acertos}/{report.total}")
    print(f"falso 'Não verificável': {report.falso_nao_verificavel}")
    print(f"falso 'Falso': {report.falso_falso}")
    print(f"IA ainda errou a data (antes do filtro): {report.erro_de_data_ia}")
    print(f"revisar à mão (sobrou 'futuro'): {report.revisar_futuro}")
    print(f"link com autoria questionada: {report.autoria_questionada}")
    print(f"revisar à mão ('suposto' em link confirmado): {report.revisar_suposto}")


def _print_model_table(by_model: dict[str, list[CaseResult]], today: date) -> None:
    """Qualidade por modelo do pool, lado a lado."""
    print(f"{'modelo':<26} {'casos':>5} {'acertos':>7} {'falso NV':>8} {'falso F':>7}")
    for model, model_results in by_model.items():
        report = grade(model_results, today)
        print(
            f"{model:<26} {report.total:>5} {report.acertos:>7} "
            f"{len(report.falso_nao_verificavel):>8} {len(report.falso_falso):>7}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Eval dos vereditos da checagem.")
    parser.add_argument("--casos-arquivo", type=Path, default=HERE / "casos_checagem.json")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", type=Path, help="gravação para reavaliar sem chamar nada")
    mode.add_argument("--real", action="store_true", help="chama o Gemini (máx. 3 casos)")
    parser.add_argument("--casos", default="", help="ids separados por vírgula (modo --real)")
    args = parser.parse_args()

    cases = load_cases(args.casos_arquivo)
    today = datetime.now(BOA_VISTA).date()
    if args.replay:
        recording = json.loads(args.replay.read_text(encoding="utf-8"))
        today = recording_day(recording, today)
        by_model = asyncio.run(replay_by_model(cases, recording, today=today))
        for model, model_results in by_model.items():
            print(f"=== {model} ===")
            _print_report(model_results, grade(model_results, today))
            print()
        _print_model_table(by_model, today)
        return
    else:
        ids = [i.strip() for i in args.casos.split(",") if i.strip()]
        require_real_run(ids)
        chosen = [c for c in cases if c.id in ids]
        if len(chosen) != len(ids):
            raise SystemExit(f"casos desconhecidos: {set(ids) - {c.id for c in chosen}}")
        results, answers, calls = asyncio.run(run_real(chosen))
        out = HERE / "gravacoes" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(
            json.dumps({"data": str(today), "respostas": answers}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"chamadas reais ao Gemini: {calls} (máx. {MAX_REAL_CALLS}); gravado em {out}")
    _print_report(results, grade(results, today))


if __name__ == "__main__":
    main()
