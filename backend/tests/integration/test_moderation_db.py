"""Strikes, suspensão e banimento no Postgres — TESTE OBRIGATÓRIO (etapa 4).

Regras (plano §3 e §9): 3 strikes ativos = suspenso por 24 h (o 4º renova); 5 = banido, com
o hash do sub do Google em banned_identities; strike de baixa confiança da IA espera o admin;
"ofensivo"/"spam" bloqueiam sem strike. Tudo sob o lock por usuário.
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.moderation import Block, SqlModerationRepository
from tests.integration.test_checks_db import new_user, other_session

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def sub_hash(user_id: UUID) -> bytes:
    return hashlib.sha256(user_id.bytes).digest()  # o HMAC real é testado em test_bans


def block(category: str = "palavrao", *, strike: bool = True, **extra: object) -> Block:
    values: dict[str, object] = {
        "actor": "local_filter",
        "category": category,
        "snapshot": f"comentário com {category}",
        "strike": strike,
    }
    return Block(**(values | extra))  # type: ignore[arg-type]


async def profile(session: AsyncSession, user_id: UUID) -> tuple[str, datetime | None]:
    async with session.begin():
        row = (
            await session.execute(
                text("select status, suspended_until from public.profiles where id = :id"),
                {"id": user_id},
            )
        ).one()
    return row.status, row.suspended_until


async def scalar(session: AsyncSession, sql: str, **params: object) -> object:
    async with session.begin():
        return await session.scalar(text(sql), params)


async def test_strikes_suspend_at_three_renew_at_four_and_ban_at_five(
    session: AsyncSession,
) -> None:
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)
    ban = sub_hash(user_id)

    first = await repo.record_block(user_id, block(), sub_hash=ban)
    second = await repo.record_block(user_id, block(), sub_hash=ban)
    assert (first.active_strikes, second.active_strikes) == (1, 2)
    assert second.status == "active"

    third = await repo.record_block(user_id, block("sexual"), sub_hash=ban)
    assert third.status == "suspended"
    assert third.suspended_until is not None
    remaining = third.suspended_until - datetime.now(UTC)
    assert timedelta(hours=23, minutes=59) < remaining <= timedelta(hours=24, seconds=5)

    fourth = await repo.record_block(user_id, block(), sub_hash=ban)
    assert fourth.status == "suspended"
    assert fourth.suspended_until is not None and fourth.suspended_until > third.suspended_until

    fifth = await repo.record_block(user_id, block(), sub_hash=ban)
    assert (fifth.active_strikes, fifth.status) == (5, "banned")
    banned_until = await scalar(
        session,
        "select suspended_until from public.banned_identities where sub_hash = :h",
        h=ban,
    )
    exists = await scalar(
        session, "select count(*) from public.banned_identities where sub_hash = :h", h=ban
    )
    assert exists == 1 and banned_until is None  # banimento permanente


async def test_block_keeps_the_text_in_the_log(session: AsyncSession) -> None:
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)

    await repo.record_block(user_id, block("palavrao"), sub_hash=sub_hash(user_id))

    async with session.begin():
        row = (
            await session.execute(
                text(
                    "select actor, action, category, content_snapshot from public.moderation_log "
                    "where user_id = :id"
                ),
                {"id": user_id},
            )
        ).one()
    assert (row.actor, row.action, row.category) == ("local_filter", "blocked_local", "palavrao")
    assert row.content_snapshot == "comentário com palavrao"


async def test_offensive_block_has_no_strike_and_no_queue(session: AsyncSession) -> None:
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)

    outcome = await repo.record_block(
        user_id, block("ofensivo", strike=False, actor="ai_filter", confidence=0.99), sub_hash=None
    )

    assert (outcome.active_strikes, outcome.status) == (0, "active")
    strikes = await scalar(
        session, "select count(*) from public.strikes where user_id = :id", id=user_id
    )
    queued = await scalar(
        session, "select count(*) from public.moderation_queue where user_id = :id", id=user_id
    )
    assert (strikes, queued) == (0, 0)


async def test_low_confidence_waits_for_the_admin(session: AsyncSession) -> None:
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)

    outcome = await repo.record_block(
        user_id,
        block("crime", strike=False, actor="ai_filter", confidence=0.6, ask_admin=True),
        sub_hash=None,
    )

    assert outcome.active_strikes == 0
    async with session.begin():
        item = (
            await session.execute(
                text(
                    "select kind, reason, category, ai_confidence, status, moderation_log_id "
                    "from public.moderation_queue where user_id = :id"
                ),
                {"id": user_id},
            )
        ).one()
    assert (item.kind, item.reason, item.category) == (
        "strike_confirmation",
        "ai_low_confidence",
        "crime",
    )
    assert item.ai_confidence == pytest.approx(0.6)
    assert item.status == "pending" and item.moderation_log_id is not None


async def test_admin_confirms_or_dismisses_the_low_confidence_strike(
    session: AsyncSession,
) -> None:
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)
    for _ in range(2):
        await repo.record_block(
            user_id,
            block("crime", strike=False, actor="ai_filter", confidence=0.6, ask_admin=True),
            sub_hash=None,
        )
    async with session.begin():
        first, second = (
            await session.scalars(
                text("select id from public.moderation_queue where user_id = :id order by id"),
                {"id": user_id},
            )
        ).all()

    confirmed = await repo.confirm_strike(first, sub_hash=sub_hash(user_id))
    assert confirmed is not None and confirmed.active_strikes == 1
    assert await repo.dismiss(second) is True
    assert await repo.confirm_strike(first, sub_hash=sub_hash(user_id)) is None  # resolvido

    async with session.begin():
        resolutions = (
            await session.scalars(
                text(
                    "select resolution from public.moderation_queue where user_id = :id order by id"
                ),
                {"id": user_id},
            )
        ).all()
    assert resolutions == ["strike_confirmed", "dismissed"]


async def test_parallel_strikes_are_all_counted(session: AsyncSession) -> None:
    user_id = await new_user(session)
    repo = SqlModerationRepository(session)
    await repo.record_block(user_id, block(), sub_hash=sub_hash(user_id))
    await repo.record_block(user_id, block(), sub_hash=sub_hash(user_id))

    async def strike() -> None:
        async with other_session() as own:
            await SqlModerationRepository(own).record_block(
                user_id, block(), sub_hash=sub_hash(user_id)
            )

    await asyncio.gather(strike(), strike())

    status, until = await profile(session, user_id)
    active = await scalar(
        session,
        "select count(*) from public.strikes where user_id = :id and revoked_at is null",
        id=user_id,
    )
    assert active == 4
    assert status == "suspended" and until is not None


async def test_admin_ban_and_unban(session: AsyncSession) -> None:
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)
    ban = sub_hash(user_id)
    await repo.record_block(user_id, block(), sub_hash=ban)

    await repo.ban(user_id, sub_hash=ban, reason="fila")
    assert (await profile(session, user_id))[0] == "banned"
    # Um strike confirmado depois não "rebaixa" o banimento para suspensão.
    await repo.record_block(user_id, block(), sub_hash=ban)
    await repo.record_block(user_id, block(), sub_hash=ban)
    assert (await profile(session, user_id))[0] == "banned"

    await repo.unban(user_id, sub_hash=ban)

    status, until = await profile(session, user_id)
    assert (status, until) == ("active", None)
    hashes = await scalar(
        session, "select count(*) from public.banned_identities where sub_hash = :h", h=ban
    )
    active = await scalar(
        session,
        "select count(*) from public.strikes where user_id = :id and revoked_at is null",
        id=user_id,
    )
    assert (hashes, active) == (0, 0)  # desbanir recomeça do zero
    actions = await scalar(
        session,
        "select string_agg(action, ',' order by id) from public.moderation_log "
        "where user_id = :id and actor = 'admin'",
        id=user_id,
    )
    assert actions == "banned,unbanned"


async def test_ban_converts_a_kept_suspension_into_a_permanent_ban(session: AsyncSession) -> None:
    # A conta suspensa foi excluída antes (hash com data); agora o usuário é banido.
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)
    ban = sub_hash(user_id)
    async with session.begin():
        await session.execute(
            text(
                "insert into public.banned_identities (sub_hash, suspended_until) "
                "values (:h, now() + interval '1 day')"
            ),
            {"h": ban},
        )

    await repo.ban(user_id, sub_hash=ban, reason="fila")

    until = await scalar(
        session,
        "select suspended_until from public.banned_identities where sub_hash = :h",
        h=ban,
    )
    assert until is None


# ---------- revisões (12/10/2026) ----------


def test_block_cannot_be_a_strike_and_ask_the_admin_at_once() -> None:
    with pytest.raises(ValueError):
        block("crime", strike=True, ask_admin=True)


async def test_ban_without_the_identity_hash_is_refused(session: AsyncSession) -> None:
    # Banir pela metade (perfil banido, identidade não registrada) deixaria o banido voltar
    # excluindo e recriando a conta.
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)

    with pytest.raises(ValueError):
        await repo.ban(user_id, sub_hash=None, reason="fila")
    with pytest.raises(ValueError):
        await repo.unban(user_id, sub_hash=None)
    with pytest.raises(ValueError):
        await repo.record_block(user_id, block(), sub_hash=None)  # strike exige o hash

    assert (await profile(session, user_id))[0] == "active"


async def test_suspension_and_ban_are_logged_and_ban_clears_the_date(
    session: AsyncSession,
) -> None:
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)
    ban = sub_hash(user_id)
    for _ in range(5):
        await repo.record_block(user_id, block(), sub_hash=ban)

    status, until = await profile(session, user_id)
    assert (status, until) == ("banned", None)
    actions = await scalar(
        session,
        "select string_agg(action, ',' order by id) from public.moderation_log "
        "where user_id = :id and actor = 'system'",
        id=user_id,
    )
    assert actions == "suspended,suspended,banned"


async def test_block_without_strike_reports_the_fresh_strike_count(session: AsyncSession) -> None:
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)
    ban = sub_hash(user_id)
    await repo.record_block(user_id, block(), sub_hash=ban)

    outcome = await repo.record_block(
        user_id, block("spam", strike=False, actor="ai_filter", confidence=0.9), sub_hash=None
    )
    assert outcome.active_strikes == 1


async def test_confirming_a_strike_of_a_deleted_profile_returns_none(
    session: AsyncSession,
) -> None:
    repo = SqlModerationRepository(session)
    user_id = await new_user(session)
    await repo.record_block(
        user_id,
        block("crime", strike=False, actor="ai_filter", confidence=0.6, ask_admin=True),
        sub_hash=None,
    )
    async with session.begin():
        queue_id = await session.scalar(
            text("select id from public.moderation_queue where user_id = :id"), {"id": user_id}
        )
        await session.execute(text("delete from auth.users where id = :id"), {"id": user_id})

    assert await repo.confirm_strike(int(queue_id), sub_hash=sub_hash(user_id)) is None
