"""Regra de veredito com relevância (19/10/2026): "Verdadeiro" e "Falso" abaixo de 70% só com
checagem citada E do mesmo assunto; citada de outro assunto vai para "relacionadas". Vale ao
gravar e na leitura (checagens antigas), na página e em "Últimas". Prompt e finalize intocados."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from app.repositories.checks import AGENCIES_ONLY_VERSION, CheckSummary, Limits, StoredCheck
from app.routers.checks import _latest, to_out
from app.services.check_service import CheckSettings
from app.services.gemini import LOW_CONFIDENCE_WITHOUT_AGENCIES
from app.services.verdict_policy import (
    AGENCIES_ONLY_FOUND,
    AGENCIES_ONLY_NONE,
    FALSE_WITHOUT_AGENCY_MIN,
)

SETTINGS = CheckSettings(
    limits=Limits(cache_hours=24, checks_per_hour=10, lookups_per_hour=60, prompt_version="v"),
    daily_gemini_checks=200,
    model="gemini-teste",
)
TITLE = "Lula gastou R$ 7 bilhões em viagens pessoais com dinheiro público"
SAME = {
    "agencia": "Aos Fatos",
    "titulo": "Lula não gastou R$ 7 bilhões em viagens pessoais",
    "veredito": "falso",
    "url": "https://www.aosfatos.org/noticias/lula-7-bilhoes-viagens/",
    "data": "2026-10-05",
}
SAME_TRUE = {
    **SAME,
    "titulo": "É verdade que viagens custaram R$ 7 bilhões em gastos pessoais",
    "veredito": "Verdadeiro",
    "url": "https://exemplo.com.br/v",
}
OTHER = {
    "agencia": "AFP Checamos",
    "titulo": "Pausa na divulgação de resultados não comprova fraude eleitoral",
    "veredito": "Falso",
    "url": "https://checamos.afp.com/pausa",
    "data": "2026-10-08",
}
OTHER_TRUE = {**OTHER, "veredito": "Verdadeiro", "url": "https://checamos.afp.com/v"}


def stored(veredito: str, confianca: int, cited: list[dict[str, Any]], **extra: Any) -> StoredCheck:
    return StoredCheck(
        public_id="k7mqp9xwvrzt",
        kind="link",
        url="https://exemplo.com.br/noticia",
        domain="exemplo.com.br",
        title=TITLE,
        published_at=None,
        texto=None,
        veredito=veredito,
        confianca=confianca,
        result={"resumo": "Texto da IA.", "checagens_agencias": cited, **extra},
        created_at=datetime.now(UTC),
        prompt_version="v",
    )


def test_the_70_percent_rule_is_the_same_as_the_finalize_one() -> None:
    assert FALSE_WITHOUT_AGENCY_MIN == LOW_CONFIDENCE_WITHOUT_AGENCIES == 70


def test_old_false_40_to_69_backed_by_an_unrelated_check_becomes_awaiting() -> None:
    out = to_out(stored("falso", 55, [OTHER]), SETTINGS)
    assert out.veredito == "nao_verificavel"
    assert out.checagens_agencias == []  # "Ainda sem checagem" na página
    assert [c.url for c in out.checagens_relacionadas] == [OTHER["url"]]
    assert out.rebaixado_por_confianca is True
    # Texto neutro, não o resumo da IA que afirmava "falso".
    assert out.resumo != "Texto da IA."
    assert "Não encontramos checagens de agências" in out.resumo


def test_false_40_to_69_with_a_relevant_check_stays() -> None:
    out = to_out(stored("falso", 55, [SAME]), SETTINGS)
    assert out.veredito == "falso"
    assert [c.url for c in out.checagens_agencias] == [SAME["url"]]


def test_false_70_or_more_without_agency_stays_as_today() -> None:
    assert to_out(stored("falso", 70, []), SETTINGS).veredito == "falso"
    out = to_out(stored("falso", 85, [OTHER]), SETTINGS)
    assert out.veredito == "falso"
    assert out.checagens_agencias == []  # a de outro assunto não aparece como evidência
    assert [c.url for c in out.checagens_relacionadas] == [OTHER["url"]]


def test_true_backed_only_by_an_unrelated_confirming_check_becomes_neutral() -> None:
    out = to_out(stored("verdadeiro", 90, [OTHER_TRUE]), SETTINGS)
    assert out.veredito == "nao_verificavel"
    assert out.rebaixado_sem_agencia is True
    assert out.resumo != "Texto da IA."  # texto neutro, não o da IA
    assert out.checagens_agencias == []


def test_true_with_a_relevant_confirming_check_stays() -> None:
    out = to_out(stored("verdadeiro", 90, [SAME_TRUE]), SETTINGS)
    assert out.veredito == "verdadeiro"


def test_true_with_failed_search_and_unrelated_check_says_the_search_failed() -> None:
    out = to_out(stored("verdadeiro", 90, [OTHER_TRUE], busca_agencias="falhou"), SETTINGS)
    assert out.veredito == "nao_verificavel"
    assert "A busca nas agências de checagem falhou nesta análise." in out.resumo
    assert "encontramos" not in out.resumo.lower()


def test_saved_empty_keywords_are_not_replaced_by_the_title() -> None:
    # Lista vazia gravada = nada relevante naquele momento; a leitura não troca de critério.
    out = to_out(stored("falso", 55, [SAME], palavras_chave=[]), SETTINGS)
    assert out.veredito == "nao_verificavel"


def test_related_without_url_are_kept() -> None:
    no_url = {**OTHER, "url": ""}
    check = stored("falso", 85, [no_url], checagens_relacionadas=[{**OTHER, "url": ""}])
    out = to_out(check, SETTINGS)
    assert len(out.checagens_relacionadas) == 2


def test_old_check_with_no_keyword_at_all_is_left_as_is() -> None:
    # Antiga, título só de palavras ignoradas e sem palavras-chave gravadas: sem informação para
    # julgar o assunto, nada muda (só a lista vazia GRAVADA é "tudo de outro assunto").
    check = replace(stored("falso", 55, [OTHER]), title="Lula e Bolsonaro no TSE")
    out = to_out(check, SETTINGS)
    assert out.veredito == "falso"
    assert [c.url for c in out.checagens_agencias] == [OTHER["url"]]


def test_misleading_and_out_of_context_are_not_touched() -> None:
    for veredito in ("enganoso", "fora_de_contexto"):
        out = to_out(stored(veredito, 45, [OTHER]), SETTINGS)
        assert out.veredito == veredito
        assert out.checagens_agencias == []  # mas a de outro assunto não é evidência


def test_saved_keywords_win_over_the_title() -> None:
    # Ao gravar, as palavras-chave vêm do título + conteúdo; a leitura usa as gravadas.
    check = stored("falso", 55, [OTHER], palavras_chave=["pausa", "divulgacao", "fraude"])
    assert to_out(check, SETTINGS).veredito == "falso"


def test_agencies_only_moves_unrelated_checks_to_related() -> None:
    check = replace(
        stored("nao_verificavel", 0, [SAME, OTHER], modo="agencias", resumo=AGENCIES_ONLY_FOUND),
        prompt_version=AGENCIES_ONLY_VERSION,
    )
    out = to_out(check, SETTINGS)
    assert [c.url for c in out.checagens_agencias] == [SAME["url"]]
    assert [c.url for c in out.checagens_relacionadas] == [OTHER["url"]]


def test_agencies_only_with_only_unrelated_checks_does_not_say_they_are_about_it() -> None:
    check = replace(
        stored("nao_verificavel", 0, [OTHER], modo="agencias", resumo=AGENCIES_ONLY_FOUND),
        prompt_version=AGENCIES_ONLY_VERSION,
    )
    out = to_out(check, SETTINGS)
    assert out.checagens_agencias == []
    assert out.resumo == AGENCIES_ONLY_NONE


class _LatestRepo:
    def __init__(self, items: list[CheckSummary]) -> None:
        self.items = items

    async def latest(self, cursor: object, limit: int) -> list[CheckSummary]:
        return self.items


@pytest.mark.anyio
async def test_latest_list_shows_the_same_verdict_as_the_page() -> None:
    old = CheckSummary(
        public_id="k7mqp9xwvrzt",
        domain="exemplo.com.br",
        title=TITLE,
        veredito="falso",
        created_at=datetime.now(UTC),
        agency_checks=1,
        agency_ratings=("Falso",),
        confianca=55,
        agency_pairs=((OTHER["titulo"], "Falso"),),
    )
    good = replace(old, public_id="m3nq8r2st5vw", agency_pairs=((SAME["titulo"], "falso"),))
    out = await _latest(_LatestRepo([old, good]), None, 10)  # type: ignore[arg-type]
    assert [(i.veredito, i.total_agencias) for i in out.items] == [
        ("nao_verificavel", 0),
        ("falso", 1),
    ]
