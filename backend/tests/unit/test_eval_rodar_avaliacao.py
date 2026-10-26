"""Avaliação com gabarito (backend/eval/rodar_avaliacao.py): só mocks, nenhuma chamada real."""

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from app.repositories.checks import Limits
from app.services.check_service import CheckSettings, CheckTools
from app.services.factcheck import AgencyCheck
from app.services.fetcher import PageUnreadableError
from app.services.gemini import GeminiQuotaExhaustedError, GeminiUnavailableError
from eval.rodar_avaliacao import (
    CASOS_PADRAO,
    ESPERADOS,
    HERE,
    PENDENTE,
    TIPOS,
    Caso,
    CotaInsuficienteError,
    Execucao,
    Resultado,
    avaliar,
    carregar_casos,
    conferir_cota,
    filtrar,
    gravar_relatorio,
    kappa,
    linhas_vermelhas,
    metricas,
    outro_assunto_sustenta,
    parse_args,
    pior_caso_gemini,
    rotulados,
)
from tests.fakes import FakeAnalyzer, FakeFactCheck, FakeFetcher

pytestmark = pytest.mark.anyio

ESTADAO = "https://www.estadao.com.br/estadao-verifica/frude-transmissao-alteracao-votos-leicoes/"
SETTINGS = CheckSettings(
    limits=Limits(cache_hours=24, checks_per_hour=100, lookups_per_hour=100, prompt_version="v"),
    daily_gemini_checks=1000,
    model="gemini-teste",
)
GEMINI_LOG = logging.getLogger("app.services.gemini")


def caso(
    case_id: str,
    *,
    tipo: str = "boato_checado",
    entrada: str = "URGENTE: a eleição foi adiada para novembro. Repassem para todos!",
    esperado: str | None = "falso",
    url: str | None = None,
    rotulado_por: str = "Bruno",
) -> Caso:
    labelled = rotulado_por != PENDENTE
    return Caso(
        id=case_id,
        tipo=tipo,
        entrada=entrada,
        resultado_esperado=esperado,
        agencia_esperada=None,
        url_checagem_esperada=url,
        fonte_do_gabarito="https://exemplo.org/checagem" if labelled else None,
        rotulado_por=rotulado_por,
        data="2026-10-27" if labelled else None,
    )


def resultado(
    caso_: Caso,
    obtido: str | None,
    *,
    status: str = "avaliado",
    citadas: tuple[str, ...] = (),
    segundos: float = 5.0,
) -> Resultado:
    return Resultado(
        caso_id=caso_.id,
        tipo=caso_.tipo,
        esperado=caso_.resultado_esperado,
        obtido=obtido,
        status=status,
        urls_citadas=citadas,
        urls_encontradas=citadas,
        modelo="gemini-teste" if status == "avaliado" else None,
        segundos=segundos,
        erros_ia=(),
    )


# ---------- o arquivo de casos ----------


def write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "casos.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_labelled_and_pending_cases(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path,
        """
casos:
  - id: boato-1
    tipo: boato_checado
    entrada: "A eleição foi adiada."
    resultado_esperado: falso
    agencia_esperada: Aos Fatos
    url_checagem_esperada: https://www.aosfatos.org/noticias/x/
    fonte_do_gabarito: https://www.aosfatos.org/noticias/x/
    rotulado_por: Caio
    data: 2026-10-27
  - id: link-1
    tipo: link_agencia
    entrada: https://www.estadao.com.br/estadao-verifica/y/
    resultado_esperado: null
    agencia_esperada: null
    url_checagem_esperada: null
    fonte_do_gabarito: null
    rotulado_por: PENDENTE
    data: null
""",
    )
    casos = carregar_casos(path)
    assert [c.id for c in casos] == ["boato-1", "link-1"]
    assert casos[0].data == "2026-10-27"  # data do YAML vira texto ISO
    assert [c.id for c in rotulados(casos)] == ["boato-1"]
    assert casos[1].is_url and not casos[0].is_url


@pytest.mark.parametrize(
    ("campo", "valor", "erro"),
    [
        ("tipo", "boato", "tipo"),
        ("resultado_esperado", "mentira", "resultado_esperado"),
        ("rotulado_por", "", "rotulado_por"),
        ("fonte_do_gabarito", "", "fonte_do_gabarito"),
        ("data", None, "data"),
    ],
)
def test_labelled_case_must_be_complete(
    tmp_path: Path, campo: str, valor: str | None, erro: str
) -> None:
    fields: dict[str, str | None] = {
        "id": "c1",
        "tipo": "boato_checado",
        "entrada": "texto",
        "resultado_esperado": "falso",
        "agencia_esperada": None,
        "url_checagem_esperada": None,
        "fonte_do_gabarito": "https://x.org",
        "rotulado_por": "Bruno",
        "data": "2026-10-27",
    }
    fields[campo] = valor
    path = write_yaml(tmp_path, json.dumps({"casos": [fields]}, ensure_ascii=False))
    with pytest.raises(ValueError, match=erro):
        rotulados(carregar_casos(path))


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    item = {"id": "c1", "tipo": "boato_checado", "entrada": "t", "rotulado_por": PENDENTE}
    path = write_yaml(tmp_path, json.dumps({"casos": [item, item]}))
    with pytest.raises(ValueError, match="repetido"):
        carregar_casos(path)


def test_the_repository_file_has_the_30_candidates_10_5_5_5_5_and_is_valid() -> None:
    casos = carregar_casos(CASOS_PADRAO)
    assert len(casos) == 30
    assert Counter(c.tipo for c in casos) == {
        "boato_checado": 10,
        "fato_verdadeiro": 5,
        "link_agencia": 5,
        "noticia_legitima": 5,
        "boato_sem_checagem": 5,
    }
    assert set(TIPOS) == {c.tipo for c in casos}
    # Os rotulados (à medida que forem conferidos) precisam estar completos.
    rotulados(casos)
    # Caso ainda pendente não carrega gabarito pela metade.
    assert all(c.resultado_esperado is None for c in casos if c.rotulado_por == PENDENTE)


# ---------- linhas vermelhas ----------


@pytest.mark.parametrize(
    ("caso_", "obtido", "citadas", "regras"),
    [
        (caso("b"), "verdadeiro", ("https://a.org/x",), ["verdadeiro_em_boato"]),
        (caso("b"), "falso", (), []),
        (
            caso("f", tipo="fato_verdadeiro", esperado="verdadeiro"),
            "falso",
            (),
            ["falso_em_fato"],
        ),
        (
            caso("n", tipo="noticia_legitima", esperado="nao_verificavel"),
            "falso",
            (),
            ["falso_em_fato"],
        ),
        (caso("x", esperado="nao_verificavel"), "verdadeiro", (), ["verdadeiro_sem_agencia"]),
        (
            caso("l", tipo="link_agencia", entrada=ESTADAO, esperado="checagem_de_agencia"),
            "nao_verificavel",
            (),
            ["link_de_agencia_nao_reconhecido"],
        ),
        (
            caso("l", tipo="link_agencia", entrada=ESTADAO, esperado="checagem_de_agencia"),
            "checagem_de_agencia",
            (),
            [],
        ),
    ],
)
def test_red_lines(caso_: Caso, obtido: str, citadas: tuple[str, ...], regras: list[str]) -> None:
    assert linhas_vermelhas(caso_, resultado(caso_, obtido, citadas=citadas)) == regras


def test_not_evaluated_cases_never_count_as_red_lines() -> None:
    c = caso("b")
    assert linhas_vermelhas(c, resultado(c, None, status="nao_avaliado")) == []


def test_unrecognised_agency_link_is_a_red_line_even_when_the_ai_failed_afterwards() -> None:
    # Reconhecer o link não depende da IA: não reconheceu, foi para o Gemini, ele falhou.
    link = caso("l", tipo="link_agencia", entrada=ESTADAO, esperado="checagem_de_agencia")
    failed = resultado(link, None, status="nao_avaliado")
    assert linhas_vermelhas(link, failed) == ["link_de_agencia_nao_reconhecido"]
    unreadable = resultado(link, None, status="erro:pagina_ilegivel")
    assert linhas_vermelhas(link, unreadable) == []  # sem ler a página não há como reconhecer


# ---------- métricas ----------


def test_kappa_is_one_for_perfect_agreement_and_zero_for_chance() -> None:
    assert kappa(["falso", "verdadeiro"] * 5, ["falso", "verdadeiro"] * 5) == pytest.approx(1.0)
    chance = kappa(["falso", "falso", "verdadeiro", "verdadeiro"] * 3, ["falso", "verdadeiro"] * 6)
    assert chance == pytest.approx(0.0)
    assert kappa(["falso"] * 10, ["falso"] * 10) is None  # uma categoria só: indefinido


def test_metrics() -> None:
    casos = [caso(f"b{i}", url=f"https://agencia.org/{i}") for i in range(10)]
    casos.append(caso("f", tipo="fato_verdadeiro", esperado="verdadeiro"))
    res = [
        resultado(c, "falso", citadas=(f"https://agencia.org/{i}",), segundos=float(i + 1))
        for i, c in enumerate(casos[:8])
    ]
    res.append(resultado(casos[8], "verdadeiro", citadas=("https://outra.org/z",), segundos=30))
    res.append(resultado(casos[9], None, status="nao_avaliado", segundos=18))
    res.append(resultado(casos[10], "falso", segundos=4))

    m = metricas(casos, res)

    assert m["linhas_vermelhas"] == [
        {"caso": "b8", "regra": "verdadeiro_em_boato"},
        {"caso": "f", "regra": "falso_em_fato"},
    ]
    # Checagem certa: só entre os avaliados com url esperada (o não avaliado não entra).
    assert m["checagem_certa"] == {"encontradas": 8, "total": 9, "taxa": pytest.approx(8 / 9)}
    assert m["concordancia"]["acertos"] == 8
    assert m["concordancia"]["total"] == 10
    assert m["concordancia"]["kappa"] is not None  # 10 casos, 2 categorias
    # Tempo de todas as checagens executadas (o usuário esperou também nas não avaliadas).
    assert m["tempo"]["pior_s"] == 30 and m["tempo"]["pior_caso"] == "b8"
    assert m["tempo"]["mediana_s"] == pytest.approx(5.0)
    assert m["contagem"] == {"avaliado": 10, "nao_avaliado": 1}


def test_kappa_is_omitted_with_few_cases() -> None:
    c = caso("b")
    assert metricas([c], [resultado(c, "falso")])["concordancia"]["kappa"] is None


def test_found_check_matches_url_variants() -> None:
    c = caso("b", url=ESTADAO)
    found = resultado(c, "falso", citadas=(ESTADAO.rstrip("/") + "?utm_source=x",))
    assert metricas([c], [found])["checagem_certa"]["encontradas"] == 1


# ---------- execução (mocks) ----------


class ScriptedAnalyzer(FakeAnalyzer):
    """Cada chamada segue um roteiro e loga como o cliente real (gemini_usage/gemini_api_error)."""

    def __init__(self, script: list[tuple[Any, ...]]) -> None:
        super().__init__()
        self.script = list(script)
        self.calls = 0

    async def analyze(self, data: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        step = self.script.pop(0)
        if step[0] == "ok":
            _, veredito, model = step
            GEMINI_LOG.info("gemini_usage", extra={"model": model})
            self.veredito, self.model = veredito, model
            return await super().analyze(data)
        _, status, model, *rest = step
        # Como o cliente real: status None com a mensagem de timeout nosso, salvo se o roteiro
        # disser outra (ex.: resposta vazia).
        message = rest[0] if rest else "sem resposta dentro do teto da tentativa"
        GEMINI_LOG.warning(
            "gemini_api_error",
            extra={"model": model, "status": status, "google_message": message},
        )
        if status == 429:
            raise GeminiQuotaExhaustedError
        raise GeminiUnavailableError("modelo lento")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        self.now += 1.0
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def tools(analyzer: FakeAnalyzer, factcheck: FakeFactCheck | None = None) -> CheckTools:
    return CheckTools(
        fetcher=FakeFetcher(), factcheck=factcheck or FakeFactCheck(), analyzer=analyzer
    )


async def run(
    casos: list[Caso],
    analyzer: FakeAnalyzer,
    *,
    factcheck: FakeFactCheck | None = None,
    orcamento: int = 10,
    max_por_caso: int = 1,
    orcamento_factcheck: int | None = None,
    execucao: Execucao | None = None,
) -> Any:
    clock = Clock()
    result = await avaliar(
        casos,
        tools(analyzer, factcheck),
        SETTINGS,
        orcamento=orcamento,
        max_por_caso=max_por_caso,
        orcamento_factcheck=orcamento_factcheck,
        dormir=clock.sleep,
        relogio=clock,
        execucao=execucao,
    )
    return result, clock


async def test_evaluates_labelled_cases_and_ignores_pending() -> None:
    analyzer = ScriptedAnalyzer([("ok", "falso", "m1"), ("ok", "falso", "m1")])
    casos = [caso("a"), caso("p", rotulado_por=PENDENTE, esperado=None), caso("b")]

    execucao, clock = await run(casos, analyzer)

    assert [r.caso_id for r in execucao.resultados] == ["a", "b"]
    assert all(r.status == "avaliado" and r.obtido == "falso" for r in execucao.resultados)
    assert execucao.chamadas_gemini == 2
    assert execucao.parada is None
    assert execucao.ignorados_pendentes == 1
    assert clock.sleeps == [21.0]  # pausa entre checagens (RPM), não antes da primeira


async def test_each_case_starts_without_cache() -> None:
    # Mesma entrada duas vezes: com o mesmo repositório, a 2ª viria do cache (sem Gemini).
    analyzer = ScriptedAnalyzer([("ok", "falso", "m1"), ("ok", "falso", "m1")])
    execucao, _ = await run([caso("a"), caso("b")], analyzer)
    assert analyzer.calls == 2
    assert execucao.chamadas_gemini == 2


async def test_budget_is_checked_before_each_case() -> None:
    # Até 2 chamadas por caso: 0+2, 1+2 e 2+2 cabem em 4; com 3 usadas, 3+2 já não cabe.
    analyzer = ScriptedAnalyzer([("ok", "falso", "m1")] * 5)
    execucao, _ = await run(
        [caso(f"c{i}") for i in range(5)], analyzer, orcamento=4, max_por_caso=2
    )
    assert len(execucao.resultados) == 3
    assert execucao.chamadas_gemini == 3
    assert execucao.parada == "orçamento"


@pytest.mark.parametrize("budget", [0, -1])
async def test_budget_must_be_positive(budget: int) -> None:
    with pytest.raises(ValueError, match="orçamento"):
        await run([caso("a")], ScriptedAnalyzer([]), orcamento=budget)


async def test_stops_at_429() -> None:
    analyzer = ScriptedAnalyzer([("ok", "falso", "m1"), ("erro", 429, "m1"), ("ok", "falso", "m1")])
    execucao, _ = await run([caso("a"), caso("b"), caso("c")], analyzer)

    assert execucao.parada == "429"
    assert [r.status for r in execucao.resultados] == ["avaliado", "nao_avaliado"]
    assert analyzer.calls == 2


@pytest.mark.parametrize("status", [None, 503, 504])
async def test_stops_after_two_timeouts_or_503_in_a_row(status: int | None) -> None:
    analyzer = ScriptedAnalyzer(
        [("erro", status, "m1"), ("erro", status, "m1"), ("ok", "falso", "m1")]
    )
    execucao, _ = await run([caso("a"), caso("b"), caso("c")], analyzer)

    assert execucao.parada == "2 falhas seguidas (timeout/503)"
    assert [r.status for r in execucao.resultados] == ["nao_avaliado", "nao_avaliado"]
    assert [r.obtido for r in execucao.resultados] == [None, None]


async def test_one_failure_then_success_resets_the_streak() -> None:
    analyzer = ScriptedAnalyzer(
        [("erro", 503, "m1"), ("ok", "falso", "m2"), ("erro", None, "m1"), ("ok", "falso", "m2")]
    )
    execucao, _ = await run([caso(c) for c in "abcd"], analyzer)
    assert execucao.parada is None
    assert [r.status for r in execucao.resultados] == [
        "nao_avaliado",
        "avaliado",
        "nao_avaliado",
        "avaliado",
    ]


async def test_ai_failures_are_counted_by_model() -> None:
    analyzer = ScriptedAnalyzer(
        [("erro", 503, "m1"), ("ok", "falso", "m2"), ("erro", None, "m1"), ("ok", "falso", "m2")]
    )
    casos = [caso(c) for c in "abcd"]
    execucao, _ = await run(casos, analyzer)
    assert metricas(casos, execucao.resultados)["falhas_ia_por_modelo"] == {
        "m1": {"503": 1, "timeout": 1}
    }


async def test_agency_link_is_evaluated_as_agency_check_without_ai() -> None:
    factcheck = FakeFactCheck()
    factcheck.results = [
        AgencyCheck(agencia="Estadão", titulo="T", veredito="Falso", url=ESTADAO, alegacao="A")
    ]
    analyzer = ScriptedAnalyzer([])
    link = caso(
        "l", tipo="link_agencia", entrada=ESTADAO, esperado="checagem_de_agencia", url=ESTADAO
    )

    execucao, _ = await run([link], analyzer, factcheck=factcheck)

    (r,) = execucao.resultados
    assert (r.status, r.obtido) == ("avaliado", "checagem_de_agencia")
    assert r.urls_encontradas == (ESTADAO,)
    assert analyzer.calls == 0 and execucao.chamadas_gemini == 0


async def test_found_checks_include_related_ones() -> None:
    factcheck = FakeFactCheck()
    factcheck.results = [
        AgencyCheck(agencia="Lupa", titulo="Outro assunto", veredito="Falso", url="https://l.org/1")
    ]
    analyzer = ScriptedAnalyzer([("ok", "falso", "m1")])
    execucao, _ = await run([caso("a")], analyzer, factcheck=factcheck)
    assert "https://l.org/1" in execucao.resultados[0].urls_encontradas


async def test_unreadable_link_is_recorded_as_error_not_as_a_verdict() -> None:
    analyzer = ScriptedAnalyzer([])
    fetcher = FakeFetcher()
    fetcher.error = PageUnreadableError("bloqueado")
    link = caso(
        "n",
        tipo="noticia_legitima",
        entrada="https://g1.globo.com/x.ghtml",
        esperado="nao_verificavel",
    )
    clock = Clock()
    execucao = await avaliar(
        [link],
        CheckTools(fetcher=fetcher, factcheck=FakeFactCheck(), analyzer=analyzer),
        SETTINGS,
        orcamento=5,
        max_por_caso=1,
        dormir=clock.sleep,
        relogio=clock,
    )
    (r,) = execucao.resultados
    assert r.status == "erro:pagina_ilegivel" and r.obtido is None
    assert linhas_vermelhas(link, r) == []


# ---------- relatório ----------


async def test_report_files_are_written_with_the_commit_in_the_name(tmp_path: Path) -> None:
    analyzer = ScriptedAnalyzer([("ok", "falso", "m1")])
    casos = [caso("a")]
    execucao, _ = await run(casos, analyzer)

    md, js = gravar_relatorio(
        casos, execucao, pasta=tmp_path, commit="abc1234", dia="2026-10-27", orcamento=10
    )

    assert md.name == "2026-10-27_abc1234.md" and js.name == "2026-10-27_abc1234.json"
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["commit"] == "abc1234"
    assert data["linhas_vermelhas"] == []
    assert data["chamadas_gemini"] == 1 and data["orcamento"] == 10
    assert data["resultados"][0]["caso_id"] == "a"
    text = md.read_text(encoding="utf-8")
    for heading in (
        "Linhas vermelhas",
        "Checagem certa encontrada",
        "Concordância de veredito",
        "Tempo",
        "Falhas da IA por modelo",
    ):
        assert heading in text


# ---------- linha de comando ----------


def test_budget_argument_is_required() -> None:
    with pytest.raises(SystemExit):
        parse_args([])
    assert parse_args(["--orcamento", "6"]).orcamento == 6
    with pytest.raises(SystemExit):
        parse_args(["--orcamento", "0"])


def test_models_argument_is_trimmed_and_deduplicated() -> None:
    args = parse_args(["--orcamento", "6", "--modelos", " gemini-a, ,gemini-b,gemini-a "])
    assert args.modelos == ("gemini-a", "gemini-b")
    with pytest.raises(SystemExit):
        parse_args(["--orcamento", "6", "--modelos", " , "])


def test_worst_case_per_check_counts_the_schema_retry_and_skips_disabled_models() -> None:
    # Uma tentativa por modelo, e a cadeia pode rodar 2 vezes (resposta fora do schema).
    assert pior_caso_gemini(["a"]) == 2
    assert pior_caso_gemini(["a", "b", "gemma"], {"gemma": 0}) == 4


# ---------- robustez da execução ----------


async def test_empty_response_without_status_is_not_a_transient_failure() -> None:
    # Status vazio sem ser o timeout nosso (ex.: resposta vazia): não é "passageiro" e não
    # conta para as 2 falhas seguidas; aparece como sem_status, não como timeout.
    analyzer = ScriptedAnalyzer(
        [
            ("erro", None, "m1", "resposta vazia (MAX_TOKENS)"),
            ("erro", None, "m1", "resposta vazia (MAX_TOKENS)"),
            ("ok", "falso", "m1"),
        ]
    )
    casos = [caso(c) for c in "abc"]
    execucao, _ = await run(casos, analyzer)
    assert execucao.parada is None
    assert len(execucao.resultados) == 3
    assert metricas(casos, execucao.resultados)["falhas_ia_por_modelo"] == {"m1": {"sem_status": 2}}


async def test_results_already_paid_for_survive_an_exception() -> None:
    class Broken(ScriptedAnalyzer):
        async def analyze(self, data: Any, *args: Any, **kwargs: Any) -> Any:
            if self.calls == 1:
                raise RuntimeError("SDK quebrou")
            return await super().analyze(data, *args, **kwargs)

    execucao = Execucao()
    with pytest.raises(RuntimeError):
        await run([caso("a"), caso("b")], Broken([("ok", "falso", "m1")]), execucao=execucao)
    assert [r.caso_id for r in execucao.resultados] == ["a"]
    assert execucao.chamadas_gemini == 2  # o caso que quebrou também conta (na dúvida, conta)


async def test_fact_check_queries_are_counted_and_capped() -> None:
    analyzer = ScriptedAnalyzer([("ok", "falso", "m1")] * 5)
    factcheck = FakeFactCheck()
    execucao, _ = await run(
        [caso(c) for c in "abcde"], analyzer, factcheck=factcheck, orcamento_factcheck=4
    )
    sent = sum(len(q) for q in factcheck.queries)
    assert execucao.consultas_factcheck == sent > 0
    assert execucao.parada == "orçamento da Fact Check"
    assert execucao.consultas_factcheck <= 4


async def test_a_second_report_on_the_same_day_does_not_overwrite_the_first(
    tmp_path: Path,
) -> None:
    analyzer = ScriptedAnalyzer([("ok", "falso", "m1")])
    casos = [caso("a")]
    execucao, _ = await run(casos, analyzer)
    first = gravar_relatorio(
        casos, execucao, pasta=tmp_path, commit="abc1234", dia="2026-10-27", orcamento=10
    )
    second = gravar_relatorio(
        casos, execucao, pasta=tmp_path, commit="abc1234", dia="2026-10-27", orcamento=10
    )
    assert first[0].name == "2026-10-27_abc1234.md"
    assert second[0].name == "2026-10-27_abc1234-2.md"
    assert second[1].name == "2026-10-27_abc1234-2.json"


# ---------- ajustes de 26/10: outro assunto, só IA, consumo, subconjunto, README ----------

OFF_TOPIC_TRUE = {"titulo": "Vacina da dengue foi aprovada pela Anvisa", "veredito": "Verdadeiro"}
ON_TOPIC_TRUE = {
    "titulo": "Eleição não foi adiada para novembro, confirma o TSE",
    "veredito": "Verdadeiro",
}
OFF_TOPIC_FALSE = {"titulo": "Vacina da dengue causa cegueira", "veredito": "Falso"}
KEYS = ["eleicao", "adiada", "novembro"]


@pytest.mark.parametrize(
    ("veredito", "confianca", "citadas", "esperado"),
    [
        # "Verdadeiro" só de pé por causa da checagem de outro assunto: viola o #43.
        ("verdadeiro", 90, [OFF_TOPIC_TRUE], True),
        # Com a do mesmo assunto confirmando, a de outro assunto não é o que sustenta.
        ("verdadeiro", 90, [ON_TOPIC_TRUE, OFF_TOPIC_TRUE], False),
        # "Falso" < 70% só com checagem de outro assunto: viola.
        ("falso", 60, [OFF_TOPIC_FALSE], True),
        # "Falso" >= 70% não depende de checagem: não viola.
        ("falso", 90, [OFF_TOPIC_FALSE], False),
        ("nao_verificavel", 0, [OFF_TOPIC_TRUE], False),
        ("verdadeiro", 90, [], False),
    ],
)
def test_off_topic_check_sustaining_the_verdict_uses_the_43_rule(
    veredito: str, confianca: int, citadas: list[dict[str, str]], esperado: bool
) -> None:
    assert outro_assunto_sustenta(veredito, confianca, citadas, KEYS) is esperado


def test_off_topic_sustaining_is_a_red_line() -> None:
    c = caso("b")
    r = Resultado(
        **{**resultado(c, "falso").__dict__, "outro_assunto_sustenta": True},
    )
    assert linhas_vermelhas(c, r) == ["agencia_de_outro_assunto_sustentando"]


async def test_the_platform_does_not_let_an_off_topic_check_sustain_true() -> None:
    # De ponta a ponta: a IA diz "verdadeiro" citando checagem de outro assunto; a salvaguarda
    # da plataforma rebaixa, e a avaliação não acusa linha vermelha (deve ser 0).
    factcheck = FakeFactCheck()
    factcheck.results = [
        AgencyCheck(
            agencia="Lupa",
            titulo="Vacina da dengue foi aprovada pela Anvisa",
            veredito="Verdadeiro",
            url="https://lupa.uol.com.br/x",
        )
    ]
    analyzer = ScriptedAnalyzer([("ok", "verdadeiro", "m1")])
    execucao, _ = await run([caso("a", esperado="nao_verificavel")], analyzer, factcheck=factcheck)
    (r,) = execucao.resultados
    assert r.obtido == "nao_verificavel"
    assert r.outro_assunto_sustenta is False
    assert metricas([caso("a", esperado="nao_verificavel")], [r])["linhas_vermelhas"] == []


def test_ai_only_verdict_is_a_metric_with_the_list_of_cases() -> None:
    casos = [caso("a"), caso("b"), caso("c", esperado="nao_verificavel")]
    res = [
        resultado(casos[0], "falso"),  # sem agência citada: só IA
        resultado(casos[1], "falso", citadas=("https://agencia.org/1",)),
        resultado(casos[2], "nao_verificavel"),  # não verificável não é veredito da IA
    ]
    m = metricas(casos, res)
    assert m["veredito_so_da_ia"] == {"total": 1, "casos": ["a"]}
    assert m["linhas_vermelhas"] == []  # métrica, não linha vermelha


async def test_report_shows_model_gemini_calls_and_fact_check_queries_separately(
    tmp_path: Path,
) -> None:
    analyzer = ScriptedAnalyzer([("ok", "falso", "gemini-3.7-flash")])
    casos = [caso("a")]
    execucao, _ = await run(casos, analyzer)
    md, js = gravar_relatorio(
        casos,
        execucao,
        pasta=tmp_path,
        commit="abc1234",
        dia="2026-10-27",
        orcamento=30,
        modelos=("gemini-3.7-flash",),
    )
    consumo = json.loads(js.read_text(encoding="utf-8"))["consumo"]
    assert consumo == {
        "modelos_configurados": ["gemini-3.7-flash"],
        "modelo_que_respondeu": {"gemini-3.7-flash": 1},
        "chamadas_gemini": 1,
        "orcamento_gemini": 30,
        "consultas_factcheck": execucao.consultas_factcheck,
    }
    text = md.read_text(encoding="utf-8")
    assert "- Modelo usado: gemini-3.7-flash" in text
    assert "- Chamadas ao Gemini gastas: 1 de 30" in text
    assert f"- Consultas à Fact Check gastas: {execucao.consultas_factcheck}" in text
    assert "Veredito só da IA, sem agência citada" in text


def test_subset_by_type_and_id() -> None:
    casos = [caso("a"), caso("b", tipo="link_agencia", entrada=ESTADAO), caso("c")]
    assert [c.id for c in filtrar(casos, tipos=["link_agencia"])] == ["b"]
    assert [c.id for c in filtrar(casos, ids=["c", "a"])] == ["a", "c"]  # ordem do arquivo
    assert [c.id for c in filtrar(casos, tipos=["boato_checado"], ids=["a", "b"])] == ["a"]
    assert filtrar(casos) == casos
    with pytest.raises(ValueError, match="tipo desconhecido"):
        filtrar(casos, tipos=["boato"])
    with pytest.raises(ValueError, match="id desconhecido"):
        filtrar(casos, ids=["zzz"])


def test_subset_arguments() -> None:
    args = parse_args(["--orcamento", "30", "--tipos", "boato_checado, link_agencia", "--ids", "a"])
    assert args.tipos == ("boato_checado", "link_agencia")
    assert args.ids == ("a",)
    assert parse_args(["--orcamento", "30"]).tipos == ()


NOITES = (
    ("boato_checado",),
    ("link_agencia", "fato_verdadeiro"),
    ("noticia_legitima", "boato_sem_checagem"),
)


def test_the_repository_cases_split_into_three_nights_of_10() -> None:
    casos = carregar_casos(CASOS_PADRAO)
    nights = [filtrar(casos, tipos=tipos) for tipos in NOITES]
    assert [len(n) for n in nights] == [10, 10, 10]
    ids = [c.id for n in nights for c in n]
    assert sorted(ids) == sorted(c.id for c in casos)  # as 3 noites cobrem tudo, sem repetir


def test_readme_lists_the_exact_vocabulary_the_plan_and_the_commands() -> None:
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    for value in sorted(ESPERADOS):
        assert f"`{value}`" in readme
    assert "CHECAFATOS_EVAL_REAL=1" in readme
    assert "entre 00:00 e 02:30 de Boa Vista" in readme
    for tipos in NOITES:
        command = (
            "python -m eval.rodar_avaliacao --modelos gemini-3.7-flash --orcamento 18 "
            f"--tipos {','.join(tipos)}"
        )
        assert command in readme
    assert "--orcamento 30" not in readme  # o plano antigo (2 noites de 15) saiu


# ---------- cota de hoje (mesma fonte do painel) ----------


def test_quota_check_passes_when_what_is_left_covers_the_budget() -> None:
    rpd = {"gemini-3.7-flash": 20}
    assert conferir_cota(["gemini-3.7-flash"], {"gemini-3.7-flash": 2}, rpd, 18) == 18
    assert conferir_cota(["gemini-3.7-flash"], {}, rpd, 18) == 20


def test_quota_check_aborts_when_what_is_left_is_below_the_budget() -> None:
    rpd = {"gemini-3.7-flash": 20}
    with pytest.raises(CotaInsuficienteError, match=r"restam 17 .*3 de 20 usadas.*orçamento é 18"):
        conferir_cota(["gemini-3.7-flash"], {"gemini-3.7-flash": 3}, rpd, 18)
    with pytest.raises(CotaInsuficienteError, match="restam 0"):
        conferir_cota(["gemini-3.7-flash"], {"gemini-3.7-flash": 25}, rpd, 1)  # nunca negativo


def test_quota_check_treats_an_unknown_model_as_without_quota() -> None:
    with pytest.raises(CotaInsuficienteError):
        conferir_cota(["modelo-novo"], {}, {"gemini-3.7-flash": 20}, 1)


def test_quota_check_sums_the_chain() -> None:
    rpd = {"a": 20, "b": 20}
    assert conferir_cota(["a", "b"], {"a": 15, "b": 5}, rpd, 18) == 20
