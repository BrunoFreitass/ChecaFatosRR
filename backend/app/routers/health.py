import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.db import DatabaseNotConfiguredError, get_engine
from app.errors import AppError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

# /health/ready é público (monitor de uptime, health check): o resultado fica em cache para
# chamadas em massa não ocuparem as poucas conexões do pool.
READY_OK_SECONDS = 60.0
READY_FAIL_SECONDS = 10.0  # falha dura pouco: o banco que volta aparece logo


class ReadyCache:
    """Resultado da última verificação do banco; uma verificação por vez (single-flight)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._expires_at = float("-inf")
        self._error: Exception | None = None

    async def check(self, probe: Callable[[], Awaitable[None]]) -> Exception | None:
        """None = banco ok; senão, a exceção da última verificação."""
        if self._clock() < self._expires_at:
            return self._error
        async with self._lock:
            if self._clock() < self._expires_at:  # outra chamada acabou de verificar
                return self._error
            try:
                await probe()
                self._error = None
            except Exception as exc:  # o motivo vai para o log; a resposta 503 é genérica
                self._error = exc
                # Uma linha por verificação real (no máximo a cada 10 s), não por chamada.
                logger.warning("db_unavailable", exc_info=exc)
            ttl = READY_OK_SECONDS if self._error is None else READY_FAIL_SECONDS
            self._expires_at = self._clock() + ttl
            return self._error


async def probe_database() -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("select 1"))


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: não toca no banco (usado pelo Render/Cloud Run e pelo monitor)."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    """Readiness: confirma que o banco responde (em cache: ver READY_OK_SECONDS)."""
    cache: ReadyCache = request.app.state.ready_cache
    error = await cache.check(lambda: probe_database())
    if isinstance(error, DatabaseNotConfiguredError):
        raise AppError(503, "db_not_configured", "Banco de dados não configurado.")
    if error is not None:
        raise AppError(503, "db_unavailable", "Banco de dados indisponível no momento.")
    return {"status": "ok", "database": "ok"}
