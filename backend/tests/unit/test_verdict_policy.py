"""Regras de veredito aplicadas também NA LEITURA (decisões de 11/10/2026).

Checagens salvas antes da política (ex.: s29kr7s0bg2a, "Verdadeiro 95%" sem agência) são
corrigidas na página pública e em "Últimas", sem mexer no banco.
"""

import pytest

from app.config import DEFAULT_OPEN_PLATFORMS, Settings
from app.services.verdict_policy import (
    corroborates,
    is_open_platform,
    neutral_summary,
    read_verdict,
)

TITLE = "Ruas diz que segurança pública será prioridade de seu governo | G1"


# ---------- leitura ----------


def test_old_true_without_agency_is_read_as_awaiting_check() -> None:
    assert read_verdict("verdadeiro", []) == ("nao_verificavel", True)


def test_old_true_with_a_corroborating_rating_stays() -> None:
    assert read_verdict("verdadeiro", ["Verdadeiro"]) == ("verdadeiro", False)


@pytest.mark.parametrize("ratings", [["Falso"], ["Partly true"], ["Verdadeiro, mas exagerado"]])
def test_old_true_with_only_non_corroborating_ratings_is_downgraded(ratings: list[str]) -> None:
    assert read_verdict("verdadeiro", ratings) == ("nao_verificavel", True)


@pytest.mark.parametrize("veredito", ["falso", "enganoso", "fora_de_contexto", "nao_verificavel"])
def test_other_verdicts_are_read_as_stored(veredito: str) -> None:
    assert read_verdict(veredito, []) == (veredito, False)


@pytest.mark.parametrize("rating", ["Verdadeiro", "#FATO", "verídico", "True", " Correto. "])
def test_corroborating_ratings(rating: str) -> None:
    assert corroborates(rating)


@pytest.mark.parametrize("rating", ["Not true", "Fato ou Fake", "Verificado", "", "Falso"])
def test_non_corroborating_ratings(rating: str) -> None:
    assert not corroborates(rating)


# ---------- texto neutro ----------


def test_neutral_summary_for_a_confirmed_link_drops_the_site_suffix() -> None:
    assert neutral_summary("link", "g1.globo.com", TITLE, confirmed_origin=True) == (
        "Conteúdo publicado em g1.globo.com (origem confirmada) sobre: Ruas diz que segurança "
        "pública será prioridade de seu governo. Não encontramos checagens de agências sobre ele."
    )


def test_neutral_summary_for_an_open_platform() -> None:
    text = neutral_summary("link", "fulano.medium.com", "Meu texto", confirmed_origin=False)
    assert text == (
        "Conteúdo publicado em fulano.medium.com sobre: Meu texto. "
        "Não encontramos checagens de agências sobre ele."
    )


def test_neutral_summary_keeps_a_long_tail_after_the_bar() -> None:
    # Só corta " | Site" curto no fim; uma barra no meio do título é parte dele.
    title = "Eleição | o que muda no segundo turno para quem vota em Roraima"
    text = neutral_summary("link", "g1.globo.com", title, confirmed_origin=True)
    assert "sobre: Eleição | o que muda" in text


def test_neutral_summary_strips_links_from_the_page_title() -> None:
    # O título vem da página (do atacante): link no título não vai para o resumo público.
    title = "Urgente https://golpe.example.com/pix confira"
    text = neutral_summary("link", "g1.globo.com", title, confirmed_origin=True)
    assert "golpe.example.com" not in text and "http" not in text
    assert "sobre: Urgente confira." in text


def test_neutral_summary_keeps_a_title_whose_tail_has_many_words() -> None:
    title = "Lula | discurso de posse completo"
    text = neutral_summary("link", "g1.globo.com", title, confirmed_origin=True)
    assert "sobre: Lula | discurso de posse completo." in text


def test_neutral_summary_when_cited_agency_checks_do_not_confirm() -> None:
    text = neutral_summary(
        "link", "g1.globo.com", "Título", confirmed_origin=True, agency_checks=True
    )
    assert text.endswith("As checagens de agências encontradas não confirmam o conteúdo.")
    assert "Não encontramos" not in text


def test_neutral_summary_for_pasted_text_and_missing_domain() -> None:
    assert neutral_summary("texto", None, None, confirmed_origin=True) == (
        "Não encontramos checagens de agências sobre este conteúdo."
    )
    assert neutral_summary("link", None, None, confirmed_origin=True) == (
        "Não encontramos checagens de agências sobre este conteúdo."
    )


# ---------- plataformas abertas (lista configurável) ----------


@pytest.mark.parametrize(
    "domain",
    [
        "fulano.blogspot.com",
        "fulano.wordpress.com",
        "medium.com",
        "fulano.substack.com",
        "sites.google.com",
        "x.com",
        "m.facebook.com",
    ],
)
def test_default_open_platforms(domain: str) -> None:
    assert is_open_platform(domain, DEFAULT_OPEN_PLATFORMS)


@pytest.mark.parametrize("domain", ["g1.globo.com", "folhabv.com.br", "notmedium.com"])
def test_news_sites_are_not_open_platforms(domain: str) -> None:
    assert not is_open_platform(domain, DEFAULT_OPEN_PLATFORMS)


def test_open_platform_list_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPEN_PLATFORM_DOMAINS", "exemplo.org, Blog.Example.com")
    platforms = Settings().open_platforms
    assert platforms == ("exemplo.org", "blog.example.com")
    assert is_open_platform("a.exemplo.org", platforms)
    # Redes sociais continuam abertas mesmo fora da lista configurada.
    assert is_open_platform("instagram.com", platforms)
    assert not is_open_platform("medium.com", platforms)


def test_default_setting_is_the_default_list() -> None:
    assert Settings().open_platforms == DEFAULT_OPEN_PLATFORMS
