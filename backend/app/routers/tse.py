"""Apuração oficial do TSE (etapa 5): resultado e status do banner da home."""

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.config import Settings, get_settings
from app.db import session_scope
from app.errors import AppError
from app.repositories.tse import SqlTseMapRepository, SqlTseRepository, TseRepository
from app.services.tse.client import HttpTseClient, TseClient
from app.services.tse.mapa import TseMapRepository, read_map, refresh_map
from app.services.tse.service import ScopeResult, TseCache, read_results, refresh_results

router = APIRouter(tags=["tse"])

OFFICIAL_LINK = "https://resultados.tse.jus.br/"
BANNER_TEXT = "Acompanhe a apuração oficial do 2º turno — fonte: TSE"
# Navegador e Cloudflare podem reaproveitar por 15 s; o ETag faz a revalidação custar ~0 byte.
RESULT_CACHE = "public, max-age=15"
# O mapa muda devagar (cada UF no máximo a cada 2 min): cache público de 30 s.
MAP_CACHE = "public, max-age=30"
STATUS_CACHE = "public, max-age=60"


# A recarga do TSE roda em segundo plano, depois da resposta: cada leitura e cada recarga abre
# a própria sessão do banco (a da requisição já estaria fechada).
TseRepos = Callable[[], AbstractAsyncContextManager[TseRepository]]
TseMapRepos = Callable[[], AbstractAsyncContextManager[TseMapRepository]]


@asynccontextmanager
async def _sql_tse_repo() -> AsyncIterator[TseRepository]:
    async with session_scope() as session:
        yield SqlTseRepository(session)


@asynccontextmanager
async def _sql_tse_map_repo() -> AsyncIterator[TseMapRepository]:
    async with session_scope() as session:
        yield SqlTseMapRepository(session)


def get_tse_repos() -> TseRepos:
    return _sql_tse_repo


def get_tse_map_repos() -> TseMapRepos:
    return _sql_tse_map_repo


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
    repos: Annotated[TseRepos, Depends(get_tse_repos)],
    client: Annotated[TseClient, Depends(get_tse_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    if not settings.tse_enabled:
        raise AppError(404, "apuracao_indisponivel", "A apuração ainda não está disponível.")

    async def read() -> tuple[bytes, str]:
        # Corpo e ETag montados uma vez por leitura do banco (não a cada requisição).
        async with repos() as repo:
            return _render(await read_results(repo, settings))

    async def refresh() -> None:  # só em segundo plano: a requisição nunca espera o TSE
        async with repos() as repo:
            await refresh_results(repo, client, settings)

    cache: TseCache[tuple[bytes, str]] = request.app.state.tse_cache
    content, etag = await cache.get(read, refresh)
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


@router.get("/tse/mapa")
async def mapa(
    request: Request,
    repos: Annotated[TseMapRepos, Depends(get_tse_map_repos)],
    client: Annotated[TseClient, Depends(get_tse_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Resumo por UF para o mapa. TSE_MAPA_ENABLED=false: 404 sem buscar nada no TSE."""
    if not (settings.tse_enabled and settings.tse_mapa_enabled):
        raise AppError(404, "mapa_indisponivel", "O mapa da apuração não está disponível.")

    async def read() -> tuple[bytes, str]:
        async with repos() as repo:
            states = await read_map(repo, settings)
        body = {
            "fonte": "TSE",
            "link_oficial": OFFICIAL_LINK,
            "eleicao": str(settings.tse_election_code),
            "cores": settings.tse_mapa_color_map,
            "estados": [{"uf": uf, "resultado": resumo} for uf, resumo in states],
        }
        content = json.dumps(body, ensure_ascii=False).encode()
        return content, '"' + hashlib.sha256(content).hexdigest()[:32] + '"'

    async def refresh() -> None:  # só em segundo plano: a requisição nunca espera o TSE
        async with repos() as repo:
            await refresh_map(repo, client, settings)

    cache: TseCache[tuple[bytes, str]] = request.app.state.tse_map_cache
    content, etag = await cache.get(read, refresh)
    headers = {"ETag": etag, "Cache-Control": MAP_CACHE}
    if _not_modified(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content, media_type="application/json", headers=headers)
