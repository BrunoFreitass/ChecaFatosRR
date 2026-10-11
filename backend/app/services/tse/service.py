"""Apuração do TSE: refresh preguiçoso com single-flight em duas camadas.

1. TseCache (no processo): o resultado montado vale TTL segundos; uma requisição recarrega
   e, enquanto isso, as outras recebem o valor anterior na hora (nunca esperam o TSE).
2. Claim no banco (tse_snapshots.fetched_at): no máximo uma busca no TSE por escopo a cada
   TSE_REFRESH_SECONDS, mesmo com várias instâncias.
TSE fora do ar ou com formato novo: fica o último resultado bom (e o log diz por quê).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from app.config import Settings
from app.repositories.tse import TseRepository
from app.services.tse.client import TseClient, TseUnavailableError
from app.services.tse.parser import TseFormatError, TseResult, parse_result

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScopeResult:
    escopo: str
    resultado: dict[str, Any] | None  # None = ainda sem resultado desta eleição


def to_payload(result: TseResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["totalizado_em"] = result.totalizado_em.isoformat() if result.totalizado_em else None
    return payload


async def load_results(
    repo: TseRepository, client: TseClient, settings: Settings
) -> list[ScopeResult]:
    election = str(settings.tse_election_code)
    scopes = settings.tse_scope_list
    # Claims em sequência (mesma sessão do banco); as buscas no TSE, em paralelo.
    claimed = [s for s in scopes if await repo.claim(s, settings.tse_refresh_seconds)]
    fetched = await asyncio.gather(*(_fetch(client, s) for s in claimed))
    for scope, result in zip(claimed, fetched, strict=True):
        if result is not None:
            await repo.save(scope, to_payload(result), result.totalizado_em)
    saved = {s.scope: s.payload for s in await repo.get_all()}
    out = []
    for scope in scopes:
        payload = saved.get(scope)
        # Snapshot de outra eleição (ensaio com o 1º turno) não aparece.
        valid = payload is not None and payload.get("eleicao") == election
        out.append(ScopeResult(scope, payload if valid else None))
    return out


async def _fetch(client: TseClient, scope: str) -> TseResult | None:
    try:
        return parse_result(await client.fetch(scope))
    except TseUnavailableError as exc:
        logger.warning("tse_fetch_failed", extra={"scope": scope, "error": str(exc)})
    except TseFormatError as exc:
        logger.error("tse_format_error", extra={"scope": scope, "error": str(exc)})
    except Exception:  # bug no parser não derruba a página: fica o último resultado bom
        logger.exception("tse_parse_failed", extra={"scope": scope})
    return None


class TseCache[T]:
    """Valor montado em memória por TTL segundos; uma recarga por vez (single-flight)."""

    def __init__(self, ttl: float = 5.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._value: T | None = None
        self._has_value = False
        self._expires_at = float("-inf")

    async def get(self, load: Callable[[], Awaitable[T]]) -> T:
        if self._fresh():
            return self._value  # type: ignore[return-value]
        if self._lock.locked() and self._has_value:
            return self._value  # type: ignore[return-value]  # outra requisição recarrega
        async with self._lock:
            if self._fresh():
                return self._value  # type: ignore[return-value]
            try:
                value = await load()
            except Exception:
                if not self._has_value:
                    raise
                logger.warning("tse_load_failed", exc_info=True)
            else:
                self._value, self._has_value = value, True
            self._expires_at = self._clock() + self._ttl
            return self._value  # type: ignore[return-value]

    def _fresh(self) -> bool:
        return self._has_value and self._clock() < self._expires_at
