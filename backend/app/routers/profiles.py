from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, current_user, is_admin
from app.config import Settings, get_settings
from app.db import get_session
from app.errors import AppError
from app.legal import CURRENT_TERMS_VERSION
from app.repositories.profiles import Profile, ProfileRepository, SqlProfileRepository
from app.schemas.profiles import NicknameAvailabilityOut, ProfileCreate, ProfileOut
from app.services.profiles import (
    create_profile,
    keep_moderation_on_delete,
    nickname_availability,
)
from app.services.rate_limit import SlidingWindowLimiter
from app.services.read_cache import ReadCaches, get_read_caches
from app.services.supabase_admin import SupabaseAdmin, get_supabase_admin

router = APIRouter(tags=["perfil"])


def get_profile_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProfileRepository:
    return SqlProfileRepository(session)


User = Annotated[CurrentUser, Depends(current_user)]
Repo = Annotated[ProfileRepository, Depends(get_profile_repository)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _rate_limited(
    name: Literal["profile_create", "nickname_check"],
) -> Callable[..., Awaitable[None]]:
    """Limite por usuário (em memória): POST /me e a dica de apelido gravam no log."""

    # async: roda no event loop (uma thread só). Como "def" o FastAPI a levaria ao threadpool
    # e várias threads mexeriam no mesmo dict/deque sem trava.
    async def dependency(request: Request, user: User) -> None:
        limiter: SlidingWindowLimiter = getattr(request.app.state.rate_limiters, name)
        retry_after = limiter.hit(str(user.id))
        if retry_after is not None:
            raise AppError(
                429,
                "rate_limited",
                "Muitas tentativas. Aguarde um pouco e tente de novo.",
                {"retry_after": retry_after},
                {"Retry-After": str(retry_after)},
            )

    return dependency


def _to_out(profile: Profile, user: CurrentUser, settings: Settings) -> ProfileOut:
    status = profile.effective_status(datetime.now(UTC))
    return ProfileOut(
        apelido=profile.apelido,
        status=status,
        suspended_until=profile.suspended_until if status == "suspended" else None,
        is_admin=is_admin(user, settings.admin_email),
        terms_version=profile.terms_version,
        created_at=profile.created_at,
    )


@router.get("/me", response_model=ProfileOut)
async def read_me(user: User, repo: Repo, settings: AppSettings) -> ProfileOut:
    profile = await repo.get(user.id)
    if profile is None:
        raise AppError(
            404,
            "profile_not_found",
            "Escolha um apelido para continuar.",
            {"terms_version": CURRENT_TERMS_VERSION},
        )
    return _to_out(profile, user, settings)


@router.post(
    "/me",
    response_model=ProfileOut,
    status_code=201,
    dependencies=[Depends(_rate_limited("profile_create"))],
)
async def create_me(
    payload: ProfileCreate, user: User, repo: Repo, settings: AppSettings
) -> ProfileOut:
    profile = await create_profile(
        user=user,
        apelido=payload.apelido,
        terms_version=payload.terms_version,
        repo=repo,
        ban_salt=settings.ban_hash_salt,
    )
    return _to_out(profile, user, settings)


@router.get(
    "/apelido/disponivel",
    response_model=NicknameAvailabilityOut,
    dependencies=[Depends(_rate_limited("nickname_check"))],
)
async def check_nickname(
    q: Annotated[str, Query(max_length=40)], _user: User, repo: Repo
) -> NicknameAvailabilityOut:
    disponivel, motivo = await nickname_availability(q, repo)
    return NicknameAvailabilityOut(disponivel=disponivel, motivo=motivo)


@router.delete("/me", status_code=204)
async def delete_me(
    user: User,
    repo: Repo,
    settings: AppSettings,
    admin: Annotated[SupabaseAdmin, Depends(get_supabase_admin)],
    caches: Annotated[ReadCaches, Depends(get_read_caches)],
) -> Response:
    """Exclusão de conta (LGPD). O trigger do banco apaga os textos do log de moderação.
    Suspensão e advertências ficam guardadas pelo hash do sub do Google antes (plano §10)."""
    try:
        await keep_moderation_on_delete(user, repo, settings.ban_hash_salt)
        await admin.delete_user(user.id)
    finally:
        # Os comentários da conta somem do banco (cascade): sai das listas na hora.
        caches.comments.invalidate()
        caches.latest.invalidate()
    return Response(status_code=204)
