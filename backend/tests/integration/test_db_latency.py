"""Latência do banco (13/10/2026): Render na Virginia, Supabase em São Paulo, ~100 ms por
viagem. Cada transação custava ~10 viagens (~1 s); o ping de 1 viagem e menos transações por
requisição cortam isso. Postgres real (o pool e o ping são do SQLAlchemy de verdade)."""

import logging

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from tests.integration.conftest import TEST_DATABASE_URL

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def _pid(session: AsyncSession) -> int:
    async with session.begin():
        return int(await session.scalar(text("select pg_backend_pid()")))


async def test_pool_reconnects_when_the_server_dropped_the_connection(
    session: AsyncSession,
) -> None:
    """Supavisor ou rede derrubam uma conexão ociosa: o ping percebe, o pool abre outra e a
    requisição segue (sem 500)."""
    old = await _pid(session)
    killer = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await killer.execute("select pg_terminate_backend($1)", old)
    finally:
        await killer.close()

    new = await _pid(session)

    assert new != old


async def test_pooled_connection_is_reused_between_requests(session: AsyncSession) -> None:
    assert await _pid(session) == await _pid(session)


async def test_new_connection_is_logged_with_its_cost(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Em produção, este log mostra se as conexões estão sendo reaproveitadas (deve ser raro)
    e quanto custa abrir uma (TCP + TLS + autenticação)."""
    caplog.set_level(logging.INFO, logger="app.db")
    await db.dispose_engine()

    async with db.get_engine().connect() as conn:
        await conn.execute(text("select 1"))

    records = [r for r in caplog.records if r.getMessage() == "db_new_connection"]
    assert len(records) == 1
    assert records[0].connect_ms >= 0  # type: ignore[attr-defined]
