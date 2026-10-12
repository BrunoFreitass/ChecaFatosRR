"""Moderação no banco: log, strikes, suspensão, banimento e a fila do admin.

Toda mudança de strike ou status começa pelo lock do perfil (for no key update, o mesmo do
begin_check): dois strikes ao mesmo tempo são contados os dois, e a suspensão não se perde.
Regra (plano §3): 3 strikes ativos = suspenso por 24 h (cada novo strike renova); 5 = banido,
com o hash do sub do Google em banned_identities. Banido só sai com unban.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ProfileStatus = Literal["active", "suspended", "banned"]
BlockActor = Literal["local_filter", "ai_filter"]

SUSPEND_AT = 3
BAN_AT = 5


@dataclass(frozen=True)
class Block:
    """Comentário bloqueado (não é salvo em comments): o texto fica só no log."""

    actor: BlockActor
    category: str
    snapshot: str
    strike: bool
    confidence: float | None = None  # da IA (0 a 1)
    ask_admin: bool = False  # strike de baixa confiança: o admin confirma

    def __post_init__(self) -> None:
        if self.strike and self.ask_admin:
            raise ValueError("ou o strike é automático, ou o admin confirma: não os dois")


@dataclass(frozen=True)
class StrikeOutcome:
    active_strikes: int
    status: ProfileStatus
    suspended_until: datetime | None


class ModerationRepository(Protocol):
    async def record_block(
        self, user_id: UUID, block: Block, *, sub_hash: bytes | None
    ) -> StrikeOutcome: ...
    async def confirm_strike(
        self, queue_id: int, *, sub_hash: bytes | None
    ) -> StrikeOutcome | None: ...
    async def dismiss(self, queue_id: int) -> bool: ...
    async def ban(self, user_id: UUID, *, sub_hash: bytes | None, reason: str) -> None: ...
    async def unban(self, user_id: UUID, *, sub_hash: bytes | None) -> None: ...


# Só o lock: contar aqui veria o snapshot de antes da espera (os comandos seguintes, não).
_LOCK_PROFILE = "select 1 from public.profiles where id = :uid for no key update"

# Log do bloqueio (+ item da fila, se o strike espera o admin) num comando.
_LOG_BLOCK = """
with log as (
  insert into public.moderation_log (user_id, actor, action, category, content_snapshot, details)
  values (:uid, :actor, :action, :category, :snapshot, cast(:details as jsonb))
  returning id
),
q as (
  insert into public.moderation_queue
    (kind, reason, user_id, moderation_log_id, category, ai_confidence)
  select 'strike_confirmation', 'ai_low_confidence', cast(:uid as uuid), log.id,
         cast(:category as text), cast(:confidence as double precision)
  from log where cast(:ask_admin as boolean)
)
select log.id, p.status, p.suspended_until,
       (select count(*) from public.strikes s where s.user_id = p.id and s.revoked_at is null)
         as active
from log cross join public.profiles p where p.id = :uid
"""

# Strike + status num comando (o count do snapshot não vê o strike novo: + 1).
_STRIKE = f"""
with st as (
  insert into public.strikes (user_id, category, moderation_log_id)
  values (:uid, :category, :log_id)
),
n as (
  select count(*) + 1 as active from public.strikes
  where user_id = :uid and revoked_at is null
),
p as (
  update public.profiles pr set
    status = case when pr.status = 'banned' or n.active >= {BAN_AT} then 'banned'
                  when n.active >= {SUSPEND_AT} then 'suspended'
                  else pr.status end,
    suspended_until = case when pr.status = 'banned' or n.active >= {BAN_AT} then null
                           when n.active >= {SUSPEND_AT}
                           then clock_timestamp() + interval '24 hours'
                           else pr.suspended_until end
  from n where pr.id = :uid
  returning pr.status, pr.suspended_until
),
b as (
  insert into public.banned_identities (sub_hash)
  select cast(:sub_hash as bytea) from n where n.active >= {BAN_AT}
  on conflict (sub_hash) do update set suspended_until = null
),
-- Auditoria: cada suspensão (e renovação) e o banimento ficam no log.
l as (
  insert into public.moderation_log (user_id, actor, action, details)
  select cast(:uid as uuid), 'system', p.status, jsonb_build_object('strikes', n.active)
  from n cross join p where n.active >= {SUSPEND_AT}
)
select n.active, p.status, p.suspended_until from n cross join p
"""  # noqa: S608 (só as constantes inteiras BAN_AT e SUSPEND_AT entram no f-string)

_PENDING_STRIKE = """
select q.user_id, q.category, q.moderation_log_id
from public.moderation_queue q
where q.id = :qid and q.kind = 'strike_confirmation' and q.status = 'pending'
"""

_RESOLVE = """
update public.moderation_queue set status = 'resolved', resolution = :resolution,
       resolved_at = clock_timestamp()
where id = :qid and status = 'pending'
returning id
"""

_BAN = """
with p as (
  update public.profiles set status = 'banned', suspended_until = null where id = :uid
),
b as (
  insert into public.banned_identities (sub_hash) values (:sub_hash)
  on conflict (sub_hash) do update set suspended_until = null
)
insert into public.moderation_log (user_id, actor, action, details)
values (:uid, 'admin', 'banned', cast(:details as jsonb))
"""

# Desbanir recomeça do zero: os strikes ativos são revogados (senão o próximo baniria de novo).
_UNBAN = """
with r as (
  update public.strikes set revoked_at = clock_timestamp()
  where user_id = :uid and revoked_at is null
  returning 1
),
p as (update public.profiles set status = 'active', suspended_until = null where id = :uid),
d as (delete from public.banned_identities where sub_hash = cast(:sub_hash as bytea))
insert into public.moderation_log (user_id, actor, action, details)
select :uid, 'admin', 'unbanned', jsonb_build_object('strikes_revogados', count(*)) from r
"""


class ProfileGoneError(Exception):
    """O perfil não existe mais (conta excluída no meio do caminho)."""


def _require_hash(sub_hash: bytes | None) -> bytes:
    # Banir sem registrar a identidade deixaria o banido voltar excluindo e recriando a conta.
    if sub_hash is None or len(sub_hash) != 32:
        raise ValueError("hash da identidade Google obrigatório (HMAC de 32 bytes)")
    return sub_hash


class SqlModerationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_block(
        self, user_id: UUID, block: Block, *, sub_hash: bytes | None
    ) -> StrikeOutcome:
        action = "blocked_local" if block.actor == "local_filter" else "blocked_ai"
        details: dict[str, Any] = {"strike": block.strike}
        if block.confidence is not None:
            details["confianca"] = block.confidence
        if block.strike:
            _require_hash(sub_hash)  # qualquer strike pode ser o 5º
        async with self._session.begin():
            await self._lock(user_id)
            result = await self._session.execute(
                text(_LOG_BLOCK),
                {
                    "uid": user_id,
                    "actor": block.actor,
                    "action": action,
                    "category": block.category,
                    "snapshot": block.snapshot[:1000],
                    "details": json.dumps(details),
                    "confidence": block.confidence,
                    "ask_admin": block.ask_admin,
                },
            )
            logged = result.mappings().one()
            if not block.strike:
                return StrikeOutcome(
                    int(logged["active"]), logged["status"], logged["suspended_until"]
                )
            return await self._strike(user_id, block.category, int(logged["id"]), sub_hash)

    async def confirm_strike(
        self, queue_id: int, *, sub_hash: bytes | None
    ) -> StrikeOutcome | None:
        """Admin confirma o strike que a IA pediu com baixa confiança. None = item não está
        mais pendente (outro clique já resolveu) ou a conta foi excluída no meio."""
        _require_hash(sub_hash)
        try:
            return await self._confirm_strike(queue_id, sub_hash)
        except ProfileGoneError:
            return None  # o item da fila sai junto com a conta (on delete cascade)

    async def _confirm_strike(self, queue_id: int, sub_hash: bytes | None) -> StrikeOutcome | None:
        async with self._session.begin():
            item = (
                (await self._session.execute(text(_PENDING_STRIKE), {"qid": queue_id}))
                .mappings()
                .first()
            )
            if item is None:
                return None
            await self._lock(item["user_id"])
            # O resolve vem antes: se outro admin resolveu no meio, nada de strike duplicado.
            if (
                await self._session.scalar(
                    text(_RESOLVE), {"qid": queue_id, "resolution": "strike_confirmed"}
                )
                is None
            ):
                return None
            return await self._strike(
                item["user_id"], item["category"], item["moderation_log_id"], sub_hash
            )

    async def dismiss(self, queue_id: int) -> bool:
        async with self._session.begin():
            resolved = await self._session.scalar(
                text(_RESOLVE), {"qid": queue_id, "resolution": "dismissed"}
            )
        return resolved is not None

    async def ban(self, user_id: UUID, *, sub_hash: bytes | None, reason: str) -> None:
        _require_hash(sub_hash)
        async with self._session.begin():
            await self._lock(user_id)
            await self._session.execute(
                text(_BAN),
                {"uid": user_id, "sub_hash": sub_hash, "details": json.dumps({"motivo": reason})},
            )

    async def unban(self, user_id: UUID, *, sub_hash: bytes | None) -> None:
        _require_hash(sub_hash)
        async with self._session.begin():
            await self._lock(user_id)
            await self._session.execute(text(_UNBAN), {"uid": user_id, "sub_hash": sub_hash})

    async def _lock(self, user_id: UUID) -> None:
        if await self._session.scalar(text(_LOCK_PROFILE), {"uid": user_id}) is None:
            raise ProfileGoneError

    async def _strike(
        self, user_id: UUID, category: str, log_id: int | None, sub_hash: bytes | None
    ) -> StrikeOutcome:
        row = (
            (
                await self._session.execute(
                    text(_STRIKE),
                    {
                        "uid": user_id,
                        "category": category,
                        "log_id": log_id,
                        "sub_hash": sub_hash,
                    },
                )
            )
            .mappings()
            .one()
        )
        return StrikeOutcome(int(row["active"]), row["status"], row["suspended_until"])
