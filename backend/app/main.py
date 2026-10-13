from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import dispose_engine
from app.errors import register_error_handlers
from app.logging_setup import configure_logging
from app.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.routers import checks, comments, health, legal, profiles, tse
from app.services.rate_limit import RateLimiters
from app.services.tse.service import TseCache


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    """Fábrica da aplicação. Em produção: `uvicorn --factory app.main:create_app`."""
    settings = get_settings()
    if settings.app_env != "test":
        configure_logging(settings.log_level)

    app = FastAPI(
        title="ChecaFatosRR API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    # A última adicionada é a mais externa: CORS envolve o RequestContext, então até a
    # resposta 500 gerada pelo RequestContext recebe os cabeçalhos de CORS.
    # Mais interno: o 413 do limite de corpo passa pelo RequestContext (request_id e
    # cabeçalhos de segurança) e pelo CORS.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    # Autenticação via header Authorization (Bearer), sem cookies: allow_credentials=False.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    register_error_handlers(app)
    app.state.rate_limiters = RateLimiters()
    app.state.ready_cache = health.ReadyCache()
    app.state.tse_cache = TseCache(ttl=5.0)
    app.include_router(health.router)
    app.include_router(legal.router)
    app.include_router(profiles.router)
    app.include_router(checks.router)
    app.include_router(tse.router)
    app.include_router(comments.router)
    return app
