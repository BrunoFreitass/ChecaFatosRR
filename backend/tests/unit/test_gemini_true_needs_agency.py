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


# ---------- verdadeiro sem agência (decisões de 11/10/2026) ----------
# O resumo do modelo afirmava "reportagem legítima": no lugar dele vai um texto neutro.

NEUTRAL_G1 = (
    "Conteúdo publicado em g1.globo.com (origem confirmada) sobre: Ruas diz que segurança "
    "pública será prioridade de seu governo. Não encontramos checagens de agências sobre ele."
)


def test_true_without_agency_on_a_link_gets_the_neutral_text() -> None:
    result = finalize(verdict(), link(), TODAY)

    assert result.veredito == "nao_verificavel"
    assert result.veredito_ia == "verdadeiro"  # o que o modelo disse fica registrado
    assert result.rebaixado_por_confianca is False  # não foi falta de confiança
    assert result.rebaixado_sem_agencia is True
    assert result.resumo == NEUTRAL_G1  # sem o resumo do modelo (nem o " | G1" do título)
    assert result.to_json()["rebaixado_sem_agencia"] is True


def test_true_without_agency_on_pasted_text_gets_the_neutral_text() -> None:
    result = finalize(verdict(), text(), TODAY)

    assert result.veredito == "nao_verificavel"
    assert result.resumo == "Não encontramos checagens de agências sobre este conteúdo."


def test_open_platform_link_is_not_called_confirmed_origin() -> None:
    blog = replace(link(), source_domain="fulano.blogspot.com", open_platform=True)

    result = finalize(verdict(), blog, TODAY)

    assert result.resumo.startswith("Conteúdo publicado em fulano.blogspot.com sobre: ")
    assert "origem confirmada" not in result.resumo


def test_link_without_title_or_confirmed_origin() -> None:
    no_title = replace(link(), title=None)
    assert finalize(verdict(), no_title, TODAY).resumo == (
        "Conteúdo publicado em g1.globo.com (origem confirmada). "
        "Não encontramos checagens de agências sobre ele."
    )
    unknown = replace(link(), source_domain=None)
    assert "origem confirmada" not in finalize(verdict(), unknown, TODAY).resumo


def test_true_with_low_confidence_also_gets_the_neutral_text() -> None:
    result = finalize(verdict("verdadeiro", 60), link(), TODAY)

    assert result.veredito == "nao_verificavel"
    assert result.rebaixado_por_confianca is True
    assert result.rebaixado_sem_agencia is True
    assert result.resumo == NEUTRAL_G1


def test_true_citing_a_non_confirming_agency_says_the_checks_do_not_confirm() -> None:
    misleading = replace(AGENCY, veredito="Enganoso")
    result = finalize(verdict("verdadeiro", 95, misleading.url), link(misleading), TODAY)

    assert result.veredito == "nao_verificavel"
    assert result.resumo.endswith("As checagens de agências encontradas não confirmam o conteúdo.")


def test_finalize_records_the_confirmed_origin_for_later_reads() -> None:
    assert finalize(verdict(), link(), TODAY).to_json()["origem_confirmada"] == "g1.globo.com"
    blog = replace(link(), source_domain="fulano.blogspot.com", open_platform=True)
    assert finalize(verdict(), blog, TODAY).to_json()["origem_confirmada"] is None
    assert finalize(verdict(), text(), TODAY).to_json()["origem_confirmada"] is None


def test_model_answers_that_were_not_downgraded_keep_their_summary() -> None:
    result = finalize(verdict("nao_verificavel", 50), link(), TODAY)
    assert result.resumo == RESUMO
    assert result.rebaixado_sem_agencia is False


# ---------- checagens citadas x relacionadas (decisão 3) ----------

OTHER = AgencyCheck(
    agencia="Lupa",
    titulo="Outro assunto",
    veredito="Falso",
    url="https://lupa.uol.com.br/jornalismo/outro/",
)


def test_agency_checks_the_model_did_not_cite_are_related_not_evidence() -> None:
    result = finalize(verdict("verdadeiro", 95, AGENCY.url), link(AGENCY, OTHER), TODAY)

    assert [c.url for c in result.checagens_agencias] == [AGENCY.url]
    assert [c.url for c in result.checagens_relacionadas] == [OTHER.url]
    assert result.to_json()["checagens_relacionadas"][0]["url"] == OTHER.url


def test_only_uncited_checks_leave_the_answer_awaiting_check() -> None:
    result = finalize(verdict("nao_verificavel", 50), link(OTHER), TODAY)
    assert result.checagens_agencias == []
    assert result.checagens_relacionadas == [OTHER]


# ---------- verdadeiro com agência ----------


def test_true_corroborated_by_a_cited_agency_check_stays_true() -> None:
    result = finalize(verdict("verdadeiro", 95, AGENCY.url), link(AGENCY), TODAY)

    assert result.veredito == "verdadeiro"
    assert result.resumo == RESUMO  # a agência é a evidência: resumo do modelo mantido
    assert result.rebaixado_sem_agencia is False


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
    assert result.rebaixado_sem_agencia is False


# ---------- prompt ----------


def test_prompt_says_confirmed_origin_is_not_evidence_of_truth() -> None:
    lowered = SYSTEM_INSTRUCTION.lower()
    assert "origem confirmada não é evidência" in lowered
    assert "verdadeiro" in lowered and "checagem de agência" in lowered
