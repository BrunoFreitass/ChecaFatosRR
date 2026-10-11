"""Apuração do TSE: cliente HTTP, configuração e single-flight (etapa 5).

Só mocks: o TSE é simulado (httpx.MockTransport e cliente falso); nada sai para a rede.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.repositories.tse import Snapshot
from app.services.tse.client import HttpTseClient, TseUnavailableError
from app.services.tse.parser import TseFormatError
from app.services.tse.service import TseCache, load_results

pytestmark = pytest.mark.anyio

FIXTURE = Path(__file__).parents[1] / "fixtures" / "tse" / "br-c0001-e006257-u.json"
TEMPLATE = (
    "https://resultados.tse.jus.br/oficial/ele2026/{eleicao}/dados/{escopo}/"
    "{escopo}-c{cargo:04d}-e{eleicao:06d}-u.json"
)


def tse_json(scope: str = "br", election: str = "6258") -> dict[str, Any]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["cdabr"], data["ele"] = scope, election
    return data


# ---------- configuração ----------


def test_default_settings_point_to_the_2026_second_round() -> None:
    settings = Settings()
    assert settings.tse_enabled is True
    assert settings.tse_election_code == 6258
    assert settings.tse_scope_list == ("br", "rr")
    assert settings.tse_refresh_seconds == 30


@pytest.mark.parametrize(
    "template",
    [
        "http://resultados.tse.jus.br/oficial/{escopo}.json",  # sem TLS
        "https://evil.example.com/{escopo}.json",  # outro host (SSRF pela configuração)
        "https://resultados.tse.jus.br.evil.com/{escopo}.json",
        "https://resultados.tse.jus.br/oficial/sem-escopo.json",
        "https://resultados.tse.jus.br:8443/{escopo}.json",  # porta
        "https://x@resultados.tse.jus.br/{escopo}.json",  # credenciais
        "https://resultados.tse.jus.br/{escopo}/{outro}.json",  # campo desconhecido
        "https://resultados.tse.jus.br/{escopo.__class__}.json",  # atributo
        "https://resultados.tse.jus.br/{escopo}\\x.json",  # barra invertida
    ],
)
def test_url_template_must_be_the_tse_over_https(template: str) -> None:
    with pytest.raises(ValidationError):
        Settings(tse_url_template=template)


@pytest.mark.parametrize("scopes", ["br,xyz", "br;rr", "", "BR,../", "br,sp"])
def test_scopes_are_validated(scopes: str) -> None:
    with pytest.raises(ValidationError):
        Settings(tse_scopes=scopes)


# ---------- cliente HTTP ----------


def client(handler: Any, **kwargs: Any) -> HttpTseClient:
    return HttpTseClient(
        TEMPLATE, 6258, transport_factory=lambda: httpx.MockTransport(handler), **kwargs
    )


def test_url_of_each_scope() -> None:
    assert client(lambda r: None).url_for("rr") == (
        "https://resultados.tse.jus.br/oficial/ele2026/6258/dados/rr/rr-c0001-e006258-u.json"
    )


async def test_fetch_returns_the_json_and_identifies_itself() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=tse_json())

    data = await client(handler).fetch("br")

    assert data["cdabr"] == "br"
    assert "ChecaFatosRR" in seen[0].headers["user-agent"]


@pytest.mark.parametrize("status", [302, 403, 404, 500, 503])
async def test_non_200_is_unavailable(status: int) -> None:
    handler = lambda r: httpx.Response(status, headers={"location": "https://x.example"})  # noqa: E731
    with pytest.raises(TseUnavailableError):
        await client(handler).fetch("br")


async def test_timeout_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=request)

    with pytest.raises(TseUnavailableError):
        await client(handler).fetch("br")


async def test_body_too_large_is_unavailable() -> None:
    handler = lambda r: httpx.Response(200, content=b"{" + b" " * 2000 + b"}")  # noqa: E731
    with pytest.raises(TseUnavailableError):
        await client(handler, max_bytes=1000).fetch("br")


async def test_invalid_json_is_a_format_error() -> None:
    handler = lambda r: httpx.Response(200, content=b"<html>manutencao</html>")  # noqa: E731
    with pytest.raises(TseFormatError):
        await client(handler).fetch("br")


# ---------- single-flight ----------


class FakeRepo:
    """tse_snapshots em memória; claim igual ao SQL (só um por janela de refresh)."""

    def __init__(self) -> None:
        self.snapshots: dict[str, Snapshot] = {}
        self.claims_allowed = True
        self.claims: list[str] = []
        self.saves: list[str] = []

    async def claim(self, scope: str, stale_seconds: int) -> bool:
        self.claims.append(scope)
        return self.claims_allowed

    async def save(self, scope: str, payload: dict[str, Any], tse_updated_at: Any) -> None:
        self.saves.append(scope)
        self.snapshots[scope] = Snapshot(scope, payload, tse_updated_at, datetime.now(UTC))

    async def get_all(self) -> list[Snapshot]:
        return list(self.snapshots.values())


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.election = "6258"

    async def fetch(self, scope: str) -> Any:
        self.calls.append(scope)
        if self.error is not None:
            raise self.error
        return tse_json(scope, self.election)


SETTINGS = Settings(tse_election_code=6258, tse_scopes="br,rr")


async def test_load_fetches_saves_and_returns_each_scope() -> None:
    repo, tse = FakeRepo(), FakeClient()

    results = await load_results(repo, tse, SETTINGS)

    assert tse.calls == ["br", "rr"]
    assert [r.escopo for r in results] == ["br", "rr"]
    assert results[0].resultado is not None
    assert results[0].resultado["candidatos"][0]["nome"] == "FLAVIO BOLSONARO"


async def test_load_without_the_claim_does_not_call_the_tse() -> None:
    repo, tse = FakeRepo(), FakeClient()
    await load_results(repo, tse, SETTINGS)
    repo.claims_allowed = False  # outra requisição (ou instância) já está buscando

    results = await load_results(repo, tse, SETTINGS)

    assert tse.calls == ["br", "rr"]  # nenhuma busca nova
    assert all(r.resultado is not None for r in results)  # serve o último snapshot


async def test_tse_down_keeps_the_last_snapshot_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    repo, tse = FakeRepo(), FakeClient()
    await load_results(repo, tse, SETTINGS)
    tse.error = TseUnavailableError("503")

    with caplog.at_level(logging.WARNING, logger="app.services.tse.service"):
        results = await load_results(repo, tse, SETTINGS)

    assert all(r.resultado is not None for r in results)
    assert [r.getMessage() for r in caplog.records].count("tse_fetch_failed") == 2


async def test_new_format_keeps_the_last_snapshot(caplog: pytest.LogCaptureFixture) -> None:
    repo, tse = FakeRepo(), FakeClient()
    await load_results(repo, tse, SETTINGS)
    tse.error = TseFormatError("sem candidatos")

    with caplog.at_level(logging.ERROR, logger="app.services.tse.service"):
        results = await load_results(repo, tse, SETTINGS)

    assert all(r.resultado is not None for r in results)
    assert "tse_format_error" in [r.getMessage() for r in caplog.records]


async def test_nothing_saved_yet_is_unavailable_not_an_error() -> None:
    repo, tse = FakeRepo(), FakeClient()
    tse.error = TseUnavailableError("fora")

    results = await load_results(repo, tse, SETTINGS)

    assert [r.resultado for r in results] == [None, None]


async def test_snapshot_of_another_election_is_ignored() -> None:
    # Ensaio com o 1º turno (6257) e depois a troca para o 2º (6258): nada do ensaio aparece.
    repo, tse = FakeRepo(), FakeClient()
    tse.election = "6257"
    await load_results(repo, tse, Settings(tse_election_code=6257, tse_scopes="br,rr"))
    repo.claims_allowed = False

    results = await load_results(repo, tse, SETTINGS)

    assert [r.resultado for r in results] == [None, None]


async def test_cache_serves_concurrent_requests_with_a_single_load() -> None:
    loads = 0

    async def load() -> str:
        nonlocal loads
        loads += 1
        await asyncio.sleep(0.02)
        return f"v{loads}"

    cache: TseCache[str] = TseCache(ttl=5.0)
    results = await asyncio.gather(*(cache.get(load) for _ in range(20)))

    assert loads == 1
    assert set(results) == {"v1"}


async def test_cache_expires_and_never_makes_others_wait_for_the_tse() -> None:
    now = [0.0]
    gate = asyncio.Event()

    async def slow() -> str:
        await gate.wait()
        return "novo"

    cache: TseCache[str] = TseCache(ttl=5.0, clock=lambda: now[0])
    assert await cache.get(lambda: _value("velho")) == "velho"
    now[0] = 6.0  # expirou

    refreshing = asyncio.create_task(cache.get(slow))
    await asyncio.sleep(0)
    # Enquanto uma requisição busca, as outras recebem o valor anterior na hora.
    assert await asyncio.wait_for(cache.get(slow), timeout=0.1) == "velho"
    gate.set()
    assert await refreshing == "novo"


async def test_cache_keeps_the_old_value_when_the_load_fails() -> None:
    now = [0.0]
    cache: TseCache[str] = TseCache(ttl=5.0, clock=lambda: now[0])
    await cache.get(lambda: _value("bom"))
    now[0] = 6.0

    async def broken() -> str:
        raise RuntimeError("banco fora")

    assert await cache.get(broken) == "bom"


async def test_cache_without_value_propagates_the_failure() -> None:
    async def broken() -> str:
        raise RuntimeError("banco fora")

    with pytest.raises(RuntimeError):
        await TseCache[str](ttl=5.0).get(broken)


async def _value(value: str) -> str:
    return value


def test_banner_must_end_after_it_starts() -> None:
    with pytest.raises(ValidationError):
        Settings(
            tse_banner_inicio="2026-10-26T00:00:00-04:00",
            tse_banner_fim="2026-10-25T00:00:00-04:00",
        )


async def test_deeply_nested_json_is_a_format_error() -> None:
    nested = b"[" * 100_000 + b"]" * 100_000
    handler = lambda r: httpx.Response(200, content=nested)  # noqa: E731
    with pytest.raises(TseFormatError):
        await client(handler).fetch("br")


async def test_unexpected_parser_bug_keeps_the_last_snapshot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.services.tse import service

    repo, tse = FakeRepo(), FakeClient()
    await load_results(repo, tse, SETTINGS)

    def broken(raw: Any) -> Any:
        raise ValueError("bug")

    monkeypatch.setattr(service, "parse_result", broken)
    with caplog.at_level(logging.ERROR, logger="app.services.tse.service"):
        results = await load_results(repo, tse, SETTINGS)

    assert all(r.resultado is not None for r in results)
    assert "tse_parse_failed" in [r.getMessage() for r in caplog.records]
