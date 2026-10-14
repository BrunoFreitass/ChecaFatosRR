"""ReadCache (PR cache-leitura): leituras públicas por alguns segundos, uma carga por chave e
invalidação imediata no processo (o autor vê o próprio comentário na hora)."""

import asyncio

import pytest

from app.services.read_cache import ReadCache

pytestmark = pytest.mark.anyio


class Loader:
    def __init__(self) -> None:
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.error: Exception | None = None
        self.value = "v"

    async def __call__(self) -> str:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return f"{self.value}{self.calls}"


async def test_hit_within_ttl_and_reload_after() -> None:
    now = [0.0]
    cache: ReadCache[str] = ReadCache(ttl=10.0, clock=lambda: now[0])
    load = Loader()

    assert await cache.get("k", load) == "v1"
    now[0] = 9.0
    assert await cache.get("k", load) == "v1"
    now[0] = 11.0
    assert await cache.get("k", load) == "v2"
    assert load.calls == 2


async def test_concurrent_misses_share_one_load() -> None:
    cache: ReadCache[str] = ReadCache(ttl=10.0)
    load = Loader()
    load.gate = asyncio.Event()

    pending = [asyncio.create_task(cache.get("k", load)) for _ in range(20)]
    await asyncio.sleep(0)
    load.gate.set()

    assert set(await asyncio.gather(*pending)) == {"v1"}
    assert load.calls == 1


async def test_keys_are_separate() -> None:
    cache: ReadCache[str] = ReadCache(ttl=10.0)
    a, b = Loader(), Loader()
    a.value, b.value = "a", "b"

    assert await cache.get(("feed", None), a) == "a1"
    assert await cache.get(("check", "x"), b) == "b1"


async def test_invalidate_drops_everything_at_once() -> None:
    cache: ReadCache[str] = ReadCache(ttl=10.0)
    load = Loader()
    await cache.get("k", load)

    cache.invalidate()

    assert await cache.get("k", load) == "v2"


async def test_a_load_started_before_invalidation_is_never_served_after_it() -> None:
    """O autor posta durante uma leitura em andamento: a leitura velha não pode voltar."""
    cache: ReadCache[str] = ReadCache(ttl=10.0)
    old = Loader()
    old.value = "antes"
    old.gate = asyncio.Event()
    started = asyncio.create_task(cache.get("k", old))
    await asyncio.sleep(0)

    cache.invalidate()  # comentário criado
    new = Loader()
    new.value = "depois"
    after = await cache.get("k", new)  # não pega carona na leitura velha

    old.gate.set()
    assert await started == "antes1"  # quem pediu antes recebe o que pediu
    assert after == "depois1"
    assert await cache.get("k", Loader()) == "depois1"  # e a velha não foi guardada


async def test_errors_are_not_cached_and_reach_everyone_waiting() -> None:
    cache: ReadCache[str] = ReadCache(ttl=10.0)
    load = Loader()
    load.gate = asyncio.Event()
    load.error = RuntimeError("banco fora")
    pending = [asyncio.create_task(cache.get("k", load)) for _ in range(3)]
    await asyncio.sleep(0)
    load.gate.set()

    results = await asyncio.gather(*pending, return_exceptions=True)

    assert all(isinstance(r, RuntimeError) for r in results)
    load.error, load.gate = None, None
    assert await cache.get("k", load) == "v2"  # a próxima tenta de novo


async def test_a_cancelled_request_does_not_break_the_others() -> None:
    cache: ReadCache[str] = ReadCache(ttl=10.0)
    load = Loader()
    load.gate = asyncio.Event()
    first = asyncio.create_task(cache.get("k", load))
    await asyncio.sleep(0)
    second = asyncio.create_task(cache.get("k", load))
    await asyncio.sleep(0)

    first.cancel()  # o cliente fechou a conexão
    load.gate.set()

    assert await second == "v1"
    assert load.calls == 1


async def test_size_is_bounded() -> None:
    cache: ReadCache[str] = ReadCache(ttl=10.0, max_entries=3)
    load = Loader()
    for key in range(5):
        await cache.get(key, load)

    assert len(cache) == 3  # ids aleatórios em ?checagem= não crescem a memória sem fim
