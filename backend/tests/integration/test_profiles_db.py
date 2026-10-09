from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.errors import AppError
from app.legal import CURRENT_TERMS_VERSION
from app.repositories.profiles import SqlProfileRepository
from app.services.bans import ban_hash
from app.services.profiles import create_profile
from tests.integration.conftest import create_auth_user

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SALT = "sal-integracao"


def user(user_id: object) -> CurrentUser:
    return CurrentUser(id=user_id, email="pessoa@example.com", provider="google")  # type: ignore[arg-type]


async def new_profile(session: AsyncSession, apelido: str | None = None) -> CurrentUser:
    current = user(await create_auth_user(session, google_sub=f"g-{uuid4().hex}"))
    await create_profile(
        user=current,
        apelido=apelido or f"u_{uuid4().hex[:10]}",
        terms_version=CURRENT_TERMS_VERSION,
        repo=SqlProfileRepository(session),
        ban_salt=SALT,
    )
    return current


async def test_duplicate_nickname_case_insensitive_is_409(session: AsyncSession) -> None:
    apelido = f"Dup_{uuid4().hex[:8]}"
    await new_profile(session, apelido)
    other = user(await create_auth_user(session, google_sub=f"g-{uuid4().hex}"))

    with pytest.raises(AppError) as exc:
        await create_profile(
            user=other,
            apelido=apelido.upper(),
            terms_version=CURRENT_TERMS_VERSION,
            repo=SqlProfileRepository(session),
            ban_salt=SALT,
        )

    assert exc.value.status_code == 409
    assert exc.value.code == "apelido_em_uso"


async def test_nickname_taken_is_case_insensitive(session: AsyncSession) -> None:
    apelido = f"Caixa_{uuid4().hex[:8]}"
    await new_profile(session, apelido)

    assert await SqlProfileRepository(session).nickname_taken(apelido.lower())


async def test_banned_google_identity_is_blocked(session: AsyncSession) -> None:
    google_sub = f"g-banido-{uuid4().hex}"
    user_id = await create_auth_user(session, google_sub=google_sub)
    async with session.begin():
        await session.execute(
            text("insert into public.banned_identities (sub_hash) values (:h)"),
            {"h": ban_hash(google_sub, salt=SALT)},
        )

    with pytest.raises(AppError) as exc:
        await create_profile(
            user=user(user_id),
            apelido=f"b_{uuid4().hex[:10]}",
            terms_version=CURRENT_TERMS_VERSION,
            repo=SqlProfileRepository(session),
            ban_salt=SALT,
        )

    assert exc.value.status_code == 403
    assert exc.value.code == "conta_banida"
    assert await SqlProfileRepository(session).get(user_id) is None


async def test_ban_uses_google_identity_not_supabase_user_id(session: AsyncSession) -> None:
    user_id = await create_auth_user(session, google_sub=f"g-{uuid4().hex}")
    async with session.begin():
        await session.execute(
            text("insert into public.banned_identities (sub_hash) values (:h)"),
            {"h": ban_hash(str(user_id), salt=SALT)},
        )

    await create_profile(
        user=user(user_id),
        apelido=f"ok_{uuid4().hex[:10]}",
        terms_version=CURRENT_TERMS_VERSION,
        repo=SqlProfileRepository(session),
        ban_salt=SALT,
    )

    assert await SqlProfileRepository(session).get(user_id) is not None


async def test_user_without_google_identity_is_rejected(session: AsyncSession) -> None:
    user_id = await create_auth_user(session)

    with pytest.raises(AppError) as exc:
        await create_profile(
            user=user(user_id),
            apelido=f"s_{uuid4().hex[:10]}",
            terms_version=CURRENT_TERMS_VERSION,
            repo=SqlProfileRepository(session),
            ban_salt=SALT,
        )

    assert exc.value.code == "login_google_obrigatorio"


async def insert_comment(
    session: AsyncSession,
    user_id: object,
    *,
    parent_id: int | None = None,
    link_id: int | None = None,
) -> int:
    async with session.begin():
        result = await session.execute(
            text(
                "insert into public.comments (user_id, link_id, parent_id, body) "
                "values (:u, :l, :p, 'texto') returning id"
            ),
            {"u": user_id, "l": link_id, "p": parent_id},
        )
        return int(result.scalar_one())


async def insert_link(session: AsyncSession) -> int:
    async with session.begin():
        result = await session.execute(
            text(
                "insert into public.links (url_hash, url_normalized, domain) "
                "values (:h, :u, 'example.com') returning id"
            ),
            {"h": uuid4().bytes * 2, "u": f"https://example.com/{uuid4().hex}"},
        )
        return int(result.scalar_one())


async def test_reply_one_level_is_allowed(session: AsyncSession) -> None:
    author = await new_profile(session)
    root = await insert_comment(session, author.id)

    reply = await insert_comment(session, author.id, parent_id=root)

    assert reply > root


async def test_reply_to_reply_is_rejected(session: AsyncSession) -> None:
    author = await new_profile(session)
    root = await insert_comment(session, author.id)
    reply = await insert_comment(session, author.id, parent_id=root)

    with pytest.raises(DBAPIError, match="apenas 1 nível"):
        await insert_comment(session, author.id, parent_id=reply)


async def test_reply_on_other_link_is_rejected(session: AsyncSession) -> None:
    author = await new_profile(session)
    root = await insert_comment(session, author.id)
    link_id = await insert_link(session)

    with pytest.raises(DBAPIError, match="mesmo link"):
        await insert_comment(session, author.id, parent_id=root, link_id=link_id)


async def test_account_deletion_purges_moderation_snapshots(session: AsyncSession) -> None:
    author = await new_profile(session)
    async with session.begin():
        log_id = (
            await session.execute(
                text(
                    "insert into public.moderation_log "
                    "(user_id, actor, action, category, content_snapshot) "
                    "values (:u, 'local_filter', 'blocked_local', 'palavrao', 'texto ruim') "
                    "returning id"
                ),
                {"u": author.id},
            )
        ).scalar_one()

    async with session.begin():
        await session.execute(text("delete from auth.users where id = :u"), {"u": author.id})

    async with session.begin():
        row = (
            await session.execute(
                text("select user_id, content_snapshot from public.moderation_log where id = :i"),
                {"i": log_id},
            )
        ).one()
    assert row.user_id is None
    assert row.content_snapshot is None
    assert await SqlProfileRepository(session).get(author.id) is None
