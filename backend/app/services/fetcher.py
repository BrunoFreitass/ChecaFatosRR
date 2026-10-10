"""Download da página de um link enviado pelo usuário, com as travas anti-SSRF.

O IP é validado no momento de abrir o socket (`GuardedNetworkBackend`): o DNS é resolvido
aqui, TODOS os endereços precisam ser públicos e a conexão vai para o IP já validado. O TLS
continua usando o nome original (SNI e certificado), então HTTPS segue verificado.
"""

import asyncio
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol
from urllib.parse import urljoin

import anyio
import certifi
import httpcore
import httpx

from app.services.url_guard import (
    BlockedAddressError,
    NormalizedUrl,
    normalize_url,
    pick_allowed_address,
)

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 3
USER_AGENT = "ChecaFatosRR/1.0 (+https://checafatosrr.pages.dev/sobre)"
_HTML_TYPES = {"text/html", "application/xhtml+xml", ""}

UnreadableReason = Literal["timeout", "status", "tipo", "redirects", "rede", "vazio", "rede_social"]
Resolver = Callable[[str, int], Awaitable[list[str]]]


class PageUnreadableError(Exception):
    def __init__(self, reason: UnreadableReason) -> None:
        super().__init__(reason)
        self.reason: UnreadableReason = reason


@dataclass(frozen=True)
class FetchedPage:
    url: str
    content: bytes
    truncated: bool


class PageFetcher(Protocol):
    async def fetch(self, url: NormalizedUrl) -> FetchedPage: ...


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        resolver: Resolver = system_resolver,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 — assinatura exigida pelo httpcore
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            resolved = await self._resolver(host.strip("[]"), port)
        except OSError as exc:  # domínio inexistente / DNS fora: "não deu para ler", não 500
            raise httpcore.ConnectError(f"dns: {type(exc).__name__}") from exc
        addresses = pick_allowed_address(host, resolved)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None  # noqa: S101 — addresses nunca é vazio aqui
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 — assinatura exigida pelo httpcore
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise BlockedAddressError("socket unix")

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    # Ler e interpretar o bundle de CAs custa milissegundos de CPU: uma vez por processo.
    return ssl.create_default_context(cafile=certifi.where())


class GuardedTransport(httpx.AsyncHTTPTransport):
    """Transporte do httpx com o `GuardedNetworkBackend`.

    O httpx não expõe `network_backend`; trocamos o pool interno (`_pool`). As versões de
    httpx/httpcore estão fixadas e `tests/unit/test_fetcher.py` exercita este caminho.
    """

    def __init__(self, backend: httpcore.AsyncNetworkBackend) -> None:
        super().__init__(trust_env=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=_ssl_context(),
            max_connections=4,
            max_keepalive_connections=0,
            network_backend=backend,
            retries=0,
        )


class HttpPageFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10,
        max_bytes: int = 2_000_000,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._transport_factory = transport_factory or (
            lambda: GuardedTransport(GuardedNetworkBackend())
        )

    async def fetch(self, url: NormalizedUrl) -> FetchedPage:
        try:
            async with asyncio.timeout(self._timeout):
                return await self._fetch(url)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise PageUnreadableError("timeout") from exc
        except httpx.HTTPError as exc:
            logger.info("fetch_failed", extra={"error": type(exc).__name__})
            raise PageUnreadableError("rede") from exc

    async def _fetch(self, url: NormalizedUrl) -> FetchedPage:
        async with httpx.AsyncClient(
            transport=self._transport_factory(),
            timeout=httpx.Timeout(self._timeout, connect=5),
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                "Accept-Language": "pt-BR,pt;q=0.9",
                # Sem compressão: o limite de 2 MB vale para o que chega pela rede, sem
                # descompressão de pedaços enormes no meio do caminho.
                "Accept-Encoding": "identity",
            },
        ) as client:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                async with client.stream("GET", current.url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise PageUnreadableError("status")
                        # Cada salto passa de novo pela normalização (esquema, porta, host);
                        # o IP é validado de novo na conexão.
                        current = normalize_url(urljoin(current.url, location))
                        continue
                    if not response.is_success:
                        raise PageUnreadableError("status")
                    content_type = response.headers.get("content-type", "")
                    if content_type.split(";")[0].strip().lower() not in _HTML_TYPES:
                        raise PageUnreadableError("tipo")
                    content, truncated = await self._read_limited(response)
                    return FetchedPage(url=current.url, content=content, truncated=truncated)
        raise PageUnreadableError("redirects")

    async def _read_limited(self, response: httpx.Response) -> tuple[bytes, bool]:
        # aiter_bytes entrega o conteúdo JÁ descomprimido: o limite vale para o tamanho real
        # (protege contra "bomba de gzip").
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._max_bytes:
                chunks.append(chunk[: len(chunk) - (size - self._max_bytes)])
                return b"".join(chunks), True
            chunks.append(chunk)
        return b"".join(chunks), False
