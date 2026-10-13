"""/health/ready confere o schema: falta de migration vira 503 com a lista (falha cedo)."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.routers import health

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def ready(app: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.get("/health/ready")


async def test_ready_with_all_migrations(session: AsyncSession) -> None:
    response = await ready(create_app())
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "schema": "ok"}


async def test_missing_migration_fails_readiness(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        health.SCHEMA_MARKERS, "20991231000000_futura", ("column", "public.comments", "nao_existe")
    )
    monkeypatch.setitem(
        health.SCHEMA_MARKERS, "20991231000001_outra", ("table", "public.tabela_futura", None)
    )

    response = await ready(create_app())

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "schema_desatualizado"
    assert error["migrations_pendentes"] == ["20991231000000_futura", "20991231000001_outra"]
