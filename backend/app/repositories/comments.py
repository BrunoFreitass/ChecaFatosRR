"""Comentários no banco: publicar, listar (cursor), denunciar e apagar o próprio.

Regras (plano §3 e §9, decisões de 12/10/2026): comentário só no feed geral (link_id nulo) ou
numa checagem de LINK; resposta em 1 nível só (trigger comments_one_level); 3 denúncias de
contas com mais de 24 h escondem o comentário e o mandam para a fila do admin; o autor apaga
o próprio (o texto some; as respostas ficam). Ids públicos aleatórios; o bigint é só interno.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.checks import new_public_id

ReportOutcome = Literal[
    "ok", "duplicate", "not_found", "own", "too_new", "restricted", "no_profile"
]
# Respostas por comentário na listagem (o resto fica para "ver mais", no frontend).
MAX_REPLIES = 50
CommentStatus = Literal["visible", "hidden_reports", "removed", "removed_by_author"]


@dataclass(frozen=True)
class StoredComment:
    public_id: str
    apelido: str
    body: str | None  # None = removido pelo autor
    status: CommentStatus
    created_at: datetime
    user_id: UUID


@dataclass(frozen=True)
class Thread:
    root: StoredComment
    replies: list[StoredComment] = field(default_factory=list)


@dataclass(frozen=True)
class Parent:
    id: int
    link_id: int | None
    is_reply: bool
    visible: bool


@dataclass(frozen=True)
class Precheck:
    """Estado do usuário para os limites, lido antes da IA (sem lock): o insert confere de novo."""

    status: str
    suspended_until: datetime | None
    seconds_since_last: float | None
    day_count: int
    day_retry: int | None  # segundos até sair o mais antigo da janela de 24 h
    offensive_blocks: int
    offensive_retry: int | None  # segundos até acabar 1 h desde o último bloqueio sem strike
    ai_blocks: int


_COLUMNS = "c.public_id, p.apelido::text as apelido, c.body, c.status, c.created_at, c.user_id"

_PRECHECK = """
select p.status, p.suspended_until,
  -- Tentativa bloqueada também conta no intervalo (senão uma rajada bloqueada não para).
  extract(epoch from clock_timestamp() - greatest(
    (select max(created_at) from public.comments where user_id = :uid),
    (select max(created_at) from public.moderation_log
      where user_id = :uid and action in ('blocked_local', 'blocked_ai'))
  ))::float as seconds_since_last,
  (select count(*) from public.comments
    where user_id = :uid and created_at > now() - interval '24 hours') as day_count,
  ceil(extract(epoch from (
    (select min(created_at) from public.comments
      where user_id = :uid and created_at > now() - interval '24 hours')
    + interval '24 hours' - clock_timestamp())))::int as day_retry,
  (select count(*) from public.moderation_log
    where user_id = :uid and action in ('blocked_local', 'blocked_ai')
      and category in ('ofensivo', 'spam') and created_at > now() - interval '24 hours')
    as offensive_blocks,
  ceil(extract(epoch from (
    (select max(created_at) from public.moderation_log
      where user_id = :uid and action in ('blocked_local', 'blocked_ai')
        and category in ('ofensivo', 'spam') and created_at > now() - interval '24 hours')
    + interval '1 hour' - clock_timestamp())))::int as offensive_retry,
  (select count(*) from public.moderation_log
    where user_id = :uid and action = 'blocked_ai'
      and created_at > now() - interval '24 hours') as ai_blocks
from public.profiles p where p.id = :uid
"""

# Publica sob o lock do usuário; o intervalo mínimo é conferido de novo (duas abas ao mesmo
# tempo passariam pela checagem sem lock). Item da fila do admin no mesmo comando.
_INSERT = """
with c as (
  insert into public.comments (user_id, link_id, parent_id, body, public_id)
  select :uid, :link_id, :parent_id, :body, :public_id
  where not exists (
    select 1 from public.comments
    where user_id = :uid
      and created_at > clock_timestamp() - make_interval(secs => :min_interval)
  )
  returning id, public_id, body, status, created_at, user_id
),
q as (
  insert into public.moderation_queue (kind, reason, user_id, comment_id, category, ai_confidence)
  select 'comment_review', cast(:reason as text), :uid, c.id, cast(:category as text),
         cast(:confidence as double precision)
  from c where cast(:reason as text) is not null
)
select c.public_id, p.apelido::text as apelido, c.body, c.status, c.created_at, c.user_id
from c join public.profiles p on p.id = c.user_id
"""

_REPORT = """
with t as (
  select c.id, c.user_id, c.status from public.comments c where c.public_id = :pid
),
r as (
  select p.status, p.suspended_until, p.created_at from public.profiles p where p.id = :reporter
),
ins as (
  insert into public.comment_reports (comment_id, reporter_id, reason)
  select t.id, :reporter, :reason from t, r
  where t.user_id <> :reporter and t.status = 'visible'
    and r.created_at < now() - make_interval(hours => :min_age)
    and (r.status = 'active' or (r.status = 'suspended' and r.suspended_until <= now()))
  on conflict do nothing
  returning comment_id
),
up as (
  update public.comments c set
    report_count = c.report_count + 1,
    status = case when c.report_count + 1 >= :threshold then 'hidden_reports' else c.status end
  from ins where c.id = ins.comment_id and c.status = 'visible'
  returning c.id, c.user_id, c.status
),
q as (
  insert into public.moderation_queue (kind, reason, user_id, comment_id)
  select 'comment_review', 'hidden_by_reports', up.user_id, up.id
  from up where up.status = 'hidden_reports'
  on conflict (comment_id, reason) where status = 'pending' and comment_id is not null
  do nothing
)
select
  (select count(*) from t) as found,
  (select user_id from t) as author,
  (select status from t) as comment_status,
  (select created_at from r) as reporter_since,
  (select status from r) as reporter_status,
  (select suspended_until from r) as reporter_until,
  exists (select 1 from ins) as inserted
"""

# O autor apaga: o texto some (as respostas ficam). Se estava na fila ou tinha denúncia, o
# texto vai para o log do admin antes (decisão de 12/10/2026); os itens pendentes fecham.
_DELETE_OWN = """
with t as (
  select id, body, report_count from public.comments
  where public_id = :pid and user_id = :uid and status = 'visible'
),
pending as (
  select q.id from public.moderation_queue q join t on q.comment_id = t.id
  where q.status = 'pending'
),
-- O status é reconferido no UPDATE: se o admin removeu ou as denúncias ocultaram no meio,
-- nada muda (nem log, nem fila).
up as (
  update public.comments set status = 'removed_by_author', body = null
  where id in (select id from t) and status = 'visible'
  returning id
),
log as (
  insert into public.moderation_log
    (user_id, comment_id, actor, action, content_snapshot, details)
  select :uid, t.id, 'system', 'deleted_by_author', t.body,
         jsonb_build_object('denuncias', t.report_count)
  from t join up on up.id = t.id
  where t.report_count > 0 or exists (select 1 from pending)
),
closed as (
  update public.moderation_queue set status = 'resolved', resolution = 'deleted_by_author',
         resolved_at = clock_timestamp()
  where id in (select id from pending) and exists (select 1 from up)
)
select exists (select 1 from up)
"""


class AccountRestrictedError(Exception):
    """A conta foi banida, suspensa ou excluída enquanto o comentário era analisado."""


def _comment(row: Any) -> StoredComment:
    return StoredComment(
        public_id=row["public_id"],
        apelido=row["apelido"],
        body=row["body"],
        status=row["status"],
        created_at=row["created_at"],
        user_id=row["user_id"],
    )


class SqlCommentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def link_of_check(self, check_public_id: str) -> tuple[bool, int | None]:
        """(existe, link_id). Checagem de texto colado: (True, None) — sem comentários."""
        async with self._session.begin():
            row = (
                (
                    await self._session.execute(
                        text("select link_id from public.checks where public_id = :pid"),
                        {"pid": check_public_id},
                    )
                )
                .mappings()
                .first()
            )
        return (row is not None, row["link_id"] if row else None)

    async def parent(self, public_id: str) -> Parent | None:
        async with self._session.begin():
            row = (
                (
                    await self._session.execute(
                        text(
                            "select id, link_id, parent_id, status from public.comments "
                            "where public_id = :pid"
                        ),
                        {"pid": public_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return Parent(
            id=row["id"],
            link_id=row["link_id"],
            is_reply=row["parent_id"] is not None,
            visible=row["status"] == "visible",
        )

    async def precheck(self, user_id: UUID) -> Precheck | None:
        async with self._session.begin():
            row = (
                (await self._session.execute(text(_PRECHECK), {"uid": user_id})).mappings().first()
            )
        if row is None:
            return None
        return Precheck(
            status=row["status"],
            suspended_until=row["suspended_until"],
            seconds_since_last=row["seconds_since_last"],
            day_count=int(row["day_count"]),
            day_retry=row["day_retry"],
            offensive_blocks=int(row["offensive_blocks"]),
            offensive_retry=row["offensive_retry"],
            ai_blocks=int(row["ai_blocks"]),
        )

    async def create(
        self,
        user_id: UUID,
        *,
        link_id: int | None,
        parent_id: int | None,
        body: str,
        min_interval_seconds: int,
        queue: tuple[str, str | None, float | None] | None = None,
    ) -> StoredComment | None:
        """Publica. None = outra requisição do mesmo usuário publicou dentro do intervalo."""
        reason, category, confidence = queue or (None, None, None)
        async with self._session.begin():
            # Reconfere o status sob o lock: a IA leva segundos e a conta pode ter sido
            # banida ou suspensa nesse meio-tempo.
            can_post = await self._session.scalar(
                text(
                    "select status = 'active' or (status = 'suspended' "
                    "and suspended_until <= clock_timestamp()) "
                    "from public.profiles where id = :uid for no key update"
                ),
                {"uid": user_id},
            )
            if not can_post:
                raise AccountRestrictedError
            row = (
                (
                    await self._session.execute(
                        text(_INSERT),
                        {
                            "uid": user_id,
                            "link_id": link_id,
                            "parent_id": parent_id,
                            "body": body,
                            "public_id": new_public_id(),
                            "min_interval": min_interval_seconds,
                            "reason": reason,
                            "category": category,
                            "confidence": confidence,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return _comment(row) if row else None

    async def threads(
        self, link_id: int | None, cursor: tuple[datetime, str] | None, limit: int
    ) -> list[Thread]:
        """Raízes (mais novas primeiro) com as respostas (mais antigas primeiro). A raiz
        removida pelo autor só aparece se tiver resposta visível; oculta ou removida pelo
        admin some com as respostas."""
        scope = "c.link_id = :link_id" if link_id is not None else "c.link_id is null"
        sql = (
            f"select {_COLUMNS}, c.id from public.comments c "  # noqa: S608
            "join public.profiles p on p.id = c.user_id "
            f"where c.parent_id is null and {scope} "
            "and (c.status = 'visible' or (c.status = 'removed_by_author' and exists ("
            "select 1 from public.comments r where r.parent_id = c.id and r.status = 'visible'))) "
        )
        params: dict[str, Any] = {"limit": limit}
        if link_id is not None:
            params["link_id"] = link_id
        if cursor is not None:
            sql += "and (c.created_at, c.public_id) < (:cursor_at, :cursor_id) "
            params |= {"cursor_at": cursor[0], "cursor_id": cursor[1]}
        sql += "order by c.created_at desc, c.public_id desc limit :limit"
        async with self._session.begin():
            roots = (await self._session.execute(text(sql), params)).mappings().all()
            replies = (
                (
                    await self._session.execute(
                        text(
                            f"select * from (select {_COLUMNS}, c.parent_id, "  # noqa: S608
                            "row_number() over (partition by c.parent_id "
                            "order by c.created_at, c.public_id) as n "
                            "from public.comments c join public.profiles p on p.id = c.user_id "
                            "where c.parent_id = any(:ids) "
                            "and c.status in ('visible', 'removed_by_author')) r "
                            "where r.n <= :max_replies order by r.created_at, r.public_id"
                        ),
                        {"ids": [r["id"] for r in roots], "max_replies": MAX_REPLIES},
                    )
                )
                .mappings()
                .all()
                if roots
                else []
            )
        by_parent: dict[int, list[StoredComment]] = {}
        for reply in replies:
            by_parent.setdefault(reply["parent_id"], []).append(_comment(reply))
        return [Thread(_comment(r), by_parent.get(r["id"], [])) for r in roots]

    async def report(
        self,
        public_id: str,
        reporter_id: UUID,
        *,
        reason: str | None,
        threshold: int,
        min_account_hours: int,
    ) -> ReportOutcome:
        async with self._session.begin():
            row = (
                (
                    await self._session.execute(
                        text(_REPORT),
                        {
                            "pid": public_id,
                            "reporter": reporter_id,
                            "reason": reason,
                            "threshold": threshold,
                            "min_age": min_account_hours,
                        },
                    )
                )
                .mappings()
                .one()
            )
        if row["inserted"]:
            return "ok"
        if not row["found"] or row["comment_status"] != "visible":
            return "not_found"
        if row["author"] == reporter_id:
            return "own"
        if row["reporter_since"] is None:
            return "no_profile"
        now = datetime.now(UTC)
        status, until = row["reporter_status"], row["reporter_until"]
        if status == "banned" or (status == "suspended" and until is not None and until > now):
            return "restricted"
        since = row["reporter_since"]
        if since is None or since > now - timedelta(hours=min_account_hours):
            return "too_new"
        return "duplicate"

    async def delete_own(self, public_id: str, user_id: UUID) -> bool:
        async with self._session.begin():
            deleted = await self._session.scalar(
                text(_DELETE_OWN), {"pid": public_id, "uid": user_id}
            )
        return bool(deleted)
