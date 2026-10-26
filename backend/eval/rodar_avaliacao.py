"""Avaliação da plataforma com gabarito (casos.yaml), de ponta a ponta, sem banco.

Roda a checagem de verdade (run_check: leitura do link, Fact Check, Gemini, salvaguardas) para
cada caso ROTULADO e compara com o gabarito. Baseado no e2e_37.py da validação do #42.

- Repositório em memória novo por caso: nada vai para o Supabase e nenhum caso sai do cache.
- Orçamento OBRIGATÓRIO de chamadas ao Gemini (--orcamento). Cada requisição ao Google conta
  (logs gemini_usage/gemini_api_error). Um caso só começa se couber o pior caso dele: uma
  tentativa por modelo da cadeia (--modelos), e a cadeia pode ser percorrida de novo quando a
  resposta foge do schema (gemini.MAX_ATTEMPTS).
- Para em 429 e em 2 falhas seguidas de timeout/503/504 (o caso fica "não avaliado").
- Consultas à Fact Check também são contadas (teto opcional: --orcamento-factcheck).
- Casos com rotulado_por PENDENTE são ignorados.
- Relatório em eval/resultados/AAAA-MM-DD_<commit>.md e .json (gravado também se a execução
  for interrompida no meio: o que já foi pago em chamadas não se perde).

Chamada real só com autorização do usuário: exige CHECAFATOS_EVAL_REAL=1 e o orçamento. As
chaves vêm da configuração do app (CHECAFATOS_DOTENV=1), nunca impressas. ATENÇÃO: com a chave
da produção, as chamadas gastam a cota diária do Google sem o contador do banco da produção
saber (o repositório aqui é em memória). Uso:
    CHECAFATOS_EVAL_REAL=1 CHECAFATOS_DOTENV=1 python -m eval.rodar_avaliacao --orcamento N
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import yaml

from app.errors import AppError
from app.routers.checks import to_out
from app.schemas.checks import Veredito
from app.services.check_service import CheckSettings, CheckTools, run_check
from app.services.factcheck import AgencyCheck, same_page
from app.services.gemini import MAX_ATTEMPTS
from app.services.relevance import content_keys
from app.services.verdict_policy import relevant_verdict, split_relevant
from tests.fakes import FakeCheckRepository  # repositório em memória (o mesmo dos testes)

HERE = Path(__file__).parent
CASOS_PADRAO = HERE / "casos.yaml"
RESULTADOS = HERE / "resultados"
REAL_RUN_FLAG = "CHECAFATOS_EVAL_REAL"
PENDENTE = "PENDENTE"
TIPOS = (
    "boato_checado",
    "fato_verdadeiro",
    "link_agencia",
    "noticia_legitima",
    "boato_sem_checagem",
)
AGENCY_PAGE = "checagem_de_agencia"
ESPERADOS = frozenset(get_args(Veredito)) | {AGENCY_PAGE}
# Vereditos que dizem "o conteúdo é desinformação": "verdadeiro" para eles é linha vermelha.
BOATO = frozenset({"falso", "enganoso", "fora_de_contexto"})
CATEGORICOS = BOATO | {"verdadeiro"}  # tudo menos "não verificável"
# Falhas passageiras: sem resposta no teto da tentativa, 503 "high demand" e 504 do Google.
# Status vazio por outro motivo (resposta vazia, bloqueio) NÃO é passageiro: "sem_status".
FALHA_TRANSITORIA = frozenset({"timeout", "503", "504"})
TIMEOUT_MESSAGE = "sem resposta dentro do teto da tentativa"  # gemini.with_retries
FALHAS_SEGUIDAS = 2
INTERVALO_SEGUNDOS = 21.0  # >= 20 s entre checagens (RPM 5 do nível gratuito)
MIN_CASOS_KAPPA = 10
# Busca nas agências do main: até 2 consultas por checagem (título/frase e palavras-chave).
FACTCHECK_POR_CASO = 2
GEMINI_LOGGER = "app.services.gemini"
_REQUISICOES = frozenset({"gemini_usage", "gemini_api_error"})


@dataclass(frozen=True)
class Caso:
    id: str
    tipo: str
    entrada: str
    resultado_esperado: str | None
    agencia_esperada: str | None
    url_checagem_esperada: str | None
    fonte_do_gabarito: str | None
    rotulado_por: str | None
    data: str | None

    @property
    def is_url(self) -> bool:
        return self.entrada.startswith(("http://", "https://"))


@dataclass(frozen=True)
class Resultado:
    caso_id: str
    tipo: str
    esperado: str | None
    obtido: str | None  # veredito da página, "checagem_de_agencia" ou None (sem IA/erro)
    status: str  # "avaliado" | "nao_avaliado" (IA não respondeu) | "erro:<código>"
    urls_citadas: tuple[str, ...]  # "O que dizem as agências"
    urls_encontradas: tuple[str, ...]  # citadas + relacionadas + a própria página da agência
    modelo: str | None
    segundos: float
    erros_ia: tuple[tuple[str, str], ...]  # (modelo, "503" | "429" | "timeout" | "sem_status")
    confianca: int | None = None
    # Regra da salvaguarda do #43: o veredito exibido só se sustenta com checagem citada de
    # OUTRO assunto (sem ela, "Verdadeiro"/"Falso" < 70% viraria "Ainda sem checagem").
    outro_assunto_sustenta: bool = False


@dataclass
class Execucao:
    """Preenchida aos poucos: se a execução cair no meio, o que já rodou continua aqui."""

    resultados: list[Resultado] = field(default_factory=list)
    parada: str | None = None  # None = rodou todos os rotulados
    chamadas_gemini: int = 0
    consultas_factcheck: int = 0  # teto: consultas enviadas (o main para na 1ª que acha)
    ignorados_pendentes: int = 0


# ---------------------------------------------------------------------------------------
# Casos
# ---------------------------------------------------------------------------------------


def _texto(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):  # YAML lê 2026-10-27 como data
        return value.isoformat()
    return str(value).strip()


def carregar_casos(path: Path) -> list[Caso]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get("casos") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{path}: falta a lista 'casos'")
    casos: list[Caso] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not _texto(item.get("id")):
            raise ValueError(f"{path}: caso sem id")
        case_id = _texto(item["id"]) or ""
        if case_id in seen:
            raise ValueError(f"{path}: id repetido: {case_id}")
        seen.add(case_id)
        tipo = _texto(item.get("tipo")) or ""
        if tipo not in TIPOS:
            raise ValueError(f"{case_id}: tipo inválido: {tipo!r}")
        casos.append(
            Caso(
                id=case_id,
                tipo=tipo,
                entrada=_texto(item.get("entrada")) or "",
                resultado_esperado=_texto(item.get("resultado_esperado")),
                agencia_esperada=_texto(item.get("agencia_esperada")),
                url_checagem_esperada=_texto(item.get("url_checagem_esperada")),
                fonte_do_gabarito=_texto(item.get("fonte_do_gabarito")),
                rotulado_por=_texto(item.get("rotulado_por")),
                data=_texto(item.get("data")),
            )
        )
    return casos


def _pendente(caso: Caso) -> bool:
    return (caso.rotulado_por or "").upper() == PENDENTE


def filtrar(
    casos: Sequence[Caso], *, tipos: Sequence[str] = (), ids: Sequence[str] = ()
) -> list[Caso]:
    """Subconjunto (ex.: dividir a rodada em duas noites). Tipo ou id desconhecido é erro:
    um erro de digitação não pode virar "rodou zero casos" em silêncio."""
    desconhecidos = [t for t in tipos if t not in TIPOS]
    if desconhecidos:
        raise ValueError(f"tipo desconhecido: {', '.join(desconhecidos)}")
    known = {c.id for c in casos}
    faltando = [i for i in ids if i not in known]
    if faltando:
        raise ValueError(f"id desconhecido: {', '.join(faltando)}")
    return [c for c in casos if (not tipos or c.tipo in tipos) and (not ids or c.id in ids)]


def rotulados(casos: Sequence[Caso]) -> list[Caso]:
    """Só os casos com gabarito. Rotulado pela metade é erro (nunca entra pela metade)."""
    out = []
    for caso in casos:
        if _pendente(caso):
            continue
        if not caso.rotulado_por:
            raise ValueError(f"{caso.id}: rotulado_por vazio (use PENDENTE ou o nome)")
        if caso.resultado_esperado not in ESPERADOS:
            raise ValueError(f"{caso.id}: resultado_esperado inválido: {caso.resultado_esperado!r}")
        if not caso.fonte_do_gabarito:
            raise ValueError(f"{caso.id}: falta fonte_do_gabarito")
        if not caso.data:
            raise ValueError(f"{caso.id}: falta a data da conferência")
        if not caso.entrada or caso.entrada.upper() == "PREENCHER":
            raise ValueError(f"{caso.id}: falta a entrada (texto ou link)")
        out.append(caso)
    return out


# ---------------------------------------------------------------------------------------
# Linhas vermelhas e métricas
# ---------------------------------------------------------------------------------------


def linhas_vermelhas(caso: Caso, r: Resultado) -> list[str]:
    """Erros que não podem acontecer (o alvo é 0).

    - verdadeiro_em_boato: "Verdadeiro" para o que o gabarito diz ser falso/enganoso/fora de
      contexto;
    - falso_em_fato: "Falso" para fato verdadeiro ou notícia legítima;
    - verdadeiro_sem_agencia: "Verdadeiro" sem checagem de agência citada (política de 10/10);
    - link_de_agencia_nao_reconhecido: o link é a própria checagem e a página não mostra isso
      (o bug de 22/10/2026). Reconhecer não depende da IA: vale também se ela falhou depois;
    - agencia_de_outro_assunto_sustentando: checagem citada de outro assunto sustenta o
      veredito (a mesma regra da salvaguarda do #43, verdict_policy.relevant_verdict).
    """
    regras = []
    if (
        caso.resultado_esperado == AGENCY_PAGE
        and r.status in ("avaliado", "nao_avaliado")
        and r.obtido != AGENCY_PAGE
    ):
        regras.append("link_de_agencia_nao_reconhecido")
    if r.status != "avaliado":
        return regras
    if r.obtido == "verdadeiro" and caso.resultado_esperado in BOATO:
        regras.append("verdadeiro_em_boato")
    if r.obtido == "falso" and (
        caso.resultado_esperado == "verdadeiro"
        or caso.tipo in ("fato_verdadeiro", "noticia_legitima")
    ):
        regras.append("falso_em_fato")
    if r.obtido == "verdadeiro" and not r.urls_citadas:
        regras.append("verdadeiro_sem_agencia")
    if r.outro_assunto_sustenta:
        regras.append("agencia_de_outro_assunto_sustentando")
    return regras


def outro_assunto_sustenta(
    veredito: str, confianca: int, citadas: Sequence[Mapping[str, Any]], keys: list[str] | None
) -> bool:
    """O veredito só se sustenta graças às citadas de outro assunto? (Regra do #43: com as
    do mesmo assunto não se sustenta, com todas as citadas, sim.)"""
    cited = [dict(c) for c in citadas]
    same, other = split_relevant(cited, keys)
    if not other:
        return False
    _, without_other = relevant_verdict(veredito, confianca, same)
    _, with_all = relevant_verdict(veredito, confianca, cited)
    return without_other is not None and with_all is None


def kappa(esperados: Sequence[str], obtidos: Sequence[str]) -> float | None:
    """Kappa de Cohen (concordância descontado o acaso). Indefinido com uma categoria só."""
    n = len(esperados)
    if n == 0 or n != len(obtidos):
        return None
    po = sum(e == o for e, o in zip(esperados, obtidos, strict=True)) / n
    ce, co = Counter(esperados), Counter(obtidos)
    pe = sum(ce[k] * co[k] for k in set(ce) | set(co)) / (n * n)
    if pe >= 1:
        return None
    return (po - pe) / (1 - pe)


def metricas(casos: Sequence[Caso], resultados: Sequence[Resultado]) -> dict[str, Any]:
    by_id = {c.id: c for c in casos}
    vermelhas = [
        {"caso": r.caso_id, "regra": regra}
        for r in resultados
        for regra in linhas_vermelhas(by_id[r.caso_id], r)
    ]
    avaliados = [r for r in resultados if r.status == "avaliado"]

    com_url = [r for r in avaliados if by_id[r.caso_id].url_checagem_esperada]
    encontradas = sum(
        any(same_page(by_id[r.caso_id].url_checagem_esperada, u) for u in r.urls_encontradas)
        for r in com_url
    )
    pares = [(r.esperado or "", r.obtido or "") for r in avaliados]
    acertos = sum(e == o for e, o in pares)
    esperados = [e for e, _ in pares]
    obtidos = [o for _, o in pares]

    tempos = [r.segundos for r in resultados]
    pior = max(resultados, key=lambda r: r.segundos, default=None)

    falhas: dict[str, Counter[str]] = {}
    for r in resultados:
        for modelo, status in r.erros_ia:
            falhas.setdefault(modelo, Counter())[status] += 1

    # Métrica (não linha vermelha): veredito categórico sem nenhuma checagem de agência citada.
    so_ia = [r.caso_id for r in avaliados if r.obtido in CATEGORICOS and not r.urls_citadas]

    return {
        "linhas_vermelhas": vermelhas,
        "veredito_so_da_ia": {"total": len(so_ia), "casos": so_ia},
        "checagem_certa": {
            "encontradas": encontradas,
            "total": len(com_url),
            "taxa": encontradas / len(com_url) if com_url else None,
        },
        "concordancia": {
            "acertos": acertos,
            "total": len(pares),
            "taxa": acertos / len(pares) if pares else None,
            "kappa": kappa(esperados, obtidos) if len(pares) >= MIN_CASOS_KAPPA else None,
        },
        "tempo": {
            "mediana_s": statistics.median(tempos) if tempos else None,
            "pior_s": pior.segundos if pior else None,
            "pior_caso": pior.caso_id if pior else None,
        },
        "falhas_ia_por_modelo": {m: dict(sorted(c.items())) for m, c in sorted(falhas.items())},
        "contagem": dict(sorted(Counter(r.status for r in resultados).items())),
    }


# ---------------------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------------------


class _Requisicoes(logging.Handler):
    """Uma linha de log por requisição real ao Google (sucesso ou erro)."""

    def __init__(self) -> None:
        super().__init__()
        self.registros: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() in _REQUISICOES:
            self.registros.append(record)


@contextmanager
def _ouvindo_gemini() -> Iterator[_Requisicoes]:
    handler = _Requisicoes()
    gemini = logging.getLogger(GEMINI_LOGGER)
    level = gemini.level
    gemini.setLevel(logging.INFO)  # gemini_usage é INFO
    gemini.addHandler(handler)
    try:
        yield handler
    finally:
        gemini.removeHandler(handler)
        gemini.setLevel(level)


class _AnalisadorContado:
    """Conta as requisições feitas por cada análise (no mínimo 1: na dúvida, conta)."""

    def __init__(self, inner: Any, requisicoes: _Requisicoes, execucao: Execucao) -> None:
        self._inner = inner
        self._requisicoes = requisicoes
        self._execucao = execucao

    async def analyze(self, *args: Any, **kwargs: Any) -> Any:
        before = len(self._requisicoes.registros)
        try:
            return await self._inner.analyze(*args, **kwargs)
        finally:
            self._execucao.chamadas_gemini += max(1, len(self._requisicoes.registros) - before)


class _FactCheckContado:
    def __init__(self, inner: Any, execucao: Execucao) -> None:
        self._inner = inner
        self._execucao = execucao

    async def search(self, queries: list[str]) -> list[AgencyCheck]:
        self._execucao.consultas_factcheck += len(queries)
        return await self._inner.search(queries)  # type: ignore[no-any-return]


def rotulo_erro(record: logging.LogRecord) -> str:
    status = getattr(record, "status", None)
    if status is not None:
        return str(status)
    message = str(getattr(record, "google_message", ""))
    return "timeout" if message.startswith(TIMEOUT_MESSAGE) else "sem_status"


async def _um_caso(
    caso: Caso,
    tools: CheckTools,
    configuracao: CheckSettings,
    relogio: Callable[[], float],
    requisicoes: _Requisicoes,
) -> tuple[Resultado, list[str]]:
    before = len(requisicoes.registros)
    inicio = relogio()
    url, texto = (caso.entrada, None) if caso.is_url else (None, caso.entrada)
    outcome = None
    status = "avaliado"
    try:
        outcome = await run_check(
            user_id=uuid4(),
            url=url,
            texto=texto,
            repo=FakeCheckRepository(),  # novo a cada caso: sem cache
            tools=tools,
            settings=configuracao,
        )
    except AppError as exc:
        status = f"erro:{exc.code}"
    segundos = relogio() - inicio
    errors = [r for r in requisicoes.registros[before:] if r.getMessage() == "gemini_api_error"]
    rotulos = [rotulo_erro(r) for r in errors]
    erros_ia = tuple(
        (str(getattr(r, "model", "?")), rotulo) for r, rotulo in zip(errors, rotulos, strict=True)
    )

    obtido: str | None = None
    citadas: tuple[str, ...] = ()
    encontradas: tuple[str, ...] = ()
    modelo: str | None = None
    confianca: int | None = None
    sustenta = False
    if outcome is not None:
        out = to_out(outcome.check, configuracao)
        citadas = tuple(a.url for a in out.checagens_agencias)
        encontradas = citadas + tuple(a.url for a in out.checagens_relacionadas)
        if out.modo == AGENCY_PAGE and out.checagem_de_agencia is not None:
            obtido = AGENCY_PAGE
            encontradas += (out.checagem_de_agencia.url,)
        elif outcome.agencies_only:
            status = "nao_avaliado"  # a IA não respondeu: sem veredito para comparar
        else:
            obtido, confianca = out.veredito, out.confianca
            modelo = outcome.check.result.get("model")
            # Palavras-chave como a salvaguarda usa: as gravadas na checagem (ou, sem elas,
            # as do título e do texto).
            stored = outcome.check.result.get("palavras_chave")
            keys = (
                [k for k in stored if isinstance(k, str)]
                if isinstance(stored, list)
                else content_keys(out.title, out.texto)
            )
            sustenta = outro_assunto_sustenta(
                out.veredito,
                out.confianca,
                [a.model_dump() for a in out.checagens_agencias],
                keys,
            )
    resultado = Resultado(
        caso_id=caso.id,
        tipo=caso.tipo,
        esperado=caso.resultado_esperado,
        obtido=obtido,
        status=status,
        urls_citadas=citadas,
        urls_encontradas=encontradas,
        modelo=modelo,
        segundos=round(segundos, 2),
        erros_ia=erros_ia,
        confianca=confianca,
        outro_assunto_sustenta=sustenta,
    )
    return resultado, rotulos


async def avaliar(
    casos: Sequence[Caso],
    ferramentas: CheckTools,
    configuracao: CheckSettings,
    *,
    orcamento: int,
    max_por_caso: int,
    orcamento_factcheck: int | None = None,
    intervalo: float = INTERVALO_SEGUNDOS,
    dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    relogio: Callable[[], float] = time.monotonic,
    execucao: Execucao | None = None,
) -> Execucao:
    """max_por_caso: pior caso de requisições ao Gemini numa checagem. Passe `execucao` para
    guardar o que já rodou mesmo se a execução for interrompida (exceção ou Ctrl+C)."""
    if orcamento < 1:
        raise ValueError("orçamento de chamadas ao Gemini precisa ser >= 1")
    if max_por_caso < 1:
        raise ValueError("max_por_caso precisa ser >= 1")
    execucao = execucao if execucao is not None else Execucao()
    alvo = rotulados(casos)
    execucao.ignorados_pendentes = sum(_pendente(c) for c in casos)
    seguidas = 0
    with _ouvindo_gemini() as requisicoes:
        tools = replace(
            ferramentas,
            analyzer=_AnalisadorContado(ferramentas.analyzer, requisicoes, execucao),
            factcheck=_FactCheckContado(ferramentas.factcheck, execucao),
        )
        for index, caso in enumerate(alvo):
            if execucao.chamadas_gemini + max_por_caso > orcamento:
                execucao.parada = "orçamento"
                break
            if (
                orcamento_factcheck is not None
                and execucao.consultas_factcheck + FACTCHECK_POR_CASO > orcamento_factcheck
            ):
                execucao.parada = "orçamento da Fact Check"
                break
            if index:
                await dormir(intervalo)
            resultado, rotulos = await _um_caso(caso, tools, configuracao, relogio, requisicoes)
            execucao.resultados.append(resultado)
            if "429" in rotulos:
                execucao.parada = "429"
                break
            transitoria = bool(rotulos) and all(r in FALHA_TRANSITORIA for r in rotulos)
            seguidas = seguidas + 1 if resultado.status == "nao_avaliado" and transitoria else 0
            if seguidas >= FALHAS_SEGUIDAS:
                execucao.parada = "2 falhas seguidas (timeout/503)"
                break
    return execucao


# ---------------------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _markdown(dados: dict[str, Any]) -> str:
    m = dados
    c = m["consumo"]
    linhas = [
        f"# Avaliação — {m['data']} — commit `{m['commit']}`",
        "",
        f"- Casos rotulados: {m['casos_rotulados']} · pendentes ignorados: "
        f"{m['ignorados_pendentes']} · executados: {len(m['resultados'])}",
        f"- Parada: {m['parada'] or 'nenhuma (todos os rotulados)'}",
        f"- Situação dos casos: {m['contagem']}",
        "",
        "## Consumo",
        "",
        f"- Modelo usado: {', '.join(c['modelos_configurados']) or '—'}"
        f" (respondeu: {c['modelo_que_respondeu'] or '—'})",
        f"- Chamadas ao Gemini gastas: {c['chamadas_gemini']} de {c['orcamento_gemini']}",
        f"- Consultas à Fact Check gastas: {c['consultas_factcheck']} (teto: consultas enviadas)",
        "",
        "## Linhas vermelhas (devem ser 0)",
        "",
    ]
    vermelhas = m["linhas_vermelhas"]
    linhas += [f"- `{v['caso']}`: {v['regra']}" for v in vermelhas] or ["Nenhuma."]
    so_ia = m["veredito_so_da_ia"]
    linhas += [
        "",
        "## Veredito só da IA, sem agência citada (métrica, não linha vermelha)",
        "",
        f"{so_ia['total']} caso(s)" + (": " + ", ".join(f"`{i}`" for i in so_ia["casos"]))
        if so_ia["casos"]
        else "0 casos.",
    ]
    cc, conc, tempo = m["checagem_certa"], m["concordancia"], m["tempo"]
    kappa_txt = "—" if conc["kappa"] is None else f"{conc['kappa']:.2f}"
    linhas += [
        "",
        "## Checagem certa encontrada",
        "",
        f"{cc['encontradas']} de {cc['total']} ({_pct(cc['taxa'])}) — casos avaliados com "
        "url_checagem_esperada.",
        "",
        "## Concordância de veredito",
        "",
        f"{conc['acertos']} de {conc['total']} ({_pct(conc['taxa'])}) · kappa: {kappa_txt}"
        f" (calculado com {MIN_CASOS_KAPPA}+ casos avaliados).",
        "",
        "## Tempo",
        "",
        f"Mediana: {tempo['mediana_s']} s · pior caso: {tempo['pior_s']} s"
        f" (`{tempo['pior_caso']}`).",
        "",
        "## Falhas da IA por modelo",
        "",
    ]
    falhas = m["falhas_ia_por_modelo"]
    linhas += [f"- {modelo}: {contagem}" for modelo, contagem in falhas.items()] or ["Nenhuma."]
    linhas += [
        "",
        "## Casos",
        "",
        "| caso | tipo | esperado | obtido | situação | s | modelo |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    linhas += [
        f"| {r['caso_id']} | {r['tipo']} | {r['esperado']} | {r['obtido'] or '—'} | "
        f"{r['status']} | {r['segundos']} | {r['modelo'] or '—'} |"
        for r in m["resultados"]
    ]
    return "\n".join(linhas) + "\n"


def _livre(pasta: Path, nome: str) -> Path:
    """AAAA-MM-DD_<commit>; se já existe (2ª execução no mesmo dia e commit), -2, -3..."""
    base, n = pasta / nome, 2
    while base.with_suffix(".md").exists() or base.with_suffix(".json").exists():
        base, n = pasta / f"{nome}-{n}", n + 1
    return base


def gravar_relatorio(
    casos: Sequence[Caso],
    execucao: Execucao,
    *,
    pasta: Path,
    commit: str,
    dia: str,
    orcamento: int,
    modelos: Sequence[str] = (),
) -> tuple[Path, Path]:
    respondeu = Counter(r.modelo for r in execucao.resultados if r.modelo)
    dados = {
        "commit": commit,
        "data": dia,
        "orcamento": orcamento,
        "chamadas_gemini": execucao.chamadas_gemini,
        "consultas_factcheck": execucao.consultas_factcheck,
        "consumo": {
            "modelos_configurados": list(modelos),
            "modelo_que_respondeu": dict(sorted(respondeu.items())),
            "chamadas_gemini": execucao.chamadas_gemini,
            "orcamento_gemini": orcamento,
            "consultas_factcheck": execucao.consultas_factcheck,
        },
        "parada": execucao.parada,
        "casos_rotulados": len(rotulados(casos)),
        "ignorados_pendentes": execucao.ignorados_pendentes,
        **metricas(casos, execucao.resultados),
        "resultados": [asdict(r) for r in execucao.resultados],
    }
    pasta.mkdir(parents=True, exist_ok=True)
    base = _livre(pasta, f"{dia}_{commit}")
    md, js = base.with_suffix(".md"), base.with_suffix(".json")
    js.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md.write_text(_markdown(dados), encoding="utf-8")
    return md, js


# ---------------------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------------------


def _positivo(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("precisa ser >= 1")
    return number


def _lista(value: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(n.strip() for n in value.split(",") if n.strip()))
    if not names:
        raise argparse.ArgumentTypeError("lista vazia")
    return names


def _modelos(value: str) -> tuple[str, ...]:
    return _lista(value)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Avaliação com gabarito (chamadas reais).")
    parser.add_argument("--orcamento", type=_positivo, required=True, help="máximo ao Gemini")
    parser.add_argument("--orcamento-factcheck", type=_positivo, default=None)
    parser.add_argument("--casos", type=Path, default=CASOS_PADRAO)
    parser.add_argument(
        "--modelos", type=_modelos, help="cadeia separada por vírgula (padrão: a da produção)"
    )
    parser.add_argument("--tipos", type=_lista, default=(), help="ex.: boato_checado,link_agencia")
    parser.add_argument("--ids", type=_lista, default=(), help="ids separados por vírgula")
    parser.add_argument("--intervalo", type=float, default=INTERVALO_SEGUNDOS)
    parser.add_argument("--saida", type=Path, default=RESULTADOS)
    return parser.parse_args(argv)


def pior_caso_gemini(modelos: Sequence[str], rpd: Mapping[str, int] | None = None) -> int:
    """Uma tentativa por modelo (max_retries=0), e a cadeia pode rodar MAX_ATTEMPTS vezes.
    Modelo desligado (RPD 0) nunca é chamado: não entra na conta."""
    ligados = [m for m in modelos if (rpd or {}).get(m, 1) != 0]
    return max(1, len(ligados)) * MAX_ATTEMPTS


def _commit() -> str:
    def git(*args: str) -> str:
        # Comando fixo, sem entrada do usuário.
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            cwd=HERE,
        ).stdout.strip()

    try:
        sha = git("rev-parse", "--short", "HEAD")
        # Só arquivos versionados: um relatório novo (não versionado) não marca "-sujo".
        return sha + ("-sujo" if git("status", "--porcelain", "--untracked-files=no") else "")
    except (OSError, subprocess.CalledProcessError):
        return "semcommit"


def main(argv: Sequence[str] | None = None) -> None:  # pragma: no cover - chamadas reais
    from app.config import get_settings
    from app.routers.checks import _NoFactCheck, get_check_settings
    from app.services.factcheck import HttpFactCheckClient
    from app.services.fetcher import HttpPageFetcher
    from app.services.gemini import GeminiAnalyzer, google_generate

    args = parse_args(sys.argv[1:] if argv is None else argv)
    if os.environ.get(REAL_RUN_FLAG) != "1":
        raise SystemExit(f"Chamada real exige autorização do usuário e {REAL_RUN_FLAG}=1.")
    settings = get_settings()
    if not settings.gemini_api_key:
        raise SystemExit("GEMINI_API_KEY ausente (rode com CHECAFATOS_DOTENV=1)")
    modelos = args.modelos or settings.gemini_models
    generate = google_generate(
        settings.gemini_api_key,
        modelos,
        thinking_budget=settings.gemini_thinking_budget,
        thinking_level=settings.gemini_thinking_level,
        attempt_timeout=settings.gemini_attempt_timeout_seconds,
        max_retries=0,
        budget_seconds=settings.gemini_budget_seconds,
        limits=dict(settings.model_limits),
    )
    key = settings.google_factcheck_api_key
    tools = CheckTools(
        fetcher=HttpPageFetcher(
            timeout_seconds=settings.fetch_timeout_seconds, max_bytes=settings.fetch_max_bytes
        ),
        factcheck=HttpFactCheckClient(key) if key else _NoFactCheck(),
        analyzer=GeminiAnalyzer(generate, budget_seconds=settings.gemini_budget_seconds),
    )
    casos = filtrar(carregar_casos(args.casos), tipos=args.tipos, ids=args.ids)
    execucao = Execucao()
    try:
        asyncio.run(
            avaliar(
                casos,
                tools,
                get_check_settings(settings),
                orcamento=args.orcamento,
                max_por_caso=pior_caso_gemini(
                    modelos, {m: lim.rpd for m, lim in settings.model_limits.items()}
                ),
                orcamento_factcheck=args.orcamento_factcheck,
                intervalo=args.intervalo,
                execucao=execucao,
            )
        )
    except BaseException as exc:
        execucao.parada = f"interrompida ({type(exc).__name__})"
        raise
    finally:
        md, js = gravar_relatorio(
            casos,
            execucao,
            pasta=args.saida,
            commit=_commit(),
            dia=date.today().isoformat(),
            orcamento=args.orcamento,
            modelos=modelos,
        )
        print(f"Relatório: {md}\n           {js}")
        print(
            f"Gemini: {execucao.chamadas_gemini}/{args.orcamento} · Fact Check (teto): "
            f"{execucao.consultas_factcheck} · parada: {execucao.parada}"
        )


if __name__ == "__main__":
    main()
