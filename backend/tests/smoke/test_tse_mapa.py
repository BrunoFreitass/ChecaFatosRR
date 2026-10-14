"""GET /tse/mapa (mapa do Brasil na Apuração, plano mapa-e-painel-admin). TSE e banco simulados."""

import json
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.routers.tse import get_tse_client, get_tse_map_repos
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
    def __init__(
        self, failing: frozenset[str] = frozenset(), election: dict[str, str] | None = None
    ) -> None:
        self.calls: list[str] = []
        self.failing = failing
        self.election = election or {}

    async def fetch(self, scope: str) -> Any:
        self.calls.append(scope)
        if scope in self.failing:
            raise TseUnavailableError("TSE fora")
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        data["cdabr"] = scope
        if scope in self.election:
            data["ele"] = self.election[scope]
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
    app.dependency_overrides[get_tse_map_repos] = lambda: lambda: nullcontext(repo)
    app.dependency_overrides[get_tse_client] = lambda: tse
    with TestClient(app) as client:
        yield client


def poll(api: TestClient, **kwargs: Any) -> Any:
    """Um polling do visitante: a resposta sai da memória e a busca no TSE corre em segundo
    plano; espera a busca acabar e devolve o que o próximo pedido já recebe."""
    api.get("/tse/mapa", **kwargs)
    api.portal.call(api.app.state.tse_map_cache.idle)  # type: ignore[union-attr,attr-defined]
    return api.get("/tse/mapa", **kwargs)


def _next_poll(api: TestClient, repo: MapRepo, seconds: int = 61) -> None:
    """Próximo polling do visitante: o cache do processo venceu e o relógio andou."""
    api.app.state.tse_map_cache._expires_at = float("-inf")  # type: ignore[attr-defined]
    repo.now += timedelta(seconds=seconds)


def test_first_request_fetches_one_batch_of_nine_states(api: TestClient, tse: Tse) -> None:
    body = poll(api).json()

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
    poll(api)
    _next_poll(api, repo)
    poll(api)
    _next_poll(api, repo)
    body = poll(api).json()

    assert sorted(tse.calls) == sorted(UFS)  # cada UF buscada uma vez
    assert all(e["resultado"] is not None for e in body["estados"])


def test_no_state_is_fetched_twice_within_two_minutes(
    api: TestClient, repo: MapRepo, tse: Tse
) -> None:
    for _ in range(4):  # polling a cada 30 s: 90 s ao todo
        poll(api)
        _next_poll(api, repo, seconds=30)

    assert len(tse.calls) == len(set(tse.calls))


def test_tse_failure_in_one_state_does_not_break_the_others(app: FastAPI, repo: MapRepo) -> None:
    failing = Tse(failing=frozenset({"ac"}))
    app.dependency_overrides[get_tse_map_repos] = lambda: lambda: nullcontext(repo)
    app.dependency_overrides[get_tse_client] = lambda: failing
    with TestClient(app) as client:
        response = poll(client)

    assert response.status_code == 200
    states = {e["uf"]: e["resultado"] for e in response.json()["estados"]}
    assert states["ac"] is None  # sem dado: cinza
    assert sum(r is not None for r in states.values()) == 8


def test_disabled_map_is_404_and_never_calls_the_tse(
    api: TestClient, tse: Tse, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSE_MAPA_ENABLED", "false")
    get_settings.cache_clear()

    response = poll(api)

    assert response.status_code == 404
    assert tse.calls == []
    assert api.get("/tse/status").json()["mapa"] is False


def test_status_announces_the_map_when_enabled(api: TestClient) -> None:
    assert api.get("/tse/status").json()["mapa"] is True


def test_map_never_says_elected_and_sends_the_configured_colors(api: TestClient) -> None:
    response = poll(api)

    assert "eleit" not in response.text.lower()  # "Eleito" só no BR, com o texto do TSE
    assert response.json()["cores"] == {"13": "vermelho", "22": "azul"}


def test_snapshot_of_another_election_is_not_shown(api: TestClient, repo: MapRepo) -> None:
    poll(api)
    repo.rows["ac"] = UfSnapshot("ac", "6258", {"candidatos": []}, None)
    _next_poll(api, repo, seconds=1)

    states = {e["uf"]: e["resultado"] for e in poll(api).json()["estados"]}
    assert states["ac"] is None


def test_etag_answers_304(api: TestClient) -> None:
    first = poll(api)
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


def test_file_of_another_election_is_skipped_and_the_batch_is_saved(
    app: FastAPI, repo: MapRepo
) -> None:
    """Revisão (14/10/2026): um arquivo com "ele" vazio quebraria o CHECK e o lote inteiro."""
    odd = Tse(election={"ac": ""})
    app.dependency_overrides[get_tse_map_repos] = lambda: lambda: nullcontext(repo)
    app.dependency_overrides[get_tse_client] = lambda: odd
    with TestClient(app) as client:
        response = poll(client)

    assert response.status_code == 200
    assert repo.rows["ac"].eleicao is None  # não gravado
    assert sum(r.eleicao == "6257" for r in repo.rows.values()) == 8


def test_save_failure_serves_what_was_already_saved(app: FastAPI, repo: MapRepo, tse: Tse) -> None:
    async def broken_save(items: list[UfSnapshot]) -> None:
        raise RuntimeError("banco fora")

    repo.rows["rr"] = UfSnapshot("rr", "6257", {"candidatos": [], "secoes_pct": 1.0}, None)
    repo.save_ufs = broken_save  # type: ignore[method-assign]
    app.dependency_overrides[get_tse_map_repos] = lambda: lambda: nullcontext(repo)
    app.dependency_overrides[get_tse_client] = lambda: tse
    with TestClient(app) as client:
        response = poll(client)

    assert response.status_code == 200
    states = {e["uf"]: e["resultado"] for e in response.json()["estados"]}
    assert states["rr"] == {"candidatos": [], "secoes_pct": 1.0}


def test_non_ascii_digits_are_rejected_in_colors() -> None:
    with pytest.raises(ValidationError):
        Settings(tse_mapa_cores="¹³=vermelho")


def test_each_state_fetch_is_logged_like_the_count(
    api: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="app.services.tse.mapa"):
        poll(api)

    fetched = [r for r in caplog.records if r.getMessage() == "tse_fetch_ok"]
    assert len(fetched) == 9  # um lote
    assert all(isinstance(r.fetch_ms, int) for r in fetched)  # type: ignore[attr-defined]
