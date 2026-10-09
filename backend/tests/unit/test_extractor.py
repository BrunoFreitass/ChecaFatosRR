from datetime import date

import pytest

from app.services.extractor import MAX_TEXT_CHARS, _parse_date, extract_page
from app.services.fetcher import FetchedPage, PageUnreadableError

pytestmark = pytest.mark.anyio

BODY = (
    "A Justiça Eleitoral confirmou o calendário do segundo turno em Roraima. "
    "A votação acontece no domingo, das 8h às 17h, em todas as seções do estado. "
) * 4


def page(html: str) -> FetchedPage:
    return FetchedPage(url="https://site.com.br/n", content=html.encode(), truncated=False)


async def test_extracts_title_text_date_and_cited_urls() -> None:
    html = (
        "<html><head><title>Calendário mantido</title>"
        "<meta property='article:published_time' content='2026-09-28T10:00:00Z'></head>"
        f"<body><nav>Menu Início Contato</nav><article><h1>Calendário mantido</h1><p>{BODY}</p>"
        "<p>Fonte: https://www.tse.jus.br/comunicacao/noticias e mais detalhes.</p>"
        "</article></body></html>"
    )
    result = await extract_page(page(html))
    assert result.title == "Calendário mantido"
    assert "segundo turno em Roraima" in result.text
    assert result.published_at == date(2026, 9, 28)
    assert "https://www.tse.jus.br/comunicacao/noticias" in result.urls


async def test_text_is_capped() -> None:
    html = f"<html><body><article><p>{'palavra ' * 5000}</p></article></body></html>"
    result = await extract_page(page(html))
    assert len(result.text) <= MAX_TEXT_CHARS


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html><body></body></html>",
        "<html><body><p>Faça login para continuar.</p></body></html>",
        "\x00\x01\x02 não é html",
    ],
)
async def test_empty_or_tiny_pages_are_unreadable(html: str) -> None:
    with pytest.raises(PageUnreadableError) as exc:
        await extract_page(page(html))
    assert exc.value.reason == "vazio"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 01:00 UTC de 10/10 = 21:00 de 09/10 em Boa Vista: senão a notícia nasce "amanhã".
        ("2026-10-10T01:00:00Z", date(2026, 10, 9)),
        ("2026-10-10T01:00:00+00:00", date(2026, 10, 9)),
        ("2026-10-09T23:30:00-04:00", date(2026, 10, 9)),
        ("2026-10-10", date(2026, 10, 10)),  # só a data: fica como está
        ("ontem", None),
    ],
)
def test_publication_date_is_the_boa_vista_calendar_day(raw: str, expected: date | None) -> None:
    assert _parse_date(raw) == expected
