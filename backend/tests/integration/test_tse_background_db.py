"""Apuração com recarga do TSE em segundo plano (PR tse-background) contra Postgres real.

Só o TSE é simulado: a recarga roda depois da resposta, com a própria sessão do banco
(session_scope), grava o snapshot e a próxima requisição já o recebe.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.routers.tse import get_tse_client

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

FIXTURE = Path(__file__).parents[1] / "fixtures" / "tse" / "br-c0001-e006257-u.json"


class Tse:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, scope: str) -> Any:
        self.calls.append(scope)
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        data["cdabr"], data["ele"] = scope, "6258"
        return data


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[tuple[httpx.AsyncClient, Any, Tse]]:
    async with session.begin():
        await session.execute(text("delete from public.tse_snapshots"))
    app = create_app()
    tse = Tse()
    app.dependency_overrides[get_tse_client] = lambda: tse
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, app, tse
    await app.state.tse_cache.idle()


async def test_background_refresh_saves_with_its_own_session(
    client: tuple[httpx.AsyncClient, Any, Tse], session: AsyncSession
) -> None:
    http, app, tse = client

    cold = await http.get("/tse/resultado")
    assert cold.status_code == 200
    assert [e["resultado"] for e in cold.json()["escopos"]] == [None, None]

    await app.state.tse_cache.idle()  # a recarga em segundo plano terminou
    async with session.begin():
        saved = await session.scalar(text("select count(*) from public.tse_snapshots"))
    warm = await http.get("/tse/resultado")

    assert sorted(tse.calls) == ["br", "rr"]
    assert saved == 2
    assert all(e["resultado"] is not None for e in warm.json()["escopos"])


async def test_restarted_process_serves_the_saved_snapshot_without_the_tse(
    client: tuple[httpx.AsyncClient, Any, Tse],
) -> None:
    http, app, _ = client
    await http.get("/tse/resultado")
    await app.state.tse_cache.idle()

    # Novo processo (Render acordou): memória vazia, snapshot no banco.
    restarted = create_app()
    other = Tse()
    restarted.dependency_overrides[get_tse_client] = lambda: other
    transport = httpx.ASGITransport(app=restarted)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as fresh:
        response = await fresh.get("/tse/resultado")
        assert all(e["resultado"] is not None for e in response.json()["escopos"])
        assert other.calls == []  # a resposta veio do banco, sem buscar o TSE
        await restarted.state.tse_cache.idle()
