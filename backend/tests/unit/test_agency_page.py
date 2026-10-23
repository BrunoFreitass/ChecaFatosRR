"""Link que é a própria checagem de uma agência (23/10/2026, caso real do Estadão Verifica).

O título da matéria ("É falso que ...") não é a alegação: tratado como alegação, a IA via
contradição com o rating e a página dizia "não verificável".
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from app.repositories.checks import Limits, StoredCheck
from app.routers.checks import to_out
from app.services.check_service import CheckSettings
from app.services.factcheck import parse_claims, same_page
from app.services.verdict_policy import NO_EXACT_CHECK_SENTENCE, neutral_summary

ESTADAO = "https://www.estadao.com.br/estadao-verifica/frude-transmissao-alteracao-votos-leicoes/"
SETTINGS = CheckSettings(
    limits=Limits(cache_hours=24, checks_per_hour=10, lookups_per_hour=60, prompt_version="v"),
    daily_gemini_checks=200,
    model="gemini-teste",
)
REVIEW = {
    "agencia": "Estadão",
    "titulo": "É falso que transmissão de dados permitiu alterar votos",
    "veredito": "Falso",
    "url": ESTADAO,
    "data": "2026-10-22",
}


@pytest.mark.parametrize(
    "variant",
    [
        ESTADAO,
        ESTADAO.rstrip("/"),
        ESTADAO.replace("https://www.", "https://"),
        ESTADAO.replace("https://", "http://"),
        ESTADAO + "?utm_source=whatsapp&utm_medium=social",
        ESTADAO + "#comentarios",
        ESTADAO.replace("www.estadao", "WWW.Estadao") + "?utm_campaign=x#topo",
    ],
)
def test_same_page_ignores_www_trailing_slash_utm_and_fragment(variant: str) -> None:
    assert same_page(variant, ESTADAO)
    assert same_page(ESTADAO, variant)


@pytest.mark.parametrize(
    "other",
    [
        "https://www.estadao.com.br/estadao-verifica/outra-checagem/",
        "https://www.estadao.com.br/estadao-verifica/frude-transmissao-alteracao-votos-leicoes/?id=2",
        "https://evil.example/estadao-verifica/frude-transmissao-alteracao-votos-leicoes/",
        "https://www.estadao.com.br/",
    ],
)
def test_different_pages_are_not_the_same(other: str) -> None:
    assert not same_page(other, ESTADAO)


def test_same_page_never_matches_empty_or_invalid() -> None:
    assert not same_page(None, ESTADAO)
    assert not same_page("", "")
    assert not same_page("nao e url", "nao e url")


def test_parse_claims_keeps_the_claim_reviewed() -> None:
    payload = {
        "claims": [
            {
                "text": "Transmissão dos dados permitiu alterar votos",
                "claimReview": [
                    {
                        "publisher": {"name": "Estadão"},
                        "url": ESTADAO,
                        "title": REVIEW["titulo"],
                        "textualRating": "Falso",
                    }
                ],
            },
            {
                "claimReview": [
                    {
                        "publisher": {"name": "Lupa"},
                        "url": "https://lupa.uol.com.br/x",
                        "title": "Sem alegação",
                        "textualRating": "Falso",
                    }
                ]
            },
        ]
    }
    first, second = parse_claims(payload)
    assert first.alegacao == "Transmissão dos dados permitiu alterar votos"
    assert first.titulo == REVIEW["titulo"]
    assert second.alegacao is None
    # A forma gravada nas checagens com IA não muda (nem o que vai ao prompt).
    assert "alegacao" not in first.as_json()


def stored(
    result: dict[str, Any], *, url: str = ESTADAO, veredito: str = "nao_verificavel"
) -> StoredCheck:
    return StoredCheck(
        public_id="k7mqp9xwvrzt",
        kind="link",
        url=url,
        domain="estadao.com.br",
        title="É falso que transmissão de dados permitiu alterar votos | Estadão",
        published_at=None,
        texto=None,
        veredito=veredito,
        confianca=60,
        result={"resumo": "Resumo da IA.", **result},
        created_at=datetime.now(UTC),
        prompt_version="v",
    )


def test_agency_page_check_is_shown_as_the_agency_check() -> None:
    check = stored(
        {
            "modo": "checagem_de_agencia",
            "checagem_de_agencia": {**REVIEW, "alegacao": "Transmissão permitiu alterar votos"},
        }
    )
    out = to_out(check, SETTINGS)
    assert out.modo == "checagem_de_agencia"
    page = out.checagem_de_agencia
    assert page is not None
    assert (page.agencia, page.url, page.classificacao) == ("Estadão", ESTADAO, "Falso")
    assert page.alegacao == "Transmissão permitiu alterar votos"
    assert page.titulo == REVIEW["titulo"]
    assert out.checagens_agencias == []
    assert out.pontos_suspeitos == []


def test_old_buggy_ai_check_of_the_agency_page_is_fixed_on_read() -> None:
    # Como ficou gravada em 22/10: IA chamada, "não verificável", a própria checagem citada.
    check = stored(
        {
            "checagens_agencias": [REVIEW],
            "pontos_suspeitos": ["Contradição entre título e classificação"],
            "palavras_chave": ["transmissão", "votos"],
        },
        url=ESTADAO.rstrip("/"),
    )
    out = to_out(check, SETTINGS)
    assert out.modo == "checagem_de_agencia"
    assert out.checagem_de_agencia is not None
    assert out.checagem_de_agencia.alegacao is None  # sem claimReviewed gravado: só o título
    assert out.checagem_de_agencia.titulo == REVIEW["titulo"]
    assert out.pontos_suspeitos == []


def test_ai_check_of_another_page_is_unchanged() -> None:
    check = stored(
        {"checagens_agencias": [REVIEW], "palavras_chave": ["transmissão", "votos"]},
        url="https://g1.globo.com/politica/eleicoes/noticia.ghtml",
        veredito="falso",
    )
    out = to_out(check, SETTINGS)
    assert out.modo == "ia"
    assert out.checagem_de_agencia is None


def test_not_verifiable_never_says_the_agencies_do_not_confirm() -> None:
    assert NO_EXACT_CHECK_SENTENCE == (
        "Não encontramos checagem que trate exatamente deste conteúdo. "
        "Veja as checagens relacionadas e as fontes oficiais."
    )
    text = neutral_summary(
        "link", "g1.globo.com", "Título", confirmed_origin=True, agency_checks=True
    )
    assert text.endswith(NO_EXACT_CHECK_SENTENCE)
    assert "não confirmam" not in text


def test_old_not_verifiable_summary_is_rewritten_on_read() -> None:
    old = (
        "Conteúdo publicado em g1.globo.com sobre: X. "
        "As checagens de agências encontradas não confirmam o conteúdo."
    )
    check = stored(
        {"resumo": old, "checagens_agencias": [], "palavras_chave": ["x"]},
        url="https://g1.globo.com/a.ghtml",
    )
    out = to_out(check, SETTINGS)
    assert "não confirmam" not in out.resumo
    assert out.resumo.endswith(NO_EXACT_CHECK_SENTENCE)
