"""Testes contra Postgres real. Rodam só quando TEST_DATABASE_URL existe (na CI).

Formato: postgresql://postgres:postgres@localhost:5432/postgres (sem TLS).
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import dispose_engine, get_engine

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = Path(__file__).with_name("bootstrap.sql")
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    integration = [item for item in items if "integration" in item.keywords]
    if TEST_DATABASE_URL or not integration:
        return
    # Na CI (o GitHub define CI=true) pular seria um falso verde: falha de propósito.
    if os.environ.get("CI"):
        raise pytest.UsageError("TEST_DATABASE_URL é obrigatória na CI (testes de integração)")
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL não definida (roda só na CI)")
    for item in integration:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
async def migrated_database() -> None:
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await conn.execute(BOOTSTRAP.read_text(encoding="utf-8"))
        already = await conn.fetchval("select to_regclass('public.profiles') is not null")
        if not already:
            for migration in MIGRATIONS:
                await conn.execute(migration.read_text(encoding="utf-8"))
    finally:
        await conn.close()


@pytest.fixture
async def session(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSession]:
    # Usa o engine real da aplicação (NullPool, sem cache de prepared statements).
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("DATABASE_SSL", "false")
    monkeypatch.setenv("DATABASE_SSL_ROOT_CERT", "")
    get_settings.cache_clear()
    await dispose_engine()
    async with async_sessionmaker(get_engine(), expire_on_commit=False)() as db_session:
        yield db_session
    await dispose_engine()
    get_settings.cache_clear()


async def create_auth_user(session: AsyncSession, *, google_sub: str | None = None) -> UUID:
    """Cria um usuário em auth.users (e a identidade Google, se pedida)."""
    user_id = uuid4()
    async with session.begin():
        await session.execute(text("insert into auth.users (id) values (:id)"), {"id": user_id})
        if google_sub:
            await session.execute(
                text(
                    "insert into auth.identities (user_id, provider, provider_id) "
                    "values (:id, 'google', :sub)"
                ),
                {"id": user_id, "sub": google_sub},
            )
    return user_id
