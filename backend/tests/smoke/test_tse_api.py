"""GET /tse/resultado e GET /tse/status (etapa 5). TSE e banco simulados."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.repositories.tse import Snapshot
from app.routers.tse import get_now, get_tse_client, get_tse_repository

FIXTURE = Path(__file__).parents[1] / "fixtures" / "tse" / "br-c0001-e006257-u.json"


class Repo:
    def __init__(self) -> None:
        self.snapshots: dict[str, Snapshot] = {}

    async def claim(self, scope: str, stale_seconds: int) -> bool:
        return scope not in self.snapshots

    async def save(self, scope: str, payload: dict[str, Any], tse_updated_at: Any) -> None:
        self.snapshots[scope] = Snapshot(scope, payload, tse_updated_at, datetime.now(UTC))

    async def get_all(self) -> list[Snapshot]:
        return list(self.snapshots.values())


class Tse:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, scope: str) -> Any:
        self.calls += 1
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        data["cdabr"], data["ele"] = scope, "6258"
        return data


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tse() -> Tse:
    return Tse()


@pytest.fixture
def now() -> list[datetime]:
    return [datetime(2026, 10, 25, 12, 0, tzinfo=UTC)]


@pytest.fixture
def api(app: FastAPI, tse: Tse, now: list[datetime]) -> Iterator[TestClient]:
    repo = Repo()
    app.dependency_overrides[get_tse_repository] = lambda: repo
    app.dependency_overrides[get_tse_client] = lambda: tse
    app.dependency_overrides[get_now] = lambda: now[0]
    with TestClient(app) as client:
        yield client


def test_result_has_both_scopes_with_the_official_data(api: TestClient) -> None:
    response = api.get("/tse/resultado")

    assert response.status_code == 200
    body = response.json()
    assert body["fonte"] == "TSE"
    assert body["link_oficial"] == "https://resultados.tse.jus.br/"
    assert [e["escopo"] for e in body["escopos"]] == ["br", "rr"]
    br = body["escopos"][0]["resultado"]
    assert br["secoes_pct"] == 100.0
    assert br["candidatos"][0] == {
        "numero": "22",
        "nome": "FLAVIO BOLSONARO",
        "partido": "PL",
        "vice": "ALFREDO GASPAR",
        "votos": 56104503,
        "pct": 47.03,
        "situacao": "2º turno",
        "eleito": False,
    }
    assert br["totalizado_em"] == "2026-10-05T12:51:05-03:00"


def test_result_is_cacheable_for_15_seconds_with_etag(api: TestClient) -> None:
    first = api.get("/tse/resultado")

    assert first.headers["cache-control"] == "public, max-age=15"
    etag = first.headers["etag"]
    second = api.get("/tse/resultado", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag


def test_many_requests_reach_the_tse_once_per_scope(api: TestClient, tse: Tse) -> None:
    for _ in range(20):
        api.get("/tse/resultado")
    assert tse.calls == 2


def test_disabled_count_is_a_friendly_404(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TSE_ENABLED", "false")
    get_settings.cache_clear()

    response = api.get("/tse/resultado")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "apuracao_indisponivel"


# ---------- banner da home (decisão de 11/10/2026) ----------


@pytest.mark.parametrize(
    ("when", "shown"),
    [
        (datetime(2026, 10, 25, 19, 59, tzinfo=UTC), False),  # 15h59 em Boa Vista
        (datetime(2026, 10, 25, 20, 0, tzinfo=UTC), True),  # 16h em Boa Vista = 17h Brasília
        (datetime(2026, 10, 26, 23, 0, tzinfo=UTC), True),  # ainda 26/10 em Boa Vista
        (datetime(2026, 10, 27, 4, 0, tzinfo=UTC), False),  # 27/10 0h em Boa Vista
    ],
)
def test_banner_window_in_boa_vista_time(
    api: TestClient, now: list[datetime], when: datetime, shown: bool
) -> None:
    now[0] = when
    body = api.get("/tse/status").json()
    assert body["banner"] is shown
    assert body["texto"] == "Acompanhe a apuração oficial do 2º turno — fonte: TSE"
    assert body["apuracao"] is True


def test_banner_window_comes_from_the_environment(
    api: TestClient, now: list[datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSE_BANNER_INICIO", "2026-10-20T08:00:00-04:00")
    monkeypatch.setenv("TSE_BANNER_FIM", "2026-10-20T09:00:00-04:00")
    get_settings.cache_clear()
    now[0] = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)  # 8h30 em Boa Vista

    assert api.get("/tse/status").json()["banner"] is True


def test_status_is_cacheable_for_a_minute(api: TestClient) -> None:
    assert api.get("/tse/status").headers["cache-control"] == "public, max-age=60"


@pytest.mark.parametrize(
    "header",
    [
        "W/{etag}",  # o proxy comprime e enfraquece o ETag
        '"outro", {etag}',
        "*",
    ],
)
def test_conditional_request_variants_get_304(api: TestClient, header: str) -> None:
    etag = api.get("/tse/resultado").headers["etag"]
    response = api.get("/tse/resultado", headers={"If-None-Match": header.format(etag=etag)})
    assert response.status_code == 304


def test_other_etag_gets_the_body(api: TestClient) -> None:
    response = api.get("/tse/resultado", headers={"If-None-Match": '"outro"'})
    assert response.status_code == 200 and response.json()["fonte"] == "TSE"
