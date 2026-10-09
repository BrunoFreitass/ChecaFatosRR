import asyncio
import hashlib
import re
from datetime import date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_engine
from app.repositories.checks import (
    CacheHit,
    LimitExceededError,
    Limits,
    LinkSubject,
    NewCheck,
    SqlCheckRepository,
    Started,
    StoredCheck,
    TextSubject,
)
from tests.integration.conftest import create_auth_user

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PUBLIC_ID = re.compile(r"^[0-9a-hjkmnp-tv-z]{12}$")
LIMITS = Limits(cache_hours=24, checks_per_hour=10, lookups_per_hour=60)


def other_session() -> AsyncSession:
    """Sessão independente (outra conexão), para simular requisições concorrentes."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)()


async def new_user(session: AsyncSession) -> UUID:
    user_id = await create_auth_user(session)
    async with session.begin():
        await session.execute(
            text(
                "insert into public.profiles (id, apelido, terms_version) "
                "values (:id, :apelido, 'teste')"
            ),
            {"id": user_id, "apelido": f"u_{user_id.hex[:12]}"},
        )
    return user_id


def link() -> LinkSubject:
    url = f"https://exemplo.com.br/{uuid4().hex}"
    return LinkSubject(
        url=url, url_hash=hashlib.sha256(url.encode()).digest(), domain="exemplo.com.br"
    )


def message() -> TextSubject:
    body = f"Mensagem encaminhada número {uuid4().hex} sobre a votação em Roraima."
    return TextSubject(body=body, text_hash=hashlib.sha256(body.encode()).digest())


async def finish(
    repo: SqlCheckRepository,
    started: Started,
    user_id: UUID,
    *,
    listed: bool = True,
    token: datetime | None = None,
) -> StoredCheck:
    return await repo.save_result(
        NewCheck(
            subject=started.subject,
            user_id=user_id,
            veredito="falso",
            confianca=80,
            result={"resumo": "A votação não foi adiada."},
            model="gemini-teste",
            listed=listed,
            title="Título" if started.subject.kind == "link" else None,
            published_at=date(2026, 9, 28) if started.subject.kind == "link" else None,
            claim_token=token,
        )
    )


async def test_link_check_cache_and_public_id(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    subject = link()

    started = await repo.begin_check(user_id, subject, LIMITS)
    assert isinstance(started, Started)
    token = await repo.claim(started.subject)
    assert token is not None
    assert await repo.claim(started.subject) is None  # single-flight: o segundo perde

    stored = await finish(repo, started, user_id, token=token)
    assert PUBLIC_ID.match(stored.public_id)
    assert stored.kind == "link"
    assert stored.url == subject.url
    assert stored.result == {"resumo": "A votação não foi adiada."}
    assert stored.published_at == date(2026, 9, 28)  # coluna date: sem deslocar o dia

    again = await repo.begin_check(user_id, subject, LIMITS)
    assert isinstance(again, CacheHit)
    assert again.check.public_id == stored.public_id
    assert (await repo.get(stored.public_id)) == stored
    assert await repo.claim(started.subject)  # save_result soltou o claim


async def test_expired_claim_can_be_taken_again(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    started = await repo.begin_check(user_id, link(), LIMITS)
    assert isinstance(started, Started)
    assert await repo.claim(started.subject)
    async with session.begin():
        await session.execute(
            text(
                "update public.links set checking_until = now() - interval '1 second' "
                "where id = :id"
            ),
            {"id": started.subject.id},
        )
    assert await repo.claim(started.subject)


async def test_text_check_is_never_listed(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    started = await repo.begin_check(user_id, message(), LIMITS)
    assert isinstance(started, Started)
    stored = await finish(repo, started, user_id, listed=False)
    assert stored.kind == "texto"
    assert stored.texto and stored.texto.startswith("Mensagem encaminhada")

    latest = await repo.latest(None, 1000)
    assert stored.public_id not in {item.public_id for item in latest}

    # O banco recusa marcar uma checagem de texto como listada.
    with pytest.raises(DBAPIError):
        async with session.begin():
            await session.execute(
                text("update public.checks set listed = true where public_id = :pid"),
                {"pid": stored.public_id},
            )


@pytest.mark.parametrize("both", [True, False])
async def test_check_needs_exactly_one_subject(session: AsyncSession, both: bool) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    link_started = await repo.begin_check(user_id, link(), LIMITS)
    text_started = await repo.begin_check(user_id, message(), LIMITS)
    assert isinstance(link_started, Started) and isinstance(text_started, Started)

    values = (
        {"link_id": link_started.subject.id, "text_id": text_started.subject.id}
        if both
        else {"link_id": None, "text_id": None}
    )
    with pytest.raises(DBAPIError):
        async with session.begin():
            await session.execute(
                text(
                    "insert into public.checks (link_id, text_id, veredito, confianca, result, "
                    "model, public_id, listed) values (:link_id, :text_id, 'falso', 50, '{}', "
                    "'m', '0123456789ab', false)"
                ),
                values,
            )


async def test_public_id_format_is_enforced(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    started = await repo.begin_check(user_id, link(), LIMITS)
    assert isinstance(started, Started)
    with pytest.raises(DBAPIError):
        async with session.begin():
            await session.execute(
                text(
                    "insert into public.checks (link_id, veredito, confianca, result, model, "
                    "public_id) values (:id, 'falso', 50, '{}', 'm', '42')"
                ),
                {"id": started.subject.id},
            )


async def test_concurrent_requests_share_one_link_row(session: AsyncSession) -> None:
    first_user = await new_user(session)
    second_user = await new_user(session)
    subject = link()
    async with other_session() as s1, other_session() as s2:
        results = await asyncio.gather(
            SqlCheckRepository(s1).begin_check(first_user, subject, LIMITS),
            SqlCheckRepository(s2).begin_check(second_user, subject, LIMITS),
        )
    assert all(isinstance(r, Started) for r in results)
    assert results[0].subject == results[1].subject  # type: ignore[union-attr]


async def test_hourly_limit_is_enforced_even_with_parallel_requests(
    session: AsyncSession,
) -> None:
    user_id = await new_user(session)
    one_per_hour = Limits(cache_hours=24, checks_per_hour=1, lookups_per_hour=60)

    async def attempt() -> object:
        async with other_session() as s:
            try:
                return await SqlCheckRepository(s).begin_check(user_id, link(), one_per_hour)
            except LimitExceededError as exc:
                return exc

    results = await asyncio.gather(attempt(), attempt(), attempt())
    started = [r for r in results if isinstance(r, Started)]
    blocked = [r for r in results if isinstance(r, LimitExceededError)]
    assert len(started) == 1  # o lock por usuário serializa a contagem
    assert len(blocked) == 2
    assert all(b.kind == "checagens" and 1 <= b.retry_after_seconds <= 3600 for b in blocked)

    # Devolver o limite (falha nossa) libera uma nova checagem.
    repo = SqlCheckRepository(session)
    await repo.refund(started[0].request_id)
    assert isinstance(await repo.begin_check(user_id, link(), one_per_hour), Started)


async def test_cache_lookups_have_their_own_limit(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    subject = link()
    two_lookups = Limits(cache_hours=24, checks_per_hour=10, lookups_per_hour=2)
    started = await repo.begin_check(user_id, subject, two_lookups)
    assert isinstance(started, Started)
    await finish(repo, started, user_id)

    assert isinstance(await repo.begin_check(user_id, subject, two_lookups), CacheHit)
    assert isinstance(await repo.begin_check(user_id, subject, two_lookups), CacheHit)
    with pytest.raises(LimitExceededError) as exc:
        await repo.begin_check(user_id, subject, two_lookups)
    assert exc.value.kind == "consultas"


async def test_old_check_is_not_served_from_cache(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    subject = link()
    started = await repo.begin_check(user_id, subject, LIMITS)
    assert isinstance(started, Started)
    stored = await finish(repo, started, user_id)
    async with session.begin():
        await session.execute(
            text(
                "update public.checks set created_at = now() - interval '25 hours' "
                "where public_id = :p"
            ),
            {"p": stored.public_id},
        )
    assert isinstance(await repo.begin_check(user_id, subject, LIMITS), Started)


async def test_gemini_daily_quota_never_exceeds_the_limit(session: AsyncSession) -> None:
    async with session.begin():
        await session.execute(text("delete from public.gemini_usage"))

    async def reserve() -> bool:
        async with other_session() as s:
            return await SqlCheckRepository(s).reserve_gemini("checagem", 5)

    results = await asyncio.gather(*(reserve() for _ in range(12)))
    assert results.count(True) == 5

    repo = SqlCheckRepository(session)
    assert await repo.reserve_gemini("moderacao", 1)  # cotas separadas
    assert not await repo.reserve_gemini("moderacao", 1)
    assert not await repo.reserve_gemini("checagem", 0)

    async with session.begin():
        day_ok = await session.scalar(
            text(
                "select bool_and(day = (now() at time zone 'America/Los_Angeles')::date) "
                "from public.gemini_usage"
            )
        )
    assert day_ok


async def test_latest_lists_only_listed_links_in_order(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    created: list[StoredCheck] = []
    for listed in (True, False, True, True):
        started = await repo.begin_check(user_id, link(), LIMITS)
        assert isinstance(started, Started)
        created.append(await finish(repo, started, user_id, listed=listed))

    every = [item.public_id for item in await repo.latest(None, 1000)]
    ours = [c.public_id for c in created]
    assert ours[1] not in every
    positions = [every.index(pid) for pid in (ours[3], ours[2], ours[0])]
    assert positions == sorted(positions)  # mais recentes primeiro

    first_page = await repo.latest(None, 1)
    newest = first_page[0]
    rest = await repo.latest((newest.created_at, newest.public_id), 1000)
    assert newest.public_id not in {item.public_id for item in rest}


async def test_daily_limit_counts_only_new_checks(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    two_per_day = Limits(cache_hours=24, checks_per_hour=10, lookups_per_hour=60, checks_per_day=2)
    subject = link()
    started = await repo.begin_check(user_id, subject, two_per_day)
    assert isinstance(started, Started)
    await finish(repo, started, user_id)
    assert isinstance(await repo.begin_check(user_id, link(), two_per_day), Started)

    with pytest.raises(LimitExceededError) as exc:
        await repo.begin_check(user_id, link(), two_per_day)
    assert exc.value.kind == "diario"
    assert 3600 < exc.value.retry_after_seconds <= 24 * 3600

    # Consulta ao cache continua liberada (e não conta no teto diário).
    assert isinstance(await repo.begin_check(user_id, subject, two_per_day), CacheHit)


async def test_only_the_claim_owner_releases_it(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    started = await repo.begin_check(user_id, link(), LIMITS)
    assert isinstance(started, Started)

    old_token = await repo.claim(started.subject)
    assert old_token is not None
    async with session.begin():  # o claim de A expira
        await session.execute(
            text(
                "update public.links set checking_until = now() - interval '1 second' "
                "where id = :id"
            ),
            {"id": started.subject.id},
        )
    new_token = await repo.claim(started.subject)  # B assume
    assert new_token is not None and new_token != old_token

    await repo.release(started.subject, old_token)  # A termina atrasado e tenta soltar
    assert await repo.claim(started.subject) is None  # o claim de B continua de pé

    await repo.release(started.subject, new_token)
    assert await repo.claim(started.subject) is not None


async def test_unlisted_check_does_not_change_the_title_in_latest(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    first = await repo.begin_check(user_id, link(), LIMITS)
    assert isinstance(first, Started)

    def new_check(title: str, *, listed: bool) -> NewCheck:
        return NewCheck(
            subject=first.subject,
            user_id=user_id,
            veredito="falso",
            confianca=80,
            result={"resumo": "r", "title": title},
            model="m",
            listed=listed,
            title=title,
        )

    listed = await repo.save_result(new_check("Título original", listed=True))
    unlisted = await repo.save_result(new_check("Título trocado e ofensivo", listed=False))

    latest = {item.public_id: item for item in await repo.latest(None, 1000)}
    assert latest[listed.public_id].title == "Título original"
    assert unlisted.public_id not in latest
    # A página da checagem não listada mostra o título dela mesma.
    stored = await repo.get(unlisted.public_id)
    assert stored is not None and stored.title == "Título trocado e ofensivo"


async def test_latest_reports_how_many_agency_checks_each_item_has(session: AsyncSession) -> None:
    # "Últimas" precisa saber se houve checagem de agência para mostrar "Ainda sem checagem".
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    agency = {
        "agencia": "Lupa",
        "titulo": "t",
        "veredito": "Falso",
        "url": "https://lupa.uol.com.br/x",
    }
    stored = {}
    # null (ou qualquer coisa que não seja lista) não pode derrubar "Últimas" com 500.
    cases: tuple[tuple[str, object], ...] = (
        ("sem", []),
        ("com", [agency, agency]),
        ("nulo", None),
        ("objeto", {"x": 1}),
    )
    for name, agencies in cases:
        started = await repo.begin_check(user_id, link(), LIMITS)
        assert isinstance(started, Started)
        stored[name] = await repo.save_result(
            NewCheck(
                subject=started.subject,
                user_id=user_id,
                veredito="nao_verificavel",
                confianca=30,
                result={"resumo": "r", "checagens_agencias": agencies},
                model="m",
                listed=True,
                title="Título",
            )
        )
    latest = {item.public_id: item for item in await repo.latest(None, 1000)}
    assert latest[stored["sem"].public_id].agency_checks == 0
    assert latest[stored["com"].public_id].agency_checks == 2
    assert latest[stored["nulo"].public_id].agency_checks == 0
    assert latest[stored["objeto"].public_id].agency_checks == 0
