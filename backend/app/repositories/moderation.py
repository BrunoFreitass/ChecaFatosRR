"""Moderação no banco: log, strikes, suspensão, banimento e a fila do admin.

Toda mudança de strike ou status começa pelo lock do perfil (for no key update, o mesmo do
begin_check): dois strikes ao mesmo tempo são contados os dois, e a suspensão não se perde.
Regra (plano §3): 3 strikes ativos = suspenso por 24 h (cada novo strike renova); 5 = banido,
com o hash do sub do Google em banned_identities. Banido só sai com unban.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.checks import new_public_id

logger = logging.getLogger(__name__)

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
    # Onde o comentário seria publicado ({"link_id", "parent_id"}): o "Aprovar" do admin
    # publica a partir do log (decisão de 12/10/2026).
    target: dict[str, int | None] | None = None

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


@dataclass(frozen=True)
class QueueItem:
    id: int
    kind: str
    reason: str
    category: str | None
    confidence: float | None
    created_at: datetime
    user_id: UUID | None
    apelido: str | None
    comment_public_id: str | None
    comment_status: str | None
    text: str | None  # o comentário publicado ou, se bloqueado, o texto guardado no log
    target: dict[str, Any] | None  # onde publicar um comentário bloqueado


_QUEUE_COLUMNS = """
select q.id, q.kind, q.reason, q.category, q.ai_confidence, q.created_at, q.user_id,
       p.apelido::text as apelido, c.public_id as comment_public_id, c.status as comment_status,
       coalesce(c.body, l.content_snapshot) as texto, l.details -> 'alvo' as alvo
from public.moderation_queue q
left join public.profiles p on p.id = q.user_id
left join public.comments c on c.id = q.comment_id
left join public.moderation_log l on l.id = q.moderation_log_id
"""

# Publica o comentário que a IA bloqueou com baixa confiança, a partir do texto do log.
_CLAIM_BLOCKED = """
update public.moderation_queue set status = 'resolved', resolution = :resolution,
       resolved_at = clock_timestamp()
where id = :qid and status = 'pending' and kind = 'strike_confirmation'
returning id
"""

# Publica o comentário que a IA bloqueou com baixa confiança, a partir do texto do log, no
# lugar original (alvo). O pai precisa continuar visível.
_PUBLISH_FROM_LOG = """
insert into public.comments (user_id, link_id, parent_id, body, public_id)
select q.user_id, cast(l.details -> 'alvo' ->> 'link_id' as bigint),
       cast(l.details -> 'alvo' ->> 'parent_id' as bigint), l.content_snapshot, :public_id
from public.moderation_queue q join public.moderation_log l on l.id = q.moderation_log_id
join public.profiles p on p.id = q.user_id
where q.id = :qid and q.kind = 'strike_confirmation'
  and l.content_snapshot is not null and l.details ? 'alvo'
  -- Nunca publica em nome de conta banida ou suspensa (pode ter mudado desde o bloqueio).
  and (p.status = 'active'
       or (p.status = 'suspended' and p.suspended_until <= clock_timestamp()))
  and (l.details -> 'alvo' ->> 'parent_id' is null or exists (
    select 1 from public.comments pc
    where pc.id = cast(l.details -> 'alvo' ->> 'parent_id' as bigint) and pc.status = 'visible'
  ))
returning id
"""

# Erros do pai que sumiu: FK (23503) e o raise do trigger de 1 nível (P0001).
_PARENT_GONE_SQLSTATES = frozenset({"23503", "P0001"})


class _NothingToPublishError(Exception):
    """Rollback do claim: não havia o que publicar (pai oculto ou texto sumiu)."""


_ADMIN_LOG = """
insert into public.moderation_log (user_id, comment_id, actor, action, details)
select q.user_id, cast(:cid as bigint), 'admin', :action, jsonb_build_object('fila', q.id)
from public.moderation_queue q where q.id = :qid
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
        if block.target is not None:
            details["alvo"] = block.target
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

    # ---------- fila do admin ----------

    async def queue(self, limit: int = 100) -> list[QueueItem]:
        async with self._session.begin():
            rows = (
                (
                    await self._session.execute(
                        text(
                            _QUEUE_COLUMNS
                            + "where q.status = 'pending' order by q.created_at limit :limit"
                        ),
                        {"limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return [_queue_item(r) for r in rows]

    async def queue_item(self, queue_id: int) -> QueueItem | None:
        async with self._session.begin():
            row = (
                (
                    await self._session.execute(
                        text(_QUEUE_COLUMNS + "where q.id = :qid and q.status = 'pending'"),
                        {"qid": queue_id},
                    )
                )
                .mappings()
                .first()
            )
        return _queue_item(row) if row else None

    async def approve_comment(self, queue_id: int) -> bool:
        """Comentário publicado e revisado: fica (e volta, se as denúncias o esconderam). As
        denúncias anteriores são zeradas: uma denúncia nova não desfaz a decisão do admin."""
        async with self._session.begin():
            comment_id = await self._claim(queue_id, "approved")
            if comment_id is None:
                return False
            await self._session.execute(
                text(
                    "update public.comments set status = 'visible', report_count = 0 "
                    "where id = :cid and status in ('visible', 'hidden_reports')"
                ),
                {"cid": comment_id},
            )
            await self._session.execute(
                text("delete from public.comment_reports where comment_id = :cid"),
                {"cid": comment_id},
            )
            await self._close_comment_items(comment_id, "approved")
            await self._admin_log(queue_id, "approved", comment_id)
            return True

    async def remove_comment(self, queue_id: int) -> bool:
        async with self._session.begin():
            comment_id = await self._claim(queue_id, "removed")
            if comment_id is None:
                return False
            await self._session.execute(
                text(
                    "update public.comments set status = 'removed' "
                    "where id = :cid and status in ('visible', 'hidden_reports')"
                ),
                {"cid": comment_id},
            )
            await self._close_comment_items(comment_id, "removed")
            await self._admin_log(queue_id, "removed", comment_id)
            return True

    async def publish_from_log(self, queue_id: int) -> bool:
        """Admin aprova um comentário que a IA bloqueou: publica o texto do log no lugar
        original. False = já resolvido ou o lugar não existe mais (pai apagado, por exemplo)."""
        try:
            async with self._session.begin():
                claimed = await self._session.scalar(
                    text(_CLAIM_BLOCKED), {"qid": queue_id, "resolution": "approved"}
                )
                if claimed is None:
                    return False
                comment_id = await self._session.scalar(
                    text(_PUBLISH_FROM_LOG), {"qid": queue_id, "public_id": new_public_id()}
                )
                if comment_id is None:
                    raise _NothingToPublishError
                await self._admin_log(queue_id, "approved", comment_id)
                return True
        except _NothingToPublishError:
            return False
        except DBAPIError as exc:
            # Trigger de 1 nível (raise) ou FK: o pai sumiu ou mudou. Outro erro sobe.
            if getattr(exc.orig, "sqlstate", None) not in _PARENT_GONE_SQLSTATES:
                raise
            logger.info("publish_from_log_parent_gone", extra={"queue_id": queue_id})
            return False

    async def _claim(self, queue_id: int, resolution: str) -> int | None:
        """Resolve o item se ainda estava pendente; devolve o comment_id (None = não estava)."""
        row = (
            (
                await self._session.execute(
                    text(
                        "update public.moderation_queue set status = 'resolved', "
                        "resolution = :resolution, resolved_at = clock_timestamp() "
                        "where id = :qid and status = 'pending' and comment_id is not null "
                        "returning comment_id"
                    ),
                    {"qid": queue_id, "resolution": resolution},
                )
            )
            .mappings()
            .first()
        )
        return int(row["comment_id"]) if row else None

    async def _close_comment_items(self, comment_id: int, resolution: str) -> None:
        # Outros itens pendentes do mesmo comentário (ex.: desinformação + denúncias).
        await self._session.execute(
            text(
                "update public.moderation_queue set status = 'resolved', "
                "resolution = :resolution, resolved_at = clock_timestamp() "
                "where comment_id = :cid and status = 'pending'"
            ),
            {"cid": comment_id, "resolution": resolution},
        )

    async def _admin_log(self, queue_id: int, action: str, comment_id: int | None) -> None:
        await self._session.execute(
            text(_ADMIN_LOG), {"qid": queue_id, "action": action, "cid": comment_id}
        )

    async def resolve(self, queue_id: int, resolution: str) -> bool:
        async with self._session.begin():
            return await self._resolve(queue_id, resolution)

    async def _resolve(self, queue_id: int, resolution: str) -> bool:
        resolved = await self._session.scalar(
            text(_RESOLVE), {"qid": queue_id, "resolution": resolution}
        )
        return resolved is not None

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


def _queue_item(row: Any) -> QueueItem:
    target = row["alvo"]
    if isinstance(target, str):  # asyncpg devolve jsonb como texto
        target = json.loads(target)
    return QueueItem(
        id=row["id"],
        kind=row["kind"],
        reason=row["reason"],
        category=row["category"],
        confidence=row["ai_confidence"],
        created_at=row["created_at"],
        user_id=row["user_id"],
        apelido=row["apelido"],
        comment_public_id=row["comment_public_id"],
        comment_status=row["comment_status"],
        text=row["texto"],
        target=target if isinstance(target, dict) else None,
    )
