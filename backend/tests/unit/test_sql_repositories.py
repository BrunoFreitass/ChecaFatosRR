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
LIMITS = Limits(
    cache_hours=24, checks_per_hour=10, lookups_per_hour=60, checks_per_day=20, prompt_version="v1"
)
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
    "prompt_version": "v1",
}


NO_CHECK = dict.fromkeys(CHECK_ROW)


def state(
    *,
    lookups: int = 0,
    daily: int = 0,
    hourly: int = 0,
    busy: bool = False,
    fresh: bool = False,
) -> list[dict[str, object]]:
    """Linha do 2º comando do begin_check: uso nas janelas, claim ativo e cache."""
    return [
        {
            "lookups_used": lookups,
            "lookups_retry": 120,
            "daily_used": daily,
            "daily_retry": 3600,
            "hourly_used": hourly,
            "hourly_retry": 600,
            "busy": busy,
            **(CHECK_ROW if fresh else NO_CHECK),
        }
    ]


def session_for(
    *, subject_id: int | None = 7, token: datetime | None = NOW, **usage: object
) -> FakeAsyncSession:
    session = FakeAsyncSession()
    session.on("with lock as", rows=[{"locked": 1, "id": subject_id}])
    session.on("from public.check_requests where user_id", rows=state(**usage))  # type: ignore[arg-type]
    session.on("with r as", rows=[{"request_id": 99, "token": token}])
    session.on("insert into public.check_requests", scalar=98)  # consulta ao cache
    return session


# ---------- begin_check ----------


async def test_begin_check_new_link_takes_the_claim_in_three_statements() -> None:
    session = session_for()

    outcome = await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)

    assert outcome == Started(SubjectRef("link", 7), 99, NOW)
    log = session.sql_log()
    assert "for no key update" in log[0]  # o lock por usuário vem antes de tudo
    assert "insert into public.links" in log[0]  # e o assunto, no mesmo comando
    assert "count(*) filter" in log[1] and "left join lateral" in log[1]
    assert "set checking_until = clock_timestamp()" in log[2]  # pedido + claim juntos
    assert len(log) == 3
    assert session.transactions == 1  # uma transação só


async def test_begin_check_when_someone_else_is_checking() -> None:
    session = session_for(token=None, busy=True)

    outcome = await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)

    assert outcome == Started(SubjectRef("link", 7), 99, None)  # vai esperar o outro
    assert len(session.sql_log()) == 3


async def test_claim_freed_meanwhile_rechecks_the_cache() -> None:
    # Havia claim ativo no 2º comando e ele acabou antes do nosso: o outro pode ter salvo.
    session = session_for(busy=True)
    session.on("order by c.created_at desc limit 1", rows=[CHECK_ROW])

    outcome = await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)

    assert isinstance(outcome, CacheHit)
    reuse = session.statements[-1]
    assert "set cache_hit = true" in reuse.sql and "checking_until = null" in reuse.sql
    assert reuse.params == {"rid": 99, "sid": 7, "token": NOW}
    assert session.transactions == 1


async def test_begin_check_text_inserted_concurrently() -> None:
    session = session_for(subject_id=None)
    session.on("select id from public.texts where text_hash", scalar=12)  # 2ª busca: acha
    subject = TextSubject(body="Mensagem encaminhada", text_hash=b"t" * 32)

    outcome = await SqlCheckRepository(session).begin_check(USER, subject, LIMITS)

    assert outcome == Started(SubjectRef("texto", 12), 99, NOW)
    assert "insert into public.texts" in session.sql_log()[0]


@pytest.mark.parametrize(
    ("daily", "hourly", "kind", "retry"),
    [(20, 0, "diario", 3600), (20, 10, "diario", 3600), (5, 10, "checagens", 600)],
)
async def test_begin_check_limits(daily: int, hourly: int, kind: str, retry: int) -> None:
    session = session_for(daily=daily, hourly=hourly)

    with pytest.raises(LimitExceededError) as exc:
        await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)

    assert exc.value.kind == kind  # os dois estouraram: o diário (Retry-After maior)
    assert exc.value.retry_after_seconds == retry
    assert not any("insert into public.check_requests" in sql for sql in session.sql_log())


async def test_begin_check_cache_hit_counts_only_lookups() -> None:
    session = session_for(fresh=True, daily=20, hourly=10)  # tetos de checagem não contam

    outcome = await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)

    assert isinstance(outcome, CacheHit)
    assert outcome.check.result == {"resumo": "r"}
    insert = session.statements[-1]
    assert insert.params["cache_hit"] is True
    assert not any("checking_until" in sql for sql in session.sql_log()[2:])  # sem claim
    assert len(session.sql_log()) == 3


async def test_cache_lookup_limit() -> None:
    session = session_for(fresh=True, lookups=60)

    with pytest.raises(LimitExceededError) as exc:
        await SqlCheckRepository(session).begin_check(USER, LINK, LIMITS)
    assert exc.value.kind == "consultas"
    assert exc.value.retry_after_seconds == 120


# ---------- demais métodos ----------


async def test_claim_release_quota_refund() -> None:
    session = FakeAsyncSession()
    session.on("set checking_until = clock_timestamp() + make_interval", scalar=NOW, once=True)
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

    assert (await repo.find_fresh(SubjectRef("link", 7), 24, "v1")) is not None
    fresh_query = next(s for s in session.statements if "make_interval(hours" in s.sql)
    assert "c.prompt_version = :prompt_version" in fresh_query.sql
    assert fresh_query.params["prompt_version"] == "v1"
    stored = await repo.get("0123456789ab")
    assert stored is not None and stored.published_at == date(2026, 9, 28)
    first = await repo.latest(None, 20)
    assert [item.public_id for item in first] == ["0123456789ab"]
    assert first[0].agency_checks == 0
    assert "jsonb_typeof" in session.sql_log()[-1]
    await repo.latest((NOW, "0123456789ab"), 20)
    assert "(c.created_at, c.public_id) <" in session.sql_log()[-1]
    # Um item por link sem varrer tudo: anti-join que o índice parcial atende.
    assert "not exists" in session.sql_log()[-1]
    assert "distinct on" not in session.sql_log()[-1]

    previous = await repo.find_previous(SubjectRef("link", 7), 24)
    assert previous is not None
    query = next(s for s in reversed(session.statements) if "make_interval(hours" in s.sql)
    assert "prompt_version = :" not in query.sql  # qualquer versão do prompt


async def test_save_result() -> None:
    session = FakeAsyncSession()
    session.on("insert into public.checks", rows=[CHECK_ROW])

    stored = await SqlCheckRepository(session).save_result(
        NewCheck(
            prompt_version="v1",
            subject=SubjectRef("link", 7),
            user_id=USER,
            veredito="falso",
            confianca=80,
            result={"resumo": "r"},
            model="gemini-teste",
            listed=True,
            title="Título",
            published_at=date(2026, 9, 28),
            claim_token=NOW,
        )
    )

    assert stored.public_id == "0123456789ab"
    assert stored.published_at == date(2026, 9, 28)
    [save] = session.statements  # um comando só: solta o claim, título e insert
    assert "update public.links" in save.sql and "insert into public.checks" in save.sql
    assert save.params["result"] == '{"resumo": "r"}'
    assert save.params["token"] == NOW and save.params["listed"] is True
    assert session.transactions == 1


async def test_save_text_result_has_no_link_fields() -> None:
    session = FakeAsyncSession()
    session.on("insert into public.checks", rows=[{**CHECK_ROW, "kind": "texto"}])

    await SqlCheckRepository(session).save_result(
        NewCheck(
            prompt_version="v1",
            subject=SubjectRef("texto", 3),
            user_id=USER,
            veredito="falso",
            confianca=80,
            result={"resumo": "r"},
            model="gemini-teste",
            listed=False,
        )
    )

    [save] = session.statements
    assert "update public.texts" in save.sql and "text_id" in save.sql
    assert "title" not in save.params


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
