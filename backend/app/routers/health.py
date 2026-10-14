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


# Uma marca por migration (algo que ela cria). Sem a migration aplicada, /health/ready dá 503
# com a lista (incidente de 13/10/2026: migrations dadas como aplicadas e não estavam).
# tests/unit/test_schema_markers.py exige uma marca por arquivo de supabase/migrations.
SCHEMA_MARKERS: dict[str, tuple[str, str, str | None]] = {
    "20261008000000_init": ("table", "public.strikes", None),
    "20261009000000_checagem": ("column", "public.checks", "public_id"),
    "20261010000000_prompt_version": ("column", "public.checks", "prompt_version"),
    "20261010000001_checks_listed_link_idx": ("index", "public.checks_listed_link_idx", None),
    "20261013000000_comentarios": ("column", "public.comments", "public_id"),
    "20261013000001_gemini_model_usage": ("table", "public.gemini_model_usage", None),
    "20261014000000_ajustes_moderacao": ("table", "public.deleted_account_strikes", None),
}

_MISSING_MIGRATIONS = """
select m.name from unnest(
  cast(:names as text[]), cast(:kinds as text[]), cast(:objs as text[]), cast(:cols as text[])
) as m(name, kind, obj, col)
where not case m.kind
  when 'column' then exists (
    select 1 from pg_attribute a
    where a.attrelid = to_regclass(m.obj) and a.attname = m.col and not a.attisdropped
  )
  when 'function' then to_regprocedure(m.obj) is not null
  else to_regclass(m.obj) is not null
end
order by m.name
"""


class SchemaOutdatedError(Exception):
    """O banco responde, mas falta aplicar migrations (a lista vai na resposta)."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__("migrations pendentes: " + ", ".join(missing))
        self.missing = missing


async def probe_database() -> None:
    markers = sorted(SCHEMA_MARKERS.items())
    async with get_engine().connect() as conn:
        result = await conn.execute(
            text(_MISSING_MIGRATIONS),
            {
                "names": [name for name, _ in markers],
                "kinds": [kind for _, (kind, _, _) in markers],
                "objs": [obj for _, (_, obj, _) in markers],
                "cols": [col for _, (_, _, col) in markers],
            },
        )
        missing = list(result.scalars())
    if missing:
        raise SchemaOutdatedError(missing)


# GET e HEAD: o UptimeRobot grátis só faz HEAD (antes, 405). Mesmo status do GET; o corpo
# do HEAD é descartado pelo servidor HTTP (uvicorn).
HEALTH_METHODS = ["GET", "HEAD"]


@router.api_route("/health", methods=HEALTH_METHODS)
async def health() -> dict[str, str]:
    """Liveness: não toca no banco (usado pelo Render/Cloud Run e pelo monitor)."""
    return {"status": "ok"}


@router.api_route("/health/ready", methods=HEALTH_METHODS)
async def ready(request: Request) -> dict[str, str]:
    """Readiness: o banco responde e tem todas as migrations (em cache: READY_OK_SECONDS)."""
    cache: ReadyCache = request.app.state.ready_cache
    error = await cache.check(lambda: probe_database())
    if isinstance(error, SchemaOutdatedError):
        raise AppError(
            503,
            "schema_desatualizado",
            "Banco de dados desatualizado: faltam migrations.",
            {"migrations_pendentes": error.missing},
        )
    if isinstance(error, DatabaseNotConfiguredError):
        raise AppError(503, "db_not_configured", "Banco de dados não configurado.")
    if error is not None:
        raise AppError(503, "db_unavailable", "Banco de dados indisponível no momento.")
    return {"status": "ok", "database": "ok", "schema": "ok"}
