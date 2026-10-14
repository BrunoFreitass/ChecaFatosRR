"""Mapa por UF no Postgres real: claim em lote sem sobreposição e gravação só do mais novo."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_engine
from app.repositories.tse import SqlTseMapRepository
from app.services.tse.mapa import UFS, UfSnapshot

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def _reset(session: AsyncSession) -> None:
    async with session.begin():
        await session.execute(
            text(
                "update public.tse_uf_snapshots set fetched_at = '-infinity', resumo = '{}', "
                "eleicao = null, tse_updated_at = null"
            )
        )


async def test_seed_has_the_27_states(session: AsyncSession) -> None:
    await _reset(session)
    assert sorted(s.uf for s in await SqlTseMapRepository(session).get_ufs()) == sorted(UFS)


async def test_parallel_claims_never_take_the_same_state(session: AsyncSession) -> None:
    await _reset(session)
    maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with maker() as one, maker() as two:
        first, second = await asyncio.gather(
            SqlTseMapRepository(one).claim_ufs(120, 9),
            SqlTseMapRepository(two).claim_ufs(120, 9),
        )

    assert not set(first) & set(second)
    assert len(first) + len(second) == 18
    # Recém-buscadas não voltam antes de 120 s: sobram 9.
    third = await SqlTseMapRepository(session).claim_ufs(120, 27)
    assert len(third) == 9 and not set(third) & (set(first) | set(second))


async def test_older_summary_never_overwrites_a_newer_one(session: AsyncSession) -> None:
    await _reset(session)
    repo = SqlTseMapRepository(session)
    new = datetime(2026, 10, 25, 20, 0, tzinfo=UTC)
    await repo.save_ufs([UfSnapshot("rr", "6258", {"candidatos": [{"numero": "13"}]}, new)])
    await repo.save_ufs([UfSnapshot("rr", "6258", {"candidatos": []}, new - timedelta(minutes=5))])

    rr = next(s for s in await repo.get_ufs() if s.uf == "rr")
    assert rr.resumo == {"candidatos": [{"numero": "13"}]} and rr.tse_updated_at == new

    # Outra eleição (fim do ensaio): grava mesmo com data mais antiga.
    await repo.save_ufs([UfSnapshot("rr", "6257", {"candidatos": []}, new - timedelta(days=20))])
    rr = next(s for s in await repo.get_ufs() if s.uf == "rr")
    assert rr.eleicao == "6257"
