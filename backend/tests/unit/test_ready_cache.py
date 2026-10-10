"""Cache do /health/ready: público e sem login, não pode esgotar o pool de 5 conexões.

Sucesso fica 60 s em cache, falha 10 s (volta logo quando o banco se recupera). Chamadas
simultâneas durante a verificação esperam a mesma consulta (uma ida ao banco só).
"""

import asyncio

import pytest

from app.routers.health import ReadyCache

pytestmark = pytest.mark.anyio


class Probe:
    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None
        self.delay = 0.0

    async def __call__(self) -> None:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error


async def test_success_is_cached_for_60_seconds() -> None:
    now = [0.0]
    probe = Probe()
    cache = ReadyCache(clock=lambda: now[0])

    assert await cache.check(probe) is None
    now[0] = 59.0
    assert await cache.check(probe) is None
    assert probe.calls == 1
    now[0] = 60.0
    assert await cache.check(probe) is None
    assert probe.calls == 2


async def test_failure_is_cached_for_10_seconds_only() -> None:
    now = [0.0]
    probe = Probe()
    probe.error = OSError("banco fora")
    cache = ReadyCache(clock=lambda: now[0])

    assert isinstance(await cache.check(probe), OSError)
    now[0] = 9.0
    assert isinstance(await cache.check(probe), OSError)
    assert probe.calls == 1
    probe.error = None
    now[0] = 10.0
    assert await cache.check(probe) is None  # o banco voltou: aparece em até 10 s
    assert probe.calls == 2


async def test_concurrent_calls_share_a_single_probe() -> None:
    probe = Probe()
    probe.delay = 0.05
    cache = ReadyCache()

    results = await asyncio.gather(*(cache.check(probe) for _ in range(20)))

    assert results == [None] * 20
    assert probe.calls == 1  # 20 chamadas, uma conexão


async def test_unexpected_error_is_returned_not_raised() -> None:
    probe = Probe()
    probe.error = RuntimeError("inesperado")
    assert isinstance(await ReadyCache().check(probe), RuntimeError)


async def test_failure_is_logged_once_per_probe(caplog: pytest.LogCaptureFixture) -> None:
    # Endpoint público: chamadas repetidas com o banco fora não podem inflar os logs.
    now = [0.0]
    probe = Probe()
    probe.error = OSError("banco fora")
    cache = ReadyCache(clock=lambda: now[0])

    with caplog.at_level("WARNING", logger="app.routers.health"):
        for _ in range(5):
            await cache.check(probe)
        assert [r.message for r in caplog.records] == ["db_unavailable"]
        now[0] = 10.0
        await cache.check(probe)
    assert len(caplog.records) == 2
