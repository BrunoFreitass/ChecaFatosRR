"""tse_snapshots no Postgres real: claim atômico e gravação só do mais novo (etapa 5)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_engine
from app.repositories.tse import SqlTseRepository

pytestmark = pytest.mark.anyio

T1 = datetime(2026, 10, 25, 20, 0, tzinfo=UTC)
T2 = T1 + timedelta(minutes=1)


async def _reset(session: AsyncSession) -> None:
    async with session.begin():
        await session.execute(text("delete from public.tse_snapshots"))


async def test_claim_is_single_flight_per_scope(session: AsyncSession) -> None:
    await _reset(session)
    repo = SqlTseRepository(session)

    assert await repo.claim("br", 30) is True  # primeira vez: cria a linha e é dono
    assert await repo.claim("br", 30) is False  # dentro dos 30 s: ninguém mais busca
    assert await repo.claim("rr", 30) is True  # escopos independentes

    async with session.begin():
        await session.execute(
            text("update public.tse_snapshots set fetched_at = now() - interval '31 seconds'")
        )
    assert await repo.claim("br", 30) is True


async def test_concurrent_claims_have_one_winner(session: AsyncSession) -> None:
    await _reset(session)
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)

    async def claim() -> bool:
        async with factory() as own:
            return await SqlTseRepository(own).claim("br", 30)

    results = await asyncio.gather(*(claim() for _ in range(8)))
    assert results.count(True) == 1


async def test_save_keeps_only_the_newest_totalization(session: AsyncSession) -> None:
    await _reset(session)
    repo = SqlTseRepository(session)
    await repo.claim("br", 30)

    await repo.save("br", {"eleicao": "6258", "secoes_pct": 50.0}, T2)
    await repo.save("br", {"eleicao": "6258", "secoes_pct": 40.0}, T1)  # chegou atrasado

    [snapshot] = await repo.get_all()
    assert snapshot.payload["secoes_pct"] == 50.0
    assert snapshot.tse_updated_at == T2


async def test_save_replaces_a_snapshot_of_another_election(session: AsyncSession) -> None:
    # Ensaio com o 1º turno (6257) e troca para o 2º (6258): o novo grava mesmo sendo "antigo".
    await _reset(session)
    repo = SqlTseRepository(session)
    await repo.claim("br", 30)
    await repo.save("br", {"eleicao": "6257", "secoes_pct": 100.0}, T2)

    await repo.save("br", {"eleicao": "6258", "secoes_pct": 0.0}, T1)

    [snapshot] = await repo.get_all()
    assert snapshot.payload["eleicao"] == "6258"


async def test_claimed_row_without_data_is_not_a_result(session: AsyncSession) -> None:
    await _reset(session)
    repo = SqlTseRepository(session)
    await repo.claim("br", 30)  # o TSE falhou depois do claim: nada gravado

    [snapshot] = await repo.get_all()
    assert snapshot.payload == {}
