import asyncio
import contextlib
import hashlib
import os
import re
from datetime import date, datetime
from uuid import UUID, uuid4

import asyncpg
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
    new_public_id,
)
from tests.integration.conftest import create_auth_user

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PUBLIC_ID = re.compile(r"^[0-9a-hjkmnp-tv-z]{12}$")
VERSION = "v-teste"
LIMITS = Limits(cache_hours=24, checks_per_hour=10, lookups_per_hour=60, prompt_version=VERSION)


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
            prompt_version=VERSION,
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
    token = started.claim_token  # o begin_check já toma o claim
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
    assert started.claim_token is not None
    assert await repo.claim(started.subject) is None
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
    one_per_hour = Limits(
        cache_hours=24, checks_per_hour=1, lookups_per_hour=60, prompt_version=VERSION
    )

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
    two_lookups = Limits(
        cache_hours=24, checks_per_hour=10, lookups_per_hour=2, prompt_version=VERSION
    )
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
    two_per_day = Limits(
        cache_hours=24,
        checks_per_hour=10,
        lookups_per_hour=60,
        checks_per_day=2,
        prompt_version=VERSION,
    )
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

    old_token = started.claim_token
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
            prompt_version=VERSION,
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
        ("torto", [{"veredito": 5}, {"x": 1}, "texto"]),
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
                prompt_version=VERSION,
            )
        )
    latest = {item.public_id: item for item in await repo.latest(None, 1000)}
    assert latest[stored["sem"].public_id].agency_checks == 0
    assert latest[stored["com"].public_id].agency_checks == 2
    assert latest[stored["nulo"].public_id].agency_checks == 0
    assert latest[stored["objeto"].public_id].agency_checks == 0
    # As classificações vão junto: a leitura reaplica a regra do "verdadeiro" (11/10/2026).
    assert latest[stored["com"].public_id].agency_ratings == ("Falso", "Falso")
    for name in ("sem", "nulo", "objeto", "torto"):
        assert latest[stored[name].public_id].agency_ratings == (), name


async def test_cache_is_keyed_by_prompt_version(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    v1 = Limits(cache_hours=24, checks_per_hour=10, lookups_per_hour=60, prompt_version="v1")
    v2 = Limits(cache_hours=24, checks_per_hour=10, lookups_per_hour=60, prompt_version="v2")
    subject = link()
    started = await repo.begin_check(user_id, subject, v1)
    assert isinstance(started, Started)
    stored = await repo.save_result(
        NewCheck(
            subject=started.subject,
            user_id=user_id,
            veredito="falso",
            confianca=80,
            result={"resumo": "r"},
            model="m",
            listed=True,
            title="Título",
            prompt_version="v1",
        )
    )

    assert isinstance(await repo.begin_check(user_id, subject, v1), CacheHit)
    assert (await repo.find_fresh(started.subject, 24, "v1")) is not None
    # Outra versão do prompt: não é cache, mas a checagem antiga continua pelo id.
    assert isinstance(await repo.begin_check(user_id, subject, v2), Started)
    assert (await repo.find_fresh(started.subject, 24, "v2")) is None
    assert (await repo.get(stored.public_id)) is not None


async def test_rows_from_before_prompt_versions_are_not_cache(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    started = await repo.begin_check(user_id, link(), LIMITS)
    assert isinstance(started, Started)
    stored = await finish(repo, started, user_id)
    async with session.begin():
        await session.execute(
            text("update public.checks set prompt_version = null where public_id = :p"),
            {"p": stored.public_id},
        )
    assert (await repo.find_fresh(started.subject, 24, LIMITS.prompt_version)) is None


async def test_previous_analysis_of_any_prompt_version(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    started = await repo.begin_check(user_id, link(), LIMITS)
    assert isinstance(started, Started)
    stored = await finish(repo, started, user_id)

    previous = await repo.find_previous(started.subject, 24)
    assert previous is not None and previous.public_id == stored.public_id
    assert previous.prompt_version == VERSION
    assert (await repo.find_fresh(started.subject, 24, "outra-versao")) is None


async def test_latest_has_one_item_per_link(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    subject = link()
    first = await repo.begin_check(user_id, subject, LIMITS)
    assert isinstance(first, Started)
    older = await finish(repo, first, user_id)
    newer = await finish(repo, first, user_id)  # mesmo link, análise nova (versão nova)

    ids = [item.public_id for item in await repo.latest(None, 1000)]
    assert newer.public_id in ids
    assert older.public_id not in ids


async def test_pool_reuses_the_connection_and_recovers_from_a_dropped_one(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings
    from app.db import dispose_engine

    monkeypatch.setenv("DB_POOL_SIZE", "2")
    get_settings.cache_clear()
    await dispose_engine()
    engine = get_engine()

    async def backend_pid() -> int:
        async with engine.connect() as conn:
            return int((await conn.execute(text("select pg_backend_pid()"))).scalar_one())

    first = await backend_pid()
    assert await backend_pid() == first  # reaproveitou a conexão (pool, LIFO)

    # O servidor derruba a conexão ociosa (como o Supavisor faz): o pre-ping refaz.
    killer = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
    try:
        await killer.execute("select pg_terminate_backend($1)", first)
    finally:
        await killer.close()
    assert await backend_pid() != first

    # Query cancelada no meio não deixa a próxima conexão quebrada.
    async def slow() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("select pg_sleep(5)"))

    task = asyncio.create_task(slow())
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert await backend_pid() > 0
    await dispose_engine()


# ---------- begin_check com o claim (ensaio de 12/10/2026) ----------


async def test_two_users_same_new_link_only_one_takes_the_claim(session: AsyncSession) -> None:
    subject = link()
    users = [await new_user(session), await new_user(session)]

    async def begin(user_id: UUID) -> Started | CacheHit:
        async with other_session() as own:
            return await SqlCheckRepository(own).begin_check(user_id, subject, LIMITS)

    results = await asyncio.gather(*(begin(u) for u in users))

    assert all(isinstance(r, Started) for r in results)
    tokens = [r.claim_token for r in results if isinstance(r, Started)]
    assert sum(t is not None for t in tokens) == 1  # single-flight
    assert len({r.subject for r in results if isinstance(r, Started)}) == 1  # mesmo link


async def test_save_in_progress_sends_the_other_request_to_wait(session: AsyncSession) -> None:
    # A está gravando (sem commit). B não espera o lock: vê o claim de A ativo, recebe
    # Started sem claim e vai para a espera, que acha o resultado depois do commit de A.
    # (O caso de A commitar exatamente entre o 2º e o 3º comando de B, em que B reconfere o
    # cache, está em test_sql_repositories e no smoke.)
    repo = SqlCheckRepository(session)
    public_id = new_public_id()
    first, second = await new_user(session), await new_user(session)
    subject = link()
    started = await repo.begin_check(first, subject, LIMITS)
    assert isinstance(started, Started) and started.claim_token is not None

    async with other_session() as saver:
        async with saver.begin():
            await saver.execute(
                text("update public.links set checking_until = null where id = :id"),
                {"id": started.subject.id},
            )
            await saver.execute(
                text(
                    "insert into public.checks (link_id, veredito, confianca, result, model, "
                    "created_by, public_id, listed, prompt_version) values (:id, 'falso', 80, "
                    "'{}'::jsonb, 'm', :uid, :pid, true, :v)"
                ),
                {"id": started.subject.id, "uid": first, "pid": public_id, "v": VERSION},
            )
            async with other_session() as own:
                waiting = await SqlCheckRepository(own).begin_check(second, subject, LIMITS)
            assert isinstance(waiting, Started) and waiting.claim_token is None

    fresh = await repo.find_fresh(started.subject, 24, VERSION)  # 1ª consulta da espera
    assert fresh is not None and fresh.public_id == public_id


async def test_agencies_only_result_is_never_the_previous_analysis(session: AsyncSession) -> None:
    # Modo "só agências" (12/10/2026): não é análise de IA, então não serve de "anterior".
    from app.repositories.checks import AGENCIES_ONLY_VERSION

    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    started = await repo.begin_check(user_id, link(), LIMITS)
    assert isinstance(started, Started)
    await repo.save_result(
        NewCheck(
            prompt_version=AGENCIES_ONLY_VERSION,
            subject=started.subject,
            user_id=user_id,
            veredito="nao_verificavel",
            confianca=0,
            result={"modo": "agencias", "resumo": "Análise automática indisponível agora."},
            model=AGENCIES_ONLY_VERSION,
            listed=False,
            claim_token=started.claim_token,
        )
    )

    assert await repo.find_previous(started.subject, 24) is None
    assert await repo.find_fresh(started.subject, 24, VERSION) is None


async def test_gemini_unit_can_be_given_back(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    async with session.begin():
        await session.execute(text("delete from public.gemini_usage"))
    assert await repo.reserve_gemini("checagem", 1) is True
    assert await repo.reserve_gemini("checagem", 1) is False

    await repo.release_gemini("checagem")
    await repo.release_gemini("checagem")  # nunca abaixo de zero

    assert await repo.reserve_gemini("checagem", 1) is True


async def test_request_becomes_a_lookup_only_under_the_lookup_limit(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    two_lookups = Limits(
        cache_hours=24, checks_per_hour=10, lookups_per_hour=2, prompt_version=VERSION
    )
    request_ids = []
    for _ in range(3):
        started = await repo.begin_check(user_id, link(), two_lookups)
        assert isinstance(started, Started)
        request_ids.append(started.request_id)

    assert await repo.convert_to_lookup(request_ids[0], user_id, 2) is None
    assert await repo.convert_to_lookup(request_ids[1], user_id, 2) is None
    retry = await repo.convert_to_lookup(request_ids[2], user_id, 2)
    assert retry is not None and 0 < retry <= 3600


async def test_daily_counter_per_model(session: AsyncSession) -> None:
    # RPD por modelo (nível gratuito): o esgotado é pulado sem chamar o Google.
    repo = SqlCheckRepository(session)
    async with session.begin():
        await session.execute(text("delete from public.gemini_model_usage"))

    assert await repo.reserve_gemini_model("gemini-3.8-flash", 2) is not None
    assert await repo.reserve_gemini_model("gemini-3.8-flash", 2) is not None
    assert await repo.reserve_gemini_model("gemini-3.8-flash", 2) is None  # 2 do dia, fim
    assert await repo.reserve_gemini_model("gemini-3.7-flash", 2) is not None  # outro modelo
    assert await repo.reserve_gemini_model("gemini-3.6-flash", 0) is None  # sem cota


async def test_agencies_only_result_is_never_served_as_cache(session: AsyncSession) -> None:
    """Resultado "só agências" (Gemini fora) não vira cache de 24 h: quando o Gemini voltar,
    a próxima checagem do mesmo assunto faz a análise de IA (13/10/2026)."""
    from app.repositories.checks import AGENCIES_ONLY_VERSION

    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    subject = link()
    started = await repo.begin_check(user_id, subject, LIMITS)
    assert isinstance(started, Started)
    await repo.save_result(
        NewCheck(
            subject=started.subject,
            user_id=user_id,
            veredito="nao_verificavel",
            confianca=0,
            result={"resumo": "só agências"},
            model="agencias",
            listed=False,
            title="Título",
            prompt_version=AGENCIES_ONLY_VERSION,
        )
    )

    again = await repo.begin_check(user_id, subject, LIMITS)

    assert isinstance(again, Started)  # análise nova, não cache
    assert await repo.find_fresh(started.subject, 24, VERSION) is None
    assert await repo.find_previous(started.subject, 24) is None


async def _model_calls(session: AsyncSession, day: object, model: str) -> int | None:
    async with session.begin():
        return await session.scalar(
            text("select calls from public.gemini_model_usage where day = :d and model = :m"),
            {"d": day, "m": model},
        )


async def test_model_unit_is_refunded_on_the_reservation_day(session: AsyncSession) -> None:
    """503/500: o Google recusou sem processar, a unidade volta (decisão de 13/10/2026). A
    devolução vai para a linha do dia da RESERVA (dia do Pacífico), nunca abaixo de 0."""
    from datetime import timedelta

    repo = SqlCheckRepository(session)
    async with session.begin():
        await session.execute(text("delete from public.gemini_model_usage"))
        pacific_today = await session.scalar(
            text("select (now() at time zone 'America/Los_Angeles')::date")
        )

    day = await repo.reserve_gemini_model("gemini-3.8-flash", 20)
    assert day == pacific_today
    await repo.reserve_gemini_model("gemini-3.8-flash", 20)
    assert await _model_calls(session, day, "gemini-3.8-flash") == 2

    await repo.release_gemini_model("gemini-3.8-flash", day)
    assert await _model_calls(session, day, "gemini-3.8-flash") == 1

    # Outro dia (a chamada cruzou a meia-noite do Pacífico): não mexe no dia de hoje.
    await repo.release_gemini_model("gemini-3.8-flash", day - timedelta(days=1))
    assert await _model_calls(session, day, "gemini-3.8-flash") == 1

    await repo.release_gemini_model("gemini-3.8-flash", day)
    await repo.release_gemini_model("gemini-3.8-flash", day)  # já em 0: fica em 0
    assert await _model_calls(session, day, "gemini-3.8-flash") == 0


class _Begins:
    """Conta as transações (BEGIN) abertas no engine real durante o bloco."""

    def __enter__(self) -> list[object]:
        from sqlalchemy import event

        from app.db import get_engine

        self.items: list[object] = []
        self.engine = get_engine().sync_engine

        def count(conn: object) -> None:
            self.items.append(conn)

        self.count = count
        event.listen(self.engine, "begin", count)
        return self.items

    def __exit__(self, *_: object) -> None:
        from sqlalchemy import event

        event.remove(self.engine, "begin", self.count)


async def _usage(session: AsyncSession) -> tuple[int, int]:
    async with session.begin():
        glob = await session.scalar(
            text(
                "select coalesce(sum(calls), 0) from public.gemini_usage where purpose = 'checagem'"
            )
        )
        model = await session.scalar(
            text("select coalesce(sum(calls), 0) from public.gemini_model_usage")
        )
    return int(glob), int(model)


async def _reset_usage(session: AsyncSession) -> None:
    async with session.begin():
        await session.execute(text("delete from public.gemini_usage"))
        await session.execute(text("delete from public.gemini_model_usage"))


async def test_global_and_model_quota_in_one_transaction(session: AsyncSession) -> None:
    """Cada transação custa ~0,7 s entre Virginia e São Paulo: as duas reservas vão juntas."""
    from app.repositories.checks import GLOBAL_QUOTA_REFUSED

    repo = SqlCheckRepository(session)
    await _reset_usage(session)

    with _Begins() as begins:
        day = await repo.reserve_gemini_and_model("checagem", 10, "gemini-3.8-flash", 20)

    assert day is not None and day is not GLOBAL_QUOTA_REFUSED
    assert len(begins) == 1
    assert await _usage(session) == (1, 1)


async def test_global_quota_refused_spends_nothing(session: AsyncSession) -> None:
    from app.repositories.checks import GLOBAL_QUOTA_REFUSED

    repo = SqlCheckRepository(session)
    await _reset_usage(session)
    await repo.reserve_gemini_and_model("checagem", 1, "gemini-3.8-flash", 20)

    refused = await repo.reserve_gemini_and_model("checagem", 1, "gemini-3.7-flash", 20)

    assert refused is GLOBAL_QUOTA_REFUSED
    assert await _usage(session) == (1, 1)  # o 3.7 não gastou nada


async def test_model_quota_refused_rolls_back_the_global_unit(session: AsyncSession) -> None:
    repo = SqlCheckRepository(session)
    await _reset_usage(session)
    await repo.reserve_gemini_and_model("checagem", 10, "gemini-3.8-flash", 1)

    refused = await repo.reserve_gemini_and_model("checagem", 10, "gemini-3.8-flash", 1)

    assert refused is None
    assert await _usage(session) == (1, 1)  # a unidade global da 2ª tentativa voltou


async def test_agencies_only_lookup_and_save_in_one_transaction(session: AsyncSession) -> None:
    from app.repositories.checks import AGENCIES_ONLY_VERSION

    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    started = await repo.begin_check(user_id, link(), LIMITS)
    assert isinstance(started, Started)
    check = NewCheck(
        subject=started.subject,
        user_id=user_id,
        veredito="nao_verificavel",
        confianca=0,
        result={"resumo": "só agências"},
        model=AGENCIES_ONLY_VERSION,
        listed=False,
        title="Título",
        claim_token=started.claim_token,
        prompt_version=AGENCIES_ONLY_VERSION,
    )

    with _Begins() as begins:
        saved = await repo.save_lookup_result(started.request_id, user_id, 60, check)

    assert len(begins) == 1
    assert not isinstance(saved, int) and saved.prompt_version == AGENCIES_ONLY_VERSION
    async with session.begin():
        cache_hit = await session.scalar(
            text("select cache_hit from public.check_requests where id = :id"),
            {"id": started.request_id},
        )
    assert cache_hit is True  # virou consulta


async def test_agencies_only_over_the_lookup_limit_saves_nothing(session: AsyncSession) -> None:
    from app.repositories.checks import AGENCIES_ONLY_VERSION

    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    started = await repo.begin_check(user_id, link(), LIMITS)
    assert isinstance(started, Started)
    check = NewCheck(
        subject=started.subject,
        user_id=user_id,
        veredito="nao_verificavel",
        confianca=0,
        result={},
        model=AGENCIES_ONLY_VERSION,
        listed=False,
        title=None,
        claim_token=started.claim_token,
        prompt_version=AGENCIES_ONLY_VERSION,
    )

    retry = await repo.save_lookup_result(started.request_id, user_id, 0, check)

    assert isinstance(retry, int) and retry >= 1
    assert await repo.find_previous(started.subject, 24) is None
    async with session.begin():
        saved = await session.scalar(
            text("select count(*) from public.checks where link_id = :sid"),
            {"sid": started.subject.id},
        )
    assert saved == 0  # nada gravado para este assunto
