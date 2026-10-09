"""Os repositórios SQL rodando sobre um AsyncSession falso (tests/fake_session.py).

Não validam o SQL (isso é da integração, com Postgres de verdade): validam o código Python
em volta — await em toda chamada ao banco, uso de mappings()/one()/first(), uma transação
por método e o caminho de cada decisão. Foi aqui que faltava teste para o bug do
`await session.execute(...).mappings()` em _check_usage.
"""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from app.repositories.checks import (
    CacheHit,
    LimitExceededError,
    Limits,
    LinkSubject,
    NewCheck,
    SqlCheckRepository,
    Started,
    SubjectRef,
    TextSubject,
)
from app.repositories.profiles import SqlProfileRepository
from tests.fake_session import FakeAsyncSession

pytestmark = pytest.mark.anyio

USER = uuid4()
LIMITS = Limits(cache_hours=24, checks_per_hour=10, lookups_per_hour=60, checks_per_day=20)
LINK = LinkSubject(url="https://site.com.br/a", url_hash=b"h" * 32, domain="site.com.br")
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)

CHECK_ROW = {
    "public_id": "0123456789ab",
    "kind": "link",
    "url": "https://site.com.br/a",
    "domain": "site.com.br",
    "title": "Título",
    "published_at": date(2026, 9, 28),
    "texto": None,
    "veredito": "falso",
    "confianca": 80,
    "result": '{"resumo": "r"}',  # asyncpg devolve jsonb como texto
    "created_at": NOW,
}


def usage(used: int, retry: int | None = None) -> list[dict[str, object]]:
    return [{"used": used, "retry": retry}]


def daily(params: dict[str, object]) -> bool:
    return params.get("hours") == 24


def hourly(params: dict[str, object]) -> bool:
    return params.get("hours") == 1


def session_for_new_link(*, daily_used: int = 0, hourly_used: int = 0) -> FakeAsyncSession:
    session = FakeAsyncSession()
    session.on("select id from public.links", scalar=None)
    session.on("insert into public.links", scalar=7)
    session.on("from public.check_requests", rows=usage(daily_used, 3600), when=daily)
    session.on("from public.check_requests", rows=usage(hourly_used, 600), when=hourly)
    session.on("insert into public.check_requests", scalar=99)
    return session


# ---------- begin_check ----------


async def test_begin_check_new_link() -> None:
    session = session_for_new_link()

    outcome = await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)

    assert outcome == Started(SubjectRef("link", 7), 99)
    assert "for no key update" in session.sql_log()[0]  # o lock por usuário vem antes de tudo
    assert session.transactions == 1  # uma transação só
    windows = [s.params["hours"] for s in session.statements if "count(*)" in s.sql]
    assert windows == [24, 1]  # diário antes do horário


async def test_begin_check_reuses_existing_link_without_insert() -> None:
    session = FakeAsyncSession()
    session.on("select id from public.links", scalar=7)
    session.on("from public.check_requests", rows=usage(0))
    session.on("insert into public.check_requests", scalar=99)

    outcome = await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)

    assert outcome == Started(SubjectRef("link", 7), 99)
    assert not any("insert into public.links" in sql for sql in session.sql_log())


async def test_begin_check_text_inserted_concurrently() -> None:
    session = FakeAsyncSession()
    session.on("select id from public.texts", scalar=None, once=True)  # 1ª busca: não existe
    session.on("insert into public.texts", scalar=None)  # outra transação inseriu antes
    session.on("select id from public.texts", scalar=12)  # 2ª busca: acha
    session.on("from public.check_requests", rows=usage(0))
    session.on("insert into public.check_requests", scalar=5)
    subject = TextSubject(body="Mensagem encaminhada", text_hash=b"t" * 32)

    outcome = await SqlCheckRepository(session).begin_check(USER, subject, LIMITS)

    assert outcome == Started(SubjectRef("texto", 12), 5)


@pytest.mark.parametrize(
    ("daily_used", "hourly_used", "kind", "retry"),
    [(20, 0, "diario", 3600), (5, 10, "checagens", 600)],
)
async def test_begin_check_limits(daily_used: int, hourly_used: int, kind: str, retry: int) -> None:
    session = session_for_new_link(daily_used=daily_used, hourly_used=hourly_used)

    with pytest.raises(LimitExceededError) as exc:
        await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)

    assert exc.value.kind == kind
    assert exc.value.retry_after_seconds == retry
    assert not any("insert into public.check_requests" in sql for sql in session.sql_log())


async def test_begin_check_cache_hit_counts_only_lookups() -> None:
    session = FakeAsyncSession()
    session.on("select id from public.links", scalar=7)
    session.on("from public.checks c", rows=[CHECK_ROW])
    session.on("from public.check_requests", rows=usage(0))
    session.on("insert into public.check_requests", scalar=99)

    outcome = await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)

    assert isinstance(outcome, CacheHit)
    assert outcome.check.result == {"resumo": "r"}
    usage_queries = [s for s in session.statements if "count(*)" in s.sql]
    assert [q.params["cache_hit"] for q in usage_queries] == [True]  # sem teto diário/horário


async def test_cache_lookup_limit() -> None:
    session = FakeAsyncSession()
    session.on("select id from public.links", scalar=7)
    session.on("from public.checks c", rows=[CHECK_ROW])
    session.on("from public.check_requests", rows=usage(60, 120))

    with pytest.raises(LimitExceededError) as exc:
        await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)
    assert exc.value.kind == "consultas"


# ---------- demais métodos ----------


async def test_claim_release_quota_refund() -> None:
    session = FakeAsyncSession()
    session.on("set checking_until = now() + make_interval", scalar=NOW, once=True)
    session.on("insert into public.gemini_usage", scalar=3, once=True)
    repo = SqlCheckRepository(session)
    ref = SubjectRef("link", 7)

    assert await repo.claim(ref) == NOW  # o token é o checking_until gravado
    assert await repo.claim(ref) is None  # a regra era de uso único: ocupado
    await repo.release(ref, NOW)
    release = next(s for s in session.statements if "checking_until = null" in s.sql)
    assert release.params["token"] == NOW  # só solta o próprio claim
    assert await repo.reserve_gemini("checagem", 200) is True
    assert await repo.reserve_gemini("checagem", 200) is False
    assert await repo.reserve_gemini("checagem", 0) is False  # nem vai ao banco
    await repo.refund(99)
    await repo.mark_cache_hit(99)
    assert session.transactions == 7  # uma por chamada que foi ao banco


async def test_find_fresh_get_and_latest() -> None:
    summary = {k: CHECK_ROW[k] for k in ("public_id", "domain", "title", "veredito", "created_at")}
    summary["agency_checks"] = 0
    session = FakeAsyncSession()
    # A regra mais específica primeiro: a consulta de "Últimas" também tem "from public.checks c".
    session.on("select c.public_id, l.domain, l.title, c.veredito, c.created_at", rows=[summary])
    session.on("from public.checks c", rows=[CHECK_ROW])
    repo = SqlCheckRepository(session)

    assert (await repo.find_fresh(SubjectRef("link", 7), 24)) is not None
    stored = await repo.get("0123456789ab")
    assert stored is not None and stored.published_at == date(2026, 9, 28)
    first = await repo.latest(None, 20)
    assert [item.public_id for item in first] == ["0123456789ab"]
    assert first[0].agency_checks == 0
    assert "jsonb_typeof" in session.sql_log()[-1]
    await repo.latest((NOW, "0123456789ab"), 20)
    assert "(c.created_at, c.public_id) <" in session.sql_log()[-1]


async def test_save_result() -> None:
    session = FakeAsyncSession()
    session.on("from public.checks c", rows=[CHECK_ROW])

    stored = await SqlCheckRepository(session).save_result(
        NewCheck(
            subject=SubjectRef("link", 7),
            user_id=USER,
            veredito="falso",
            confianca=80,
            result={"resumo": "r"},
            model="gemini-teste",
            listed=True,
            title="Título",
            published_at=date(2026, 9, 28),
        )
    )

    assert stored.public_id == "0123456789ab"
    insert = next(s for s in session.statements if "insert into public.checks" in s.sql)
    assert insert.params["result"] == '{"resumo": "r"}'
    assert session.transactions == 1


# ---------- perfis ----------


async def test_profile_repository_methods() -> None:
    profile_row = {
        "id": USER,
        "apelido": "eleitor_rr",
        "status": "active",
        "suspended_until": None,
        "terms_version": "2026-10-08",
        "created_at": NOW,
    }
    session = FakeAsyncSession()
    session.on("from public.profiles where id", rows=[profile_row])
    session.on("insert into public.profiles", rows=[profile_row])
    session.on("select exists(select 1 from public.profiles", scalar=True)
    session.on("from auth.identities", scalar="google-sub")
    session.on("from public.banned_identities", scalar=False)
    repo = SqlProfileRepository(session)

    assert (await repo.get(USER)) is not None
    assert await repo.nickname_taken("Eleitor_RR") is True
    assert await repo.google_sub(USER) == "google-sub"
    assert await repo.is_banned(b"x" * 32) is False
    assert (await repo.create(USER, "eleitor_rr", "2026-10-08")).apelido == "eleitor_rr"
    await repo.log_nickname_blocked("apelido", "palavrao", "regra")
