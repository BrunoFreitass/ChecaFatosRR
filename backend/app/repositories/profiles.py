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
    async def suspension_until(self, sub_hash: bytes) -> datetime | None: ...
    async def keep_suspension(self, sub_hash: bytes, until: datetime) -> None: ...
    async def active_strikes(self, user_id: UUID) -> int: ...
    async def keep_strikes(self, sub_hash: bytes, strikes: int) -> None: ...
    async def create(
        self,
        user_id: UUID,
        apelido: str,
        terms_version: str,
        *,
        suspended_until: datetime | None = None,
        sub_hash: bytes | None = None,
    ) -> Profile: ...
    async def log_nickname_blocked(self, apelido: str, category: str, rule: str) -> None: ...


_SELECT_PROFILE = (
    "select id, apelido::text as apelido, status, suspended_until, terms_version, created_at "
    "from public.profiles where id = :id"
)
# 5 advertências já são banimento (banned_identities): guardar no máximo 4 (CHECK da tabela).
MAX_CARRIED_STRIKES = 4
_KEEP_STRIKES = """
insert into public.deleted_account_strikes (sub_hash, strikes, expires_at)
values (:h, :n, now() + interval '30 days')
on conflict (sub_hash) do update set
  -- Registro vencido (o cron ainda não apagou) não conta: vale só o de agora.
  strikes = case when deleted_account_strikes.expires_at <= now() then excluded.strikes
                 else greatest(deleted_account_strikes.strikes, excluded.strikes) end,
  expires_at = excluded.expires_at
"""

_RESTORE_STRIKES = """
with kept as (
  delete from public.deleted_account_strikes where sub_hash = :h
  returning strikes, expires_at
)
insert into public.strikes (user_id, category)
select :uid, 'conta_anterior' from kept, generate_series(1, kept.strikes)
where kept.expires_at > now()
"""

_INSERT_PROFILE = (
    "insert into public.profiles (id, apelido, terms_version, status, suspended_until) "
    "values (:id, :apelido, :terms_version, "
    "case when cast(:until as timestamptz) is null then 'active' else 'suspended' end, :until) "
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
            # Operador qualificado: sem `extensions` no search_path, `=` cairia em text = text
            # (sensível a maiúsculas). O índice único usa o operador do citext de qualquer jeito.
            taken = await self._session.scalar(
                text(
                    "select exists(select 1 from public.profiles "
                    "where apelido operator(extensions.=) cast(:apelido as extensions.citext))"
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
                # Só o banimento permanente (suspended_until nulo); a suspensão guardada na
                # exclusão da conta é lida por suspension_until.
                text(
                    "select exists(select 1 from public.banned_identities "
                    "where sub_hash = :h and suspended_until is null)"
                ),
                {"h": sub_hash},
            )
        return bool(banned)

    async def suspension_until(self, sub_hash: bytes) -> datetime | None:
        """Fim da suspensão guardada quando a conta suspensa foi excluída (None = nenhuma)."""
        async with self._session.begin():
            return await self._session.scalar(
                text(
                    "select suspended_until from public.banned_identities "
                    "where sub_hash = :h and suspended_until > now()"
                ),
                {"h": sub_hash},
            )

    async def keep_suspension(self, sub_hash: bytes, until: datetime) -> None:
        """Conta suspensa vai ser excluída: guarda o hash do sub com o fim da suspensão.
        Banimento permanente (data nula) nunca vira suspensão; duas datas, fica a maior."""
        async with self._session.begin():
            await self._session.execute(
                text(
                    "insert into public.banned_identities (sub_hash, suspended_until) "
                    "values (:h, :until) on conflict (sub_hash) do update set "
                    "suspended_until = greatest(banned_identities.suspended_until, "
                    "excluded.suspended_until) "
                    "where banned_identities.suspended_until is not null"
                ),
                {"h": sub_hash, "until": until},
            )

    async def active_strikes(self, user_id: UUID) -> int:
        async with self._session.begin():
            count = await self._session.scalar(
                text(
                    "select count(*) from public.strikes "
                    "where user_id = :uid and revoked_at is null"
                ),
                {"uid": user_id},
            )
        return int(count or 0)

    async def keep_strikes(self, sub_hash: bytes, strikes: int) -> None:
        """Conta com advertências vai ser excluída: guarda quantas, pelo hash do sub, por 30
        dias (decisão de 13/10/2026). Duas exclusões no prazo: fica o maior."""
        async with self._session.begin():
            await self._session.execute(
                text(_KEEP_STRIKES), {"h": sub_hash, "n": min(strikes, MAX_CARRIED_STRIKES)}
            )

    async def create(
        self,
        user_id: UUID,
        apelido: str,
        terms_version: str,
        *,
        suspended_until: datetime | None = None,
        sub_hash: bytes | None = None,
    ) -> Profile:
        params = {
            "id": user_id,
            "apelido": apelido,
            "terms_version": terms_version,
            "until": suspended_until,
        }
        try:
            async with self._session.begin():
                result = await self._session.execute(text(_INSERT_PROFILE), params)
                row = result.mappings().first()
                if row is not None and sub_hash is not None:
                    # Mesmo Google de uma conta excluída há menos de 30 dias: as advertências
                    # voltam (e o registro guardado some).
                    await self._session.execute(
                        text(_RESTORE_STRIKES), {"h": sub_hash, "uid": user_id}
                    )
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
