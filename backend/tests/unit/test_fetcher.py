import asyncio
import gzip
import socket
from collections.abc import Callable

import httpcore
import httpx
import pytest

from app.services.fetcher import (
    GuardedNetworkBackend,
    GuardedTransport,
    HttpPageFetcher,
    PageUnreadableError,
)
from app.services.url_guard import BlockedAddressError, normalize_url

pytestmark = pytest.mark.anyio

HTML = {"content-type": "text/html; charset=utf-8"}


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Backend "de verdade" falso: registra para qual IP a conexão iria e falha."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def connect_tcp(self, host: str, port: int, **_: object) -> httpcore.AsyncNetworkStream:
        self.calls.append((host, port))
        raise httpcore.ConnectError("sem rede nos testes")


def resolver_for(*addresses: str) -> Callable[[str, int], object]:
    async def resolve(_host: str, _port: int) -> list[str]:
        return list(addresses)

    return resolve


# ---------- validação do IP no socket ----------


async def test_backend_blocks_dns_pointing_inside() -> None:
    inner = RecordingBackend()
    backend = GuardedNetworkBackend(resolver=resolver_for("10.0.0.7"), inner=inner)

    with pytest.raises(BlockedAddressError):
        await backend.connect_tcp("noticia.exemplo.com", 443)
    assert inner.calls == []


async def test_backend_blocks_rebinding_with_mixed_answer() -> None:
    inner = RecordingBackend()
    backend = GuardedNetworkBackend(
        resolver=resolver_for("93.184.216.34", "169.254.169.254"), inner=inner
    )

    with pytest.raises(BlockedAddressError):
        await backend.connect_tcp("noticia.exemplo.com", 80)
    assert inner.calls == []


async def test_backend_connects_to_the_validated_ip() -> None:
    inner = RecordingBackend()
    backend = GuardedNetworkBackend(resolver=resolver_for("93.184.216.34"), inner=inner)

    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("noticia.exemplo.com", 80)
    assert inner.calls == [("93.184.216.34", 80)]


async def test_full_transport_never_reaches_internal_ip() -> None:
    inner = RecordingBackend()
    fetcher = HttpPageFetcher(
        transport_factory=lambda: GuardedTransport(
            GuardedNetworkBackend(resolver=resolver_for("127.0.0.1"), inner=inner)
        )
    )

    with pytest.raises(BlockedAddressError):
        await fetcher.fetch(normalize_url("http://rebinding.exemplo.com/"))
    assert inner.calls == []


async def test_full_transport_uses_public_ip() -> None:
    inner = RecordingBackend()
    fetcher = HttpPageFetcher(
        transport_factory=lambda: GuardedTransport(
            GuardedNetworkBackend(resolver=resolver_for("93.184.216.34"), inner=inner)
        )
    )

    with pytest.raises(PageUnreadableError) as exc:
        await fetcher.fetch(normalize_url("http://noticia.exemplo.com/"))
    assert exc.value.reason == "rede"
    assert inner.calls == [("93.184.216.34", 80)]


# ---------- redirects, tipo, tamanho e tempo (transporte simulado) ----------


def fetcher_with(handler: Callable[[httpx.Request], object], **kwargs: object) -> HttpPageFetcher:
    return HttpPageFetcher(transport_factory=lambda: httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]


async def test_returns_html() -> None:
    page = await fetcher_with(
        lambda _: httpx.Response(200, headers=HTML, content=b"<p>oi</p>")
    ).fetch(normalize_url("https://site.com.br/a"))
    assert page.content == b"<p>oi</p>"
    assert not page.truncated


async def test_follows_relative_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/curto":
            return httpx.Response(301, headers={"location": "/noticia?utm_source=zap"})
        return httpx.Response(200, headers=HTML, content=b"ok")

    page = await fetcher_with(handler).fetch(normalize_url("https://site.com.br/curto"))
    assert page.url == "https://site.com.br/noticia"


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "http://2130706433/",
        "http://[::1]/",
    ],
)
async def test_redirect_to_internal_address_is_blocked(location: str) -> None:
    fetcher = fetcher_with(lambda _: httpx.Response(302, headers={"location": location}))
    with pytest.raises(BlockedAddressError):
        await fetcher.fetch(normalize_url("https://site.com.br/a"))


async def test_redirect_to_other_scheme_is_invalid() -> None:
    fetcher = fetcher_with(
        lambda _: httpx.Response(302, headers={"location": "file:///etc/passwd"})
    )
    with pytest.raises(ValueError):
        await fetcher.fetch(normalize_url("https://site.com.br/a"))


async def test_too_many_redirects() -> None:
    counter = iter(range(100))
    fetcher = fetcher_with(
        lambda _: httpx.Response(302, headers={"location": f"/r{next(counter)}"})
    )
    with pytest.raises(PageUnreadableError) as exc:
        await fetcher.fetch(normalize_url("https://site.com.br/a"))
    assert exc.value.reason == "redirects"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(404, headers=HTML), "status"),
        (httpx.Response(500, headers=HTML), "status"),
        (httpx.Response(200, headers={"content-type": "application/pdf"}), "tipo"),
        (httpx.Response(200, headers={"content-type": "image/jpeg"}), "tipo"),
    ],
)
async def test_unreadable(response: httpx.Response, reason: str) -> None:
    with pytest.raises(PageUnreadableError) as exc:
        await fetcher_with(lambda _: response).fetch(normalize_url("https://site.com.br/a"))
    assert exc.value.reason == reason


async def test_size_limit_truncates() -> None:
    body = b"a" * 3_000_000
    page = await fetcher_with(lambda _: httpx.Response(200, headers=HTML, content=body)).fetch(
        normalize_url("https://site.com.br/a")
    )
    assert page.truncated
    assert len(page.content) == 2_000_000


async def test_gzip_bomb_is_limited_by_decompressed_size() -> None:
    bomb = gzip.compress(b"\0" * 20_000_000)  # ~20 KB comprimido, 20 MB aberto
    headers = HTML | {"content-encoding": "gzip"}
    page = await fetcher_with(lambda _: httpx.Response(200, headers=headers, content=bomb)).fetch(
        normalize_url("https://site.com.br/a")
    )
    assert page.truncated
    assert len(page.content) == 2_000_000


async def test_timeout() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, headers=HTML)

    with pytest.raises(PageUnreadableError) as exc:
        await fetcher_with(slow, timeout_seconds=0.05).fetch(normalize_url("https://site.com.br/a"))
    assert exc.value.reason == "timeout"


async def test_unknown_domain_is_unreadable_not_500() -> None:
    async def failing_resolver(_host: str, _port: int) -> list[str]:
        raise socket.gaierror(11001, "getaddrinfo failed")

    inner = RecordingBackend()
    fetcher = HttpPageFetcher(
        transport_factory=lambda: GuardedTransport(
            GuardedNetworkBackend(resolver=failing_resolver, inner=inner)
        )
    )
    with pytest.raises(PageUnreadableError) as exc:
        await fetcher.fetch(normalize_url("https://dominio-que-nao-existe.com.br/x"))
    assert exc.value.reason == "rede"
    assert inner.calls == []
