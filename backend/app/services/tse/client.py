"""Busca o resultado de um escopo (br, rr) no site de resultados do TSE.

Só o host oficial (validado na configuração), sem seguir redirects, com timeout e teto de
bytes. Falha de rede, status diferente de 200 ou corpo grande demais: TseUnavailableError
(o serviço mantém o último resultado). JSON inválido: TseFormatError.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from app.services.tse.parser import TseFormatError

USER_AGENT = "ChecaFatosRR/1.0 (+https://checafatosrr.pages.dev; apuracao)"
PRESIDENTE = 1


class TseUnavailableError(Exception):
    """O TSE não respondeu com o arquivo (rede, status, tamanho)."""


class TseClient(Protocol):
    async def fetch(self, scope: str) -> Any: ...


class HttpTseClient:
    def __init__(
        self,
        url_template: str,
        election: int,
        *,
        cargo: int = PRESIDENTE,
        timeout: float = 5.0,
        max_bytes: int = 512_000,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._template = url_template
        self._election = election
        self._cargo = cargo
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._transport_factory = transport_factory

    def url_for(self, scope: str) -> str:
        return self._template.format(escopo=scope, eleicao=self._election, cargo=self._cargo)

    async def fetch(self, scope: str) -> Any:
        try:
            # Teto do pedido inteiro (o do httpx vale por fase: conexão, leitura...).
            async with asyncio.timeout(self._timeout + 1):
                body = await self._download(self.url_for(scope))
        except (TimeoutError, httpx.HTTPError) as exc:
            raise TseUnavailableError(type(exc).__name__) from exc
        try:
            return json.loads(body)
        except (ValueError, RecursionError) as exc:  # RecursionError: "[[[[..." aninhado
            raise TseFormatError("JSON inválido") from exc

    async def _download(self, url: str) -> bytes:
        transport = self._transport_factory() if self._transport_factory else None
        async with (
            httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise TseUnavailableError(f"HTTP {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > self._max_bytes:
                    raise TseUnavailableError("arquivo grande demais")
            return bytes(body)
