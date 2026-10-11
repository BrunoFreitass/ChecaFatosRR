"""Política (10/10/2026): origem confirmada não é fato verificado.

Teste em produção: a entrevista de Douglas Ruas no g1 saiu "Verdadeiro 95%" sem nenhuma
checagem de agência. "Verdadeiro" exige ao menos uma checagem de agência, citada pelo modelo,
que corrobore o conteúdo; sem ela vira "nao_verificavel" ("Ainda sem checagem"), com o resumo
mantido e, num link, a nota neutra de quem publicou. Aplicado no pós-processamento: não
depende só do prompt.
"""

from dataclasses import replace
from datetime import date

import pytest

from app.services.factcheck import AgencyCheck
from app.services.gemini import (
    SYSTEM_INSTRUCTION,
    AnalysisInput,
    GeminiAgencyCheck,
    GeminiVerdict,
    finalize,
)

TODAY = date(2026, 10, 10)
RESUMO = "Reportagem do g1 sobre a primeira entrevista de Douglas Ruas como governador eleito."
AGENCY = AgencyCheck(
    agencia="Aos Fatos",
    titulo="Ruas foi eleito no 1º turno",
    veredito="Verdadeiro",
    url="https://www.aosfatos.org/noticias/ruas-eleito/",
)


def link(*checks: AgencyCheck) -> AnalysisInput:
    return AnalysisInput(
        kind="link",
        content="Douglas Ruas afirmou ao RJ2 que a segurança pública será prioridade.",
        title="Ruas diz que segurança pública será prioridade de seu governo | G1",
        url="https://g1.globo.com/rj/noticia/ruas.ghtml",
        agency_checks=list(checks),
        content_urls=[],
        source_domain="g1.globo.com",
    )


def text(*checks: AgencyCheck) -> AnalysisInput:
    return AnalysisInput(
        kind="texto",
        content="Douglas Ruas foi eleito governador do Rio no 1º turno.",
        title=None,
        url=None,
        agency_checks=list(checks),
        content_urls=[],
    )


def verdict(veredito: str = "verdadeiro", confianca: int = 95, *cited: str) -> GeminiVerdict:
    return GeminiVerdict(
        veredito=veredito,  # type: ignore[arg-type]
        resumo=RESUMO,
        confianca=confianca,
        checagens_agencias=[GeminiAgencyCheck(url=url) for url in cited],
    )


# ---------- verdadeiro sem agência ----------


def test_true_without_agency_on_a_link_is_awaiting_check_with_the_origin_note() -> None:
    result = finalize(verdict(), link(), TODAY)

    assert result.veredito == "nao_verificavel"
    assert result.veredito_ia == "verdadeiro"  # o que o modelo disse fica registrado
    assert result.rebaixado_por_confianca is False  # não foi falta de confiança
    assert result.resumo.startswith(RESUMO)  # resumo mantido
    assert result.resumo.endswith("Publicado por g1.globo.com (origem confirmada).")


def test_true_without_agency_on_pasted_text_has_no_origin_note() -> None:
    result = finalize(verdict(), text(), TODAY)

    assert result.veredito == "nao_verificavel"
    assert result.resumo == RESUMO  # texto colado: a autoria não é confirmada


def test_link_without_confirmed_origin_has_no_note() -> None:
    result = finalize(verdict(), replace(link(), source_domain=None), TODAY)

    assert result.veredito == "nao_verificavel"
    assert "origem confirmada" not in result.resumo


def test_origin_note_is_not_repeated() -> None:
    note = "Publicado por g1.globo.com (origem confirmada)."
    answer = verdict().model_copy(update={"resumo": f"{RESUMO} {note}"})

    assert finalize(answer, link(), TODAY).resumo.count(note) == 1


def test_true_with_low_confidence_also_gets_the_origin_note() -> None:
    # Sem agência, o motivo é a política (não só a confiança): a nota aparece do mesmo jeito.
    result = finalize(verdict("verdadeiro", 60), link(), TODAY)

    assert result.veredito == "nao_verificavel"
    assert result.rebaixado_por_confianca is True
    assert result.resumo.endswith("Publicado por g1.globo.com (origem confirmada).")


# ---------- verdadeiro com agência ----------


def test_true_corroborated_by_a_cited_agency_check_stays_true() -> None:
    result = finalize(verdict("verdadeiro", 95, AGENCY.url), link(AGENCY), TODAY)

    assert result.veredito == "verdadeiro"
    assert result.resumo == RESUMO  # sem nota: a agência é a evidência


@pytest.mark.parametrize(
    "rating",
    [
        "Verdadeiro",
        "verdadeira",
        "Correto",
        "#FATO",
        "Fato",
        "True",
        "Verdade",
        "Verídico",
        " #Fato. ",
    ],
)
def test_agency_ratings_that_corroborate(rating: str) -> None:
    check = replace(AGENCY, veredito=rating)
    result = finalize(verdict("verdadeiro", 95, check.url), link(check), TODAY)
    assert result.veredito == "verdadeiro"


@pytest.mark.parametrize(
    "rating",
    [
        "Falso",
        "Não é verdade",
        "#FAKE",
        "Enganoso",
        "Verdadeiro, mas fora de contexto",
        "Impreciso",
        # Lista fechada: ressalvas e negações em inglês também não confirmam (revisão 10/10).
        "Not true",
        "Partly true",
        "Half true",
        "Mostly true",
        "Verdadeiro em parte",
        "Quase verdadeiro",
        "Fato ou Fake",
        "Não é bem assim",
        "Correto, mas exagerado",
        "Verificado",
        "",
    ],
)
def test_agency_ratings_that_do_not_corroborate(rating: str) -> None:
    check = replace(AGENCY, veredito=rating)
    result = finalize(verdict("verdadeiro", 95, check.url), link(check), TODAY)
    assert result.veredito == "nao_verificavel"


def test_agency_check_the_model_did_not_cite_does_not_corroborate() -> None:
    # A busca pode trazer checagens de OUTRA afirmação; só a citada vale como evidência.
    result = finalize(verdict("verdadeiro", 95), link(AGENCY), TODAY)
    assert result.veredito == "nao_verificavel"


def test_citation_of_a_url_outside_the_api_results_does_not_corroborate() -> None:
    invented = "https://www.aosfatos.org/noticias/inventada/"
    result = finalize(verdict("verdadeiro", 95, invented), link(), TODAY)
    assert result.veredito == "nao_verificavel"


# ---------- os outros vereditos seguem as regras atuais ----------


def test_false_without_agency_keeps_the_confidence_rule() -> None:
    assert finalize(verdict("falso", 75), text(), TODAY).veredito == "falso"
    low = finalize(verdict("falso", 65), text(), TODAY)
    assert low.veredito == "nao_verificavel"
    assert low.rebaixado_por_confianca is True


@pytest.mark.parametrize("veredito", ["enganoso", "fora_de_contexto"])
def test_other_verdicts_are_unchanged(veredito: str) -> None:
    result = finalize(verdict(veredito, 80), link(), TODAY)
    assert result.veredito == veredito
    assert result.resumo == RESUMO


# ---------- prompt ----------


def test_prompt_says_confirmed_origin_is_not_evidence_of_truth() -> None:
    lowered = SYSTEM_INSTRUCTION.lower()
    assert "origem confirmada não é evidência" in lowered
    assert "verdadeiro" in lowered and "checagem de agência" in lowered
