import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

ProfileStatus = Literal["active", "suspended", "banned"]

_UNIQUE_VIOLATION = "23505"
_FOREIGN_KEY_VIOLATION = "23503"


class NicknameTakenError(Exception):
    pass


class AuthUserGoneError(Exception):
    """O usuário do token já não existe em auth.users (conta apagada; token ainda válido)."""


class ProfileExistsError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    id: UUID
    apelido: str
    status: str
    suspended_until: datetime | None
    terms_version: str
    created_at: datetime

    def effective_status(self, now: datetime) -> ProfileStatus:
        """Suspensão vencida conta como ativa (não há job para "dessuspender")."""
        if self.status == "banned":
            return "banned"
        if self.status == "suspended" and self.suspended_until and self.suspended_until > now:
            return "suspended"
        return "active"


class ProfileRepository(Protocol):
    async def get(self, user_id: UUID) -> Profile | None: ...
    async def nickname_taken(self, apelido: str) -> bool: ...
    async def google_sub(self, user_id: UUID) -> str | None: ...
    async def is_banned(self, sub_hash: bytes) -> bool: ...
    async def create(self, user_id: UUID, apelido: str, terms_version: str) -> Profile: ...
    async def log_nickname_blocked(self, apelido: str, category: str, rule: str) -> None: ...


_SELECT_PROFILE = (
    "select id, apelido::text as apelido, status, suspended_until, terms_version, created_at "
    "from public.profiles where id = :id"
)
_INSERT_PROFILE = (
    "insert into public.profiles (id, apelido, terms_version) "
    "values (:id, :apelido, :terms_version) "
    "on conflict (id) do nothing "
    "returning id, apelido::text as apelido, status, suspended_until, terms_version, created_at"
)


class SqlProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UUID) -> Profile | None:
        async with self._session.begin():
            result = await self._session.execute(text(_SELECT_PROFILE), {"id": user_id})
            row = result.mappings().first()
        return Profile(**row) if row else None

    async def nickname_taken(self, apelido: str) -> bool:
        async with self._session.begin():
            taken = await self._session.scalar(
                text(
                    "select exists(select 1 from public.profiles "
                    "where apelido = cast(:apelido as extensions.citext))"
                ),
                {"apelido": apelido},
            )
        return bool(taken)

    async def google_sub(self, user_id: UUID) -> str | None:
        """`sub` da identidade Google, lido de auth.identities (o usuário não altera)."""
        async with self._session.begin():
            sub = await self._session.scalar(
                text(
                    "select provider_id from auth.identities "
                    "where user_id = :id and provider = 'google' limit 1"
                ),
                {"id": user_id},
            )
        return str(sub) if sub else None

    async def is_banned(self, sub_hash: bytes) -> bool:
        async with self._session.begin():
            banned = await self._session.scalar(
                text("select exists(select 1 from public.banned_identities where sub_hash = :h)"),
                {"h": sub_hash},
            )
        return bool(banned)

    async def create(self, user_id: UUID, apelido: str, terms_version: str) -> Profile:
        params = {"id": user_id, "apelido": apelido, "terms_version": terms_version}
        try:
            async with self._session.begin():
                result = await self._session.execute(text(_INSERT_PROFILE), params)
                row = result.mappings().first()
        except IntegrityError as exc:
            # Com "on conflict (id)", a única unicidade que sobra é a do apelido.
            sqlstate = getattr(exc.orig, "sqlstate", None)
            if sqlstate == _UNIQUE_VIOLATION:
                raise NicknameTakenError from exc
            if sqlstate == _FOREIGN_KEY_VIOLATION:
                raise AuthUserGoneError from exc
            raise
        if row is None:
            raise ProfileExistsError
        return Profile(**row)

    async def log_nickname_blocked(self, apelido: str, category: str, rule: str) -> None:
        # Sem user_id: a pessoa ainda não tem perfil. O texto é expurgado em 90 dias.
        async with self._session.begin():
            await self._session.execute(
                text(
                    "insert into public.moderation_log "
                    "(actor, action, category, content_snapshot, details) "
                    "values ('local_filter', 'nickname_blocked', :category, :apelido, "
                    "cast(:details as jsonb))"
                ),
                {
                    "category": category,
                    "apelido": apelido,
                    "details": json.dumps({"rule": rule}, ensure_ascii=False),
                },
            )
