"""Cota de hoje lida pela avaliação (eval/rodar_avaliacao.uso_hoje): a mesma fonte do painel."""

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.painel import SqlPainelRepository
from eval.rodar_avaliacao import uso_hoje

pytestmark = pytest.mark.anyio

_INSERT = "insert into public.gemini_model_usage (day, model, calls) values (:d, :m, :c)"


async def test_reads_todays_calls_per_model_like_the_admin_panel(session: AsyncSession) -> None:
    async with session.begin():
        await session.execute(text("delete from public.gemini_model_usage"))
        today = await session.scalar(
            text("select (now() at time zone 'America/Los_Angeles')::date")
        )
        rows = [
            (today, "gemini-3.7-flash", 7),
            (today, "gemini-3.8-flash", 20),
            (today - timedelta(days=1), "gemini-3.7-flash", 19),  # ontem não conta
        ]
        for day, model, calls in rows:
            await session.execute(text(_INSERT), {"d": day, "m": model, "c": calls})

    used = await uso_hoje(session, ["gemini-3.7-flash", "gemini-3.6-flash"])

    assert used == {"gemini-3.7-flash": 7, "gemini-3.6-flash": 0}  # sem linha hoje = 0
    # O mesmo número que o painel admin mostra.
    painel = await SqlPainelRepository(session).read()
    assert painel.model_used["gemini-3.7-flash"] == used["gemini-3.7-flash"]
