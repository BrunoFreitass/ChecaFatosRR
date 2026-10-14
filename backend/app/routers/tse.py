"""Apuração oficial do TSE (etapa 5): resultado e status do banner da home."""

import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.errors import AppError
from app.repositories.tse import SqlTseMapRepository, SqlTseRepository, TseRepository
from app.services.tse.client import HttpTseClient, TseClient
from app.services.tse.mapa import TseMapRepository, load_map
from app.services.tse.service import ScopeResult, TseCache, load_results

router = APIRouter(tags=["tse"])

OFFICIAL_LINK = "https://resultados.tse.jus.br/"
BANNER_TEXT = "Acompanhe a apuração oficial do 2º turno — fonte: TSE"
# Navegador e Cloudflare podem reaproveitar por 15 s; o ETag faz a revalidação custar ~0 byte.
RESULT_CACHE = "public, max-age=15"
# O mapa muda devagar (cada UF no máximo a cada 2 min): cache público de 30 s.
MAP_CACHE = "public, max-age=30"
STATUS_CACHE = "public, max-age=60"


def get_tse_repository(session: Annotated[AsyncSession, Depends(get_session)]) -> TseRepository:
    return SqlTseRepository(session)


@lru_cache(maxsize=4)
def _client(url_template: str, election: int) -> HttpTseClient:
    return HttpTseClient(url_template, election)


def get_tse_client(settings: Annotated[Settings, Depends(get_settings)]) -> TseClient:
    return _client(settings.tse_url_template, settings.tse_election_code)


def get_now() -> datetime:
    return datetime.now(UTC)


@router.get("/tse/resultado")
async def resultado(
    request: Request,
    repo: Annotated[TseRepository, Depends(get_tse_repository)],
    client: Annotated[TseClient, Depends(get_tse_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    if not settings.tse_enabled:
        raise AppError(404, "apuracao_indisponivel", "A apuração ainda não está disponível.")

    async def build() -> tuple[bytes, str]:
        # Corpo e ETag montados uma vez por recarga do cache (não a cada requisição).
        return _render(await load_results(repo, client, settings))

    cache: TseCache[tuple[bytes, str]] = request.app.state.tse_cache
    content, etag = await cache.get(build)
    headers = {"ETag": etag, "Cache-Control": RESULT_CACHE}
    if _not_modified(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content, media_type="application/json", headers=headers)


def _render(results: list[ScopeResult]) -> tuple[bytes, str]:
    body = {
        "fonte": "TSE",
        "link_oficial": OFFICIAL_LINK,
        "escopos": [{"escopo": r.escopo, "resultado": r.resultado} for r in results],
    }
    content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    return content, '"' + hashlib.sha256(content).hexdigest()[:20] + '"'


def _not_modified(header: str | None, etag: str) -> bool:
    """If-None-Match: lista separada por vírgula, "*" e ETag fraco (W/) de proxy que comprime."""
    if not header:
        return False
    tags = [tag.strip().removeprefix("W/") for tag in header.split(",")]
    return "*" in tags or etag in tags


@router.get("/tse/status")
async def status(
    settings: Annotated[Settings, Depends(get_settings)],
    now: Annotated[datetime, Depends(get_now)],
) -> Response:
    shown = settings.tse_banner_inicio <= now < settings.tse_banner_fim
    body = {
        "banner": shown,
        "texto": BANNER_TEXT,
        "apuracao": settings.tse_enabled,
        "mapa": settings.tse_enabled and settings.tse_mapa_enabled,
    }
    content = json.dumps(body, ensure_ascii=False).encode()
    return Response(content, media_type="application/json", headers={"Cache-Control": STATUS_CACHE})


def get_tse_map_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TseMapRepository:
    return SqlTseMapRepository(session)


@router.get("/tse/mapa")
async def mapa(
    request: Request,
    repo: Annotated[TseMapRepository, Depends(get_tse_map_repository)],
    client: Annotated[TseClient, Depends(get_tse_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Resumo por UF para o mapa. TSE_MAPA_ENABLED=false: 404 sem buscar nada no TSE."""
    if not (settings.tse_enabled and settings.tse_mapa_enabled):
        raise AppError(404, "mapa_indisponivel", "O mapa da apuração não está disponível.")

    async def build() -> tuple[bytes, str]:
        states = await load_map(repo, client, settings)
        body = {
            "fonte": "TSE",
            "link_oficial": OFFICIAL_LINK,
            "eleicao": str(settings.tse_election_code),
            "cores": settings.tse_mapa_color_map,
            "estados": [{"uf": uf, "resultado": resumo} for uf, resumo in states],
        }
        content = json.dumps(body, ensure_ascii=False).encode()
        return content, '"' + hashlib.sha256(content).hexdigest()[:32] + '"'

    cache: TseCache[tuple[bytes, str]] = request.app.state.tse_map_cache
    content, etag = await cache.get(build)
    headers = {"ETag": etag, "Cache-Control": MAP_CACHE}
    if _not_modified(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content, media_type="application/json", headers=headers)
