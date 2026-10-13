"""Idas ao banco de uma checagem (ensaio em produção, 12/10/2026: db_ms=5252 no check_timing).

O Render (Virginia) fala com o Supabase (São Paulo) a ~130 ms por ida, e cada transação
paga também o pre-ping do pool, o BEGIN e o COMMIT. Meta: etapa "db" abaixo de 1,5 s, ou
seja, poucas transações com poucos comandos cada.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine
from app.repositories.checks import SqlCheckRepository
from app.services.check_service import CheckOutcome, CheckTools, run_check
from tests.fakes import FakeAnalyzer, FakeFactCheck, FakeFetcher
from tests.integration.test_checks_db import new_user
from tests.smoke.test_checks import settings

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@dataclass
class Trips:
    transactions: int = 0
    statements: list[str] = field(default_factory=list)


@contextmanager
def counting() -> Iterator[Trips]:
    engine = get_engine().sync_engine
    trips = Trips()

    def on_begin(_: Any) -> None:
        trips.transactions += 1

    def on_execute(_c: Any, _cur: Any, statement: str, *_: Any) -> None:
        trips.statements.append(" ".join(statement.split())[:80])

    event.listen(engine, "begin", on_begin)
    event.listen(engine, "before_cursor_execute", on_execute)
    try:
        yield trips
    finally:
        event.remove(engine, "begin", on_begin)
        event.remove(engine, "before_cursor_execute", on_execute)


async def check(session: AsyncSession, user_id: UUID, url: str) -> CheckOutcome:
    tools = CheckTools(fetcher=FakeFetcher(), factcheck=FakeFactCheck(), analyzer=FakeAnalyzer())
    return await run_check(
        user_id=user_id,
        url=url,
        texto=None,
        repo=SqlCheckRepository(session),
        tools=tools,
        settings=settings(),
    )


async def test_new_link_check_uses_few_round_trips(session: AsyncSession) -> None:
    user_id = await new_user(session)
    url = f"https://www.folhabv.com.br/eleicoes/{user_id.hex}"

    with counting() as trips:
        outcome = await check(session, user_id, url)

    assert not outcome.cached
    assert outcome.check.title == "Calendário do 2º turno é mantido em Roraima"
    # begin_check (com o claim) + cota global E do modelo numa transação (13/10/2026: antes
    # eram duas) + gravação. Antes de 12/10 eram 5 transações e 14 comandos; o analisador
    # falso agora reserva a cota do modelo, como o real (antes este teste não a via).
    assert trips.transactions <= 3, trips.statements
    assert len(trips.statements) <= 6, trips.statements


async def test_cache_hit_uses_one_transaction(session: AsyncSession) -> None:
    first, second = await new_user(session), await new_user(session)
    url = f"https://www.folhabv.com.br/eleicoes/{first.hex}"
    original = await check(session, first, url)

    with counting() as trips:
        outcome = await check(session, second, url)

    assert outcome.cached and outcome.check.public_id == original.check.public_id
    assert trips.transactions == 1, trips.statements
    assert len(trips.statements) <= 3, trips.statements
