"""Título, texto principal, data e site de uma página HTML (trafilatura)."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime

import trafilatura

from app.config import get_settings
from app.services.fetcher import FetchedPage, PageUnreadableError
from app.services.gemini import BOA_VISTA, MAX_PROMPT_CONTENT_CHARS
from app.services.url_guard import collect_urls

logger = logging.getLogger(__name__)

# Corte do texto enviado ao Gemini (economia de cota; ver cost-aware-llm-pipeline).
MAX_TEXT_CHARS = MAX_PROMPT_CONTENT_CHARS  # o mesmo teto do que o Gemini recebe
MAX_TITLE_CHARS = 500
MIN_TEXT_CHARS = 200
_parsers: asyncio.Semaphore | None = None


def _parser_slots() -> asyncio.Semaphore:
    """Semáforo dos parsers, do tamanho de EXTRACT_CONCURRENCY (criado no primeiro uso)."""
    global _parsers
    if _parsers is None:
        _parsers = asyncio.Semaphore(get_settings().extract_concurrency)
    return _parsers


@dataclass(frozen=True)
class ExtractedPage:
    title: str | None
    text: str
    published_at: date | None
    urls: list[str]


async def extract_page(page: FetchedPage) -> ExtractedPage:
    try:
        # lxml é CPU: fora do event loop. A thread não é cancelável, então o semáforo limita
        # quantos parsers rodam juntos (HTML patológico não esgota o executor) e só é solto
        # quando a THREAD termina — não quando a requisição desiste (timeout/cancelamento).
        slots = _parser_slots()
        await slots.acquire()
        task = asyncio.ensure_future(
            asyncio.to_thread(
                trafilatura.bare_extraction,
                page.content,
                url=page.url,
                with_metadata=True,
                include_comments=False,
                include_tables=False,
                include_links=False,
                favor_precision=True,
            )
        )
        task.add_done_callback(lambda _: slots.release())
        document = await asyncio.shield(task)
    except Exception as exc:  # HTML que o parser não aguenta é "página ilegível", não 500
        logger.info("extraction_failed", extra={"error": type(exc).__name__})
        raise PageUnreadableError("vazio") from exc
    text = (getattr(document, "text", None) or "").strip()
    if len(text) < MIN_TEXT_CHARS:
        raise PageUnreadableError("vazio")

    title = (getattr(document, "title", None) or "").strip()[:MAX_TITLE_CHARS] or None
    return ExtractedPage(
        title=title,
        text=text[:MAX_TEXT_CHARS],
        published_at=_parse_date(getattr(document, "date", None)),
        # Só o trecho que o Gemini vê (o texto bruto pode ter até 2 MB).
        urls=collect_urls(text[:MAX_TEXT_CHARS]),
    )


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.date()
    # Dia do calendário de Roraima: 01:00 UTC de 10/10 ainda é 09/10 em Boa Vista.
    return parsed.astimezone(BOA_VISTA).date()
