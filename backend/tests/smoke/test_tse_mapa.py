"""GET /tse/mapa (mapa do Brasil na Apuração, plano mapa-e-painel-admin). TSE e banco simulados."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.routers.tse import get_tse_client, get_tse_map_repository
from app.services.tse.client import TseUnavailableError
from app.services.tse.mapa import UFS, UfSnapshot

FIXTURE = Path(__file__).parents[1] / "fixtures" / "tse" / "br-c0001-e006257-u.json"
NEVER = datetime(1970, 1, 1, tzinfo=UTC)


class MapRepo:
    """Banco falso: uma linha por UF, vencida até ser buscada (como o seed da migration)."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
        self.rows = {uf: UfSnapshot(uf, None, {}, None) for uf in UFS}
        self.fetched_at = dict.fromkeys(UFS, NEVER)

    async def claim_ufs(self, stale_seconds: int, limit: int) -> list[str]:
        limit_at = self.now - timedelta(seconds=stale_seconds)
        stale = sorted((at, uf) for uf, at in self.fetched_at.items() if at < limit_at)
        chosen = [uf for _, uf in stale[:limit]]
        for uf in chosen:
            self.fetched_at[uf] = self.now
        return chosen

    async def save_ufs(self, items: list[UfSnapshot]) -> None:
        for item in items:
            self.rows[item.uf] = item

    async def get_ufs(self) -> list[UfSnapshot]:
        return list(self.rows.values())


class Tse:
    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self.failing = failing

    async def fetch(self, scope: str) -> Any:
        self.calls.append(scope)
        if scope in self.failing:
            raise TseUnavailableError("TSE fora")
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        data["cdabr"] = scope
        return data


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TSE_ELECTION_CODE", "6257")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def repo() -> MapRepo:
    return MapRepo()


@pytest.fixture
def tse() -> Tse:
    return Tse()


@pytest.fixture
def api(app: FastAPI, repo: MapRepo, tse: Tse) -> Iterator[TestClient]:
    app.dependency_overrides[get_tse_map_repository] = lambda: repo
    app.dependency_overrides[get_tse_client] = lambda: tse
    with TestClient(app) as client:
        yield client


def _next_poll(api: TestClient, repo: MapRepo, seconds: int = 61) -> None:
    """Próximo polling do visitante: o cache do processo venceu e o relógio andou."""
    api.app.state.tse_map_cache._expires_at = float("-inf")  # type: ignore[attr-defined]
    repo.now += timedelta(seconds=seconds)


def test_first_request_fetches_one_batch_of_nine_states(api: TestClient, tse: Tse) -> None:
    body = api.get("/tse/mapa").json()

    assert len(tse.calls) == 9
    assert [e["uf"] for e in body["estados"]] == list(UFS)
    filled = [e for e in body["estados"] if e["resultado"] is not None]
    assert len(filled) == 9
    first = filled[0]["resultado"]
    assert {c["numero"] for c in first["candidatos"]} >= {"13", "22"}
    assert first["secoes_pct"] == 100.0


def test_one_visitor_updates_all_27_states_in_three_polls(
    api: TestClient, repo: MapRepo, tse: Tse
) -> None:
    """Plano aprovado: com 1 visitante (polling de 60 s), os 27 estados em até ~3 min."""
    api.get("/tse/mapa")
    _next_poll(api, repo)
    api.get("/tse/mapa")
    _next_poll(api, repo)
    body = api.get("/tse/mapa").json()

    assert sorted(tse.calls) == sorted(UFS)  # cada UF buscada uma vez
    assert all(e["resultado"] is not None for e in body["estados"])


def test_no_state_is_fetched_twice_within_two_minutes(
    api: TestClient, repo: MapRepo, tse: Tse
) -> None:
    for _ in range(4):  # polling a cada 30 s: 90 s ao todo
        api.get("/tse/mapa")
        _next_poll(api, repo, seconds=30)

    assert len(tse.calls) == len(set(tse.calls))


def test_tse_failure_in_one_state_does_not_break_the_others(app: FastAPI, repo: MapRepo) -> None:
    failing = Tse(failing=frozenset({"ac"}))
    app.dependency_overrides[get_tse_map_repository] = lambda: repo
    app.dependency_overrides[get_tse_client] = lambda: failing
    with TestClient(app) as client:
        response = client.get("/tse/mapa")

    assert response.status_code == 200
    states = {e["uf"]: e["resultado"] for e in response.json()["estados"]}
    assert states["ac"] is None  # sem dado: cinza
    assert sum(r is not None for r in states.values()) == 8


def test_disabled_map_is_404_and_never_calls_the_tse(
    api: TestClient, tse: Tse, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSE_MAPA_ENABLED", "false")
    get_settings.cache_clear()

    response = api.get("/tse/mapa")

    assert response.status_code == 404
    assert tse.calls == []
    assert api.get("/tse/status").json()["mapa"] is False


def test_status_announces_the_map_when_enabled(api: TestClient) -> None:
    assert api.get("/tse/status").json()["mapa"] is True


def test_map_never_says_elected_and_sends_the_configured_colors(api: TestClient) -> None:
    response = api.get("/tse/mapa")

    assert "eleit" not in response.text.lower()  # "Eleito" só no BR, com o texto do TSE
    assert response.json()["cores"] == {"13": "vermelho", "22": "azul"}


def test_snapshot_of_another_election_is_not_shown(api: TestClient, repo: MapRepo) -> None:
    api.get("/tse/mapa")
    repo.rows["ac"] = UfSnapshot("ac", "6258", {"candidatos": []}, None)
    _next_poll(api, repo, seconds=1)

    states = {e["uf"]: e["resultado"] for e in api.get("/tse/mapa").json()["estados"]}
    assert states["ac"] is None


def test_etag_answers_304(api: TestClient) -> None:
    first = api.get("/tse/mapa")
    again = api.get("/tse/mapa", headers={"If-None-Match": first.headers["etag"]})

    assert again.status_code == 304
    assert "public" in first.headers["cache-control"]


@pytest.mark.parametrize("raw", ["13=roxo", "13", "abc=azul", "13=azul,13=vermelho"])
def test_invalid_map_colors_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        Settings(tse_mapa_cores=raw)


def test_default_colors_are_the_ones_read_from_the_official_file() -> None:
    """Lidos do arquivo oficial 6257 e confirmados pelo usuário em 14/10/2026."""
    assert Settings().tse_mapa_color_map == {"13": "vermelho", "22": "azul"}
