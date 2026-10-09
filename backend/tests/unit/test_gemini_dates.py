"""Datas no prompt e na pós-validação.

Bug do teste manual de 09/10/2026: notícia real do g1 do mesmo dia virou "Não verificável"
porque o modelo, sem saber a data atual, tratou outubro de 2026 como "fatos futuros".
"""

from datetime import UTC, date, datetime

import pytest

from app.services.gemini import (
    BOA_VISTA,
    SYSTEM_INSTRUCTION,
    AnalysisInput,
    GeminiAnalyzer,
    GeminiVerdict,
    build_prompt,
    finalize,
)

pytestmark = pytest.mark.anyio

# 03:30 UTC de 09/10 = 23:30 de 08/10 em Boa Vista (UTC-4): o "hoje" é o de Roraima.
NOW = datetime(2026, 10, 9, 3, 30, tzinfo=UTC)
TODAY_RR = date(2026, 10, 8)


def data(*, published_at: date | None = None, kind: str = "link") -> AnalysisInput:
    return AnalysisInput(
        kind=kind,  # type: ignore[arg-type]
        content="TSE divulga calendário do 2º turno em Roraima.",
        title="Calendário do 2º turno",
        url="https://g1.globo.com/rr/noticia/2026/10/08/calendario.ghtml",
        agency_checks=[],
        content_urls=[],
        published_at=published_at,
    )


def verdict(*points: str, veredito: str = "verdadeiro") -> GeminiVerdict:
    return GeminiVerdict(
        veredito=veredito,  # type: ignore[arg-type]
        resumo="Resumo.",
        pontos_suspeitos=list(points),
        contexto_faltante=None,
        checagens_agencias=[],
        fontes_sugeridas=[],
        confianca=80,
    )


# ---------- prompt ----------


def test_prompt_has_current_date_and_time_in_boa_vista() -> None:
    prompt = build_prompt(data(), now=NOW)
    assert "08/10/2026" in prompt
    assert "23:30" in prompt
    assert "Boa Vista" in prompt


def test_current_date_is_outside_the_untrusted_block() -> None:
    prompt = build_prompt(data(), now=NOW)
    date_line = prompt.index("08/10/2026")
    block = prompt.index("<conteudo-")
    assert date_line < block  # o conteúdo não consegue forjar o "hoje"


def test_publication_date_goes_inside_the_block() -> None:
    prompt = build_prompt(data(published_at=date(2026, 10, 8)), now=NOW)
    block = prompt.index("<conteudo-")
    assert prompt.index("Publicado em: 08/10/2026") > block


def test_naive_now_is_rejected() -> None:
    # Sem fuso, astimezone usaria o fuso da máquina: o "hoje" poderia virar o de outro lugar.
    with pytest.raises(ValueError):
        build_prompt(data(), now=datetime(2026, 10, 9, 3, 30))


def test_prompt_uses_the_real_clock_by_default() -> None:
    year = str(datetime.now(BOA_VISTA).year)  # o ano de Roraima, não o de UTC (virada de ano)
    assert "Hoje é" in build_prompt(data())
    assert year in build_prompt(data())


@pytest.mark.parametrize(
    "rule",
    [
        # a data de corte do modelo não torna um fato suspeito
        "data de corte",
        # desconhecer um evento não é evidência de falsidade
        "não é evidência de falsidade",
        # publicação recente nunca é ponto suspeito por si só
        "publicação recente",
    ],
)
def test_system_instruction_has_date_rules(rule: str) -> None:
    assert rule in SYSTEM_INSTRUCTION.lower()


# ---------- pós-validação ----------


@pytest.mark.parametrize(
    "point",
    [
        "Acontecimentos datados de outubro de 2026, projetando fatos futuros",
        "Data futura em relação ao conhecimento disponível",
        "Fatos posteriores à data de corte do modelo",
        "Datas no FUTURO (08/10/2026)",
        "Notícia datada de 8 de outubro de 2026, ou seja, no futuro",
        "O evento de 08/10/2026 ainda não ocorreu",
        "A votação de 7 de outubro de 2026 ainda não aconteceu",
    ],
)
def test_future_points_are_dropped_for_content_dated_up_to_today(point: str) -> None:
    result = finalize(
        verdict(point, "Sem fonte oficial"), data(published_at=TODAY_RR), today=TODAY_RR
    )
    assert result.pontos_suspeitos == ["Sem fonte oficial"]


def test_future_points_are_dropped_for_pasted_text_without_date() -> None:
    # Texto colado agora existe agora: "fato futuro" sobre datas até hoje é erro do modelo.
    result = finalize(
        verdict("Cita a votação de outubro de 2026 como fato futuro"),
        data(kind="texto"),
        today=TODAY_RR,
    )
    assert result.pontos_suspeitos == []


@pytest.mark.parametrize(
    "point",
    [
        "A mensagem dá como certa a posse de 01/01/2027, que ainda não ocorreu",
        "Afirma resultado da eleição de 2030, evento futuro",
        "Relata votação de novembro de 2026, que ainda não aconteceu",
        # O caso central antes de 25/10: resultado do 2º turno dado como certo.
        "O resultado do 2º turno (25 de outubro de 2026), que ainda não ocorreu, é dado como certo",
        "O resultado do 2º turno ainda não ocorreu e é apresentado como definitivo",
        "Diz que a apuração de 25/10 já terminou, mas ela ainda não ocorreu",
        "Fala da apuração de 25/10/26 como fato futuro já encerrado",
        # "futuro" com outro sentido não é o erro de data do modelo.
        "Promete futuras obras sem citar fonte nem orçamento",
        "Fala do futuro governo sem apresentar fonte",
        "Diz que futuramente o voto será pela internet",
        # Achados do /code-review: avisos legítimos que não podem sumir.
        "Afirma resultado do 2º turno de 2026, evento futuro",
        "O 2º turno de outubro de 2026 ainda não ocorreu, mas a mensagem traz o resultado",
        "Menciona um evento futuro como se já tivesse acontecido",
        "Cita data futura: posse em 1° de novembro",
        "A apuração ainda não ocorreu, mas diz que 1/3 das urnas já foram contadas",
        "Anuncia eventos futuros em novembro sem fonte",
    ],
)
def test_future_points_about_dates_after_today_are_kept(point: str) -> None:
    result = finalize(verdict(point), data(published_at=TODAY_RR), today=TODAY_RR)
    assert result.pontos_suspeitos == [point]


def test_publication_one_day_ahead_still_filters_and_is_left_out_of_the_prompt() -> None:
    # Fuso ou metadado: a página diz 09/10 e em Roraima ainda é 08/10.
    tomorrow = date(2026, 10, 9)
    result = finalize(
        verdict("Fatos futuros: outubro de 2026"), data(published_at=tomorrow), today=TODAY_RR
    )
    assert result.pontos_suspeitos == []
    # O modelo não recebe uma "publicação" depois do "hoje" (convite a chamar de futuro).
    assert "Publicado em" not in build_prompt(data(published_at=tomorrow), now=NOW)


def test_points_without_future_claims_are_untouched() -> None:
    points = ("Sem autor identificado", "Usa tom alarmista")
    result = finalize(verdict(*points), data(published_at=TODAY_RR), today=TODAY_RR)
    assert result.pontos_suspeitos == list(points)


# ---------- analisador ----------


async def test_analyzer_sends_today_and_cleans_the_answer() -> None:
    sent: list[str] = []

    async def generate(prompt: str) -> str:
        sent.append(prompt)
        return verdict("Fatos futuros: outubro de 2026", "Sem fonte").model_dump_json()

    analyzer = GeminiAnalyzer(generate, now=lambda: NOW)
    result = await analyzer.analyze(data(published_at=TODAY_RR))

    assert "08/10/2026" in sent[0]
    assert result.pontos_suspeitos == ["Sem fonte"]
