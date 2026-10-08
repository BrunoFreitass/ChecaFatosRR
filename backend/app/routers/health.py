import logging

from fastapi import APIRouter
from sqlalchemy import text

from app.db import DatabaseNotConfiguredError, get_engine
from app.errors import AppError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: não toca no banco (usado pelo Cloud Run/Render)."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready() -> dict[str, str]:
    """Readiness: confirma que o banco responde."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("select 1"))
    except DatabaseNotConfiguredError as exc:
        raise AppError(503, "db_not_configured", "Banco de dados não configurado.") from exc
    except Exception as exc:
        logger.warning("db_unavailable", exc_info=exc)
        raise AppError(503, "db_unavailable", "Banco de dados indisponível no momento.") from exc
    return {"status": "ok", "database": "ok"}
