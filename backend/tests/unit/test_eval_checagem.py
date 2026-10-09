"""Eval de vereditos (backend/evals/checagem.py): só a parte determinística, sem rede."""

import json
from datetime import date
from pathlib import Path

import pytest

from evals.checagem import (
    MAX_REAL_CALLS,
    Case,
    CaseResult,
    RealRunRefusedError,
    budget_reserve,
    grade,
    load_cases,
    recording_day,
    replay,
    require_real_run,
    run_real,
)

pytestmark = pytest.mark.anyio

CASES_FILE = Path(__file__).parents[2] / "evals" / "casos_checagem.json"


def case(case_id: str, accepted: list[str], categoria: str = "noticia_real") -> Case:
    return Case(
        id=case_id,
        categoria=categoria,
        origem="sintético",
        kind="texto",
        url=None,
        titulo=None,
        conteudo="Texto de teste com conteúdo suficiente para a checagem.",
        publicado_em=None,
        checagens=[],
        aceitos=accepted,
    )


def result(
    c: Case, veredito: str, pontos: list[str] | None = None, pontos_ia: list[str] | None = None
) -> CaseResult:
    return CaseResult(
        case=c,
        veredito=veredito,
        veredito_ia=veredito,
        pontos=pontos or [],
        pontos_ia=pontos_ia if pontos_ia is not None else (pontos or []),
    )


# ---------- casos ----------


def test_case_file_is_valid_and_covers_the_three_categories() -> None:
    cases = load_cases(CASES_FILE)
    assert {c.categoria for c in cases} == {"noticia_real", "boato_checado", "whatsapp"}
    assert len({c.id for c in cases}) == len(cases)


def test_invalid_expected_verdict_is_rejected(tmp_path: Path) -> None:
    raw = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    raw["casos"][0]["aceitos"] = ["talvez"]
    bad = tmp_path / "casos.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(bad)


# ---------- métricas ----------


def test_grade_counts_false_unverifiable_and_false_false() -> None:
    news = case("noticia", ["verdadeiro"])
    rumor = case("boato", ["falso"], "boato_checado")
    chain = case("zap", ["falso", "enganoso", "nao_verificavel"], "whatsapp")
    report = grade(
        [
            result(news, "nao_verificavel"),  # falso "Não verificável"
            result(rumor, "falso"),  # acerto
            result(chain, "verdadeiro"),  # erro, mas não é nenhum dos dois falsos medidos
        ]
    )
    assert report.total == 3
    assert report.acertos == 1
    assert report.falso_nao_verificavel == ["noticia"]
    assert report.falso_falso == []


def test_grade_flags_false_false_on_real_news() -> None:
    news = case("noticia", ["verdadeiro"])
    report = grade([result(news, "falso")])
    assert report.falso_falso == ["noticia"]


def test_grade_measures_the_date_mistake_in_the_raw_answer() -> None:
    # Mede o PROMPT: quantas vezes a IA ainda comete o erro (antes do filtro).
    news = case("noticia", ["verdadeiro"])
    raw = ["Fatos futuros: outubro de 2026"]
    report = grade([result(news, "verdadeiro", [], raw)], date(2026, 10, 9))
    assert report.erro_de_data_ia == ["noticia"]


def test_grade_lists_surviving_future_words_for_human_review() -> None:
    # Pontos com "futuro"/"ainda não ocorreu" que o filtro manteve: revisão humana.
    news = case("noticia", ["verdadeiro"])
    point = "Dá como certa a posse de 01/01/2027, que ainda não ocorreu"
    report = grade([result(news, "verdadeiro", [point])], date(2026, 10, 9))
    assert report.erro_de_data_ia == []
    assert report.revisar_futuro == ["noticia"]


# ---------- replay (sem chamar o Gemini) ----------


async def test_replay_regrades_recorded_answers_with_current_post_validation() -> None:
    news = Case(
        id="noticia-hoje",
        categoria="noticia_real",
        origem="sintético",
        kind="link",
        url="https://g1.globo.com/rr/noticia/2026/10/09/exemplo.ghtml",
        titulo="Exemplo",
        conteudo="Texto da notícia de hoje.",
        publicado_em=date(2026, 10, 9),
        checagens=[],
        aceitos=["verdadeiro"],
    )
    recorded = {
        "noticia-hoje": json.dumps(
            {
                "veredito": "verdadeiro",
                "resumo": "Notícia de veículo conhecido.",
                "pontos_suspeitos": ["Acontecimentos de outubro de 2026, projetando fatos futuros"],
                "contexto_faltante": None,
                "checagens_agencias": [],
                "fontes_sugeridas": [],
                "confianca": 85,
            }
        )
    }
    results = await replay([news], recorded, today=date(2026, 10, 9))
    assert results[0].pontos_ia == [  # a resposta crua fica para medir o prompt
        "Acontecimentos de outubro de 2026, projetando fatos futuros"
    ]
    assert results[0].veredito == "verdadeiro"
    assert results[0].pontos == []  # a pós-validação de hoje descarta o "futuro"


# ---------- trava das chamadas reais ----------


def test_real_run_needs_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHECAFATOS_EVAL_REAL", raising=False)
    with pytest.raises(RealRunRefusedError):
        require_real_run(["a"])


def test_real_run_is_capped_at_three_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECAFATOS_EVAL_REAL", "1")
    assert MAX_REAL_CALLS == 3
    require_real_run(["a", "b", "c"])
    with pytest.raises(RealRunRefusedError):
        require_real_run(["a", "b", "c", "d"])


async def test_run_real_itself_refuses_without_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # A trava não depende de quem chama a CLI: run_real direto também recusa.
    monkeypatch.delenv("CHECAFATOS_EVAL_REAL", raising=False)
    with pytest.raises(RealRunRefusedError):
        await run_real([case("a", ["verdadeiro"])])


async def test_run_real_refuses_more_than_three_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECAFATOS_EVAL_REAL", "1")
    cases = [case(f"c{i}", ["verdadeiro"]) for i in range(4)]
    with pytest.raises(RealRunRefusedError):
        await run_real(cases)


def test_repeated_ids_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECAFATOS_EVAL_REAL", "1")
    with pytest.raises(RealRunRefusedError, match="repetid"):
        require_real_run(["a", "a"])


def test_replay_uses_the_recording_day() -> None:
    # Reproduzível: reavaliar depois de 25/10 não muda o que era "futuro" no dia da gravação.
    assert recording_day({"data": "2026-10-09"}, date(2026, 11, 1)) == date(2026, 10, 9)
    assert recording_day({}, date(2026, 11, 1)) == date(2026, 11, 1)


async def test_budget_reserve_allows_at_most_three_model_calls() -> None:
    reserve, used = budget_reserve(MAX_REAL_CALLS)
    allowed = [await reserve() for _ in range(5)]
    assert allowed == [True, True, True, False, False]
    assert used() == 3
