"""GET /admin/painel: painel mínimo do admin, só leitura (plano mapa-e-painel-admin §B).

Só o admin (require_admin: login Google + ADMIN_EMAIL), sem cache. Só contagens agregadas por
dia; os limites do Gemini vêm da configuração (os mesmos que valem nas reservas da cota).
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_admin
from app.config import Settings, get_settings
from app.db import get_session
from app.repositories.painel import PainelData, SqlPainelRepository
from app.schemas.painel import Cota, Gemini, Modelo, PainelOut

router = APIRouter(tags=["admin"])


def get_painel_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SqlPainelRepository:
    return SqlPainelRepository(session)


@router.get("/admin/painel", response_model=PainelOut)
async def admin_panel(
    _admin: Annotated[CurrentUser, Depends(require_admin)],
    repo: Annotated[SqlPainelRepository, Depends(get_painel_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    response: Response,
) -> PainelOut:
    response.headers["Cache-Control"] = "no-store"
    data = await repo.read()
    return PainelOut.model_validate(
        {
            "gerado_em": datetime.now(UTC),
            **data.daily,
            "fila": data.fila,
            "usuarios": data.usuarios,
            "gemini": _gemini(data, settings),
        }
    )


def _gemini(data: PainelData, settings: Settings) -> Gemini:
    quotas = [
        _cota("checagem", data.quota_used.get("checagem", 0), settings.gemini_daily_checks),
        _cota("moderacao", data.quota_used.get("moderacao", 0), settings.gemini_daily_moderation),
    ]
    checks = settings.gemini_models
    moderation = settings.gemini_moderation_models_list
    limits = settings.model_limits
    # Os modelos das duas cadeias, na ordem delas, e qualquer outro que tenha uso hoje.
    names = list(dict.fromkeys((*checks, *moderation, *sorted(data.model_used))))
    models = []
    for name in names:
        used = data.model_used.get(name, 0)
        limit = limits[name].rpd if name in limits else 0
        models.append(
            Modelo(
                modelo=name,
                usadas=used,
                limite=limit,
                restante=max(limit - used, 0),
                checagem=name in checks,
                moderacao=name in moderation,
            )
        )
    return Gemini(dia_pacifico=data.pacific_day, cota=quotas, modelos=models)


def _cota(uso: str, used: int, limit: int) -> Cota:
    return Cota(uso=uso, usadas=used, limite=limit, restante=max(limit - used, 0))
