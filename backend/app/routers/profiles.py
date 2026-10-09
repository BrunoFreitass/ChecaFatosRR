from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, current_user, is_admin
from app.config import Settings, get_settings
from app.db import get_session
from app.errors import AppError
from app.legal import CURRENT_TERMS_VERSION
from app.repositories.profiles import Profile, ProfileRepository, SqlProfileRepository
from app.schemas.profiles import NicknameAvailabilityOut, ProfileCreate, ProfileOut
from app.services.profiles import create_profile, nickname_availability
from app.services.supabase_admin import SupabaseAdmin, get_supabase_admin

router = APIRouter(tags=["perfil"])


def get_profile_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProfileRepository:
    return SqlProfileRepository(session)


User = Annotated[CurrentUser, Depends(current_user)]
Repo = Annotated[ProfileRepository, Depends(get_profile_repository)]
AppSettings = Annotated[Settings, Depends(get_settings)]


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


@router.post("/me", response_model=ProfileOut, status_code=201)
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


@router.get("/apelido/disponivel", response_model=NicknameAvailabilityOut)
async def check_nickname(
    q: Annotated[str, Query(max_length=40)], _user: User, repo: Repo
) -> NicknameAvailabilityOut:
    disponivel, motivo = await nickname_availability(q, repo)
    return NicknameAvailabilityOut(disponivel=disponivel, motivo=motivo)


@router.delete("/me", status_code=204)
async def delete_me(
    user: User, admin: Annotated[SupabaseAdmin, Depends(get_supabase_admin)]
) -> Response:
    """Exclusão de conta (LGPD). O trigger do banco apaga os textos do log de moderação."""
    await admin.delete_user(user.id)
    return Response(status_code=204)
