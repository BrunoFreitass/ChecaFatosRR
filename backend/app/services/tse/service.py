"""Apuração do TSE: o usuário nunca espera o TSE (stale-while-revalidate).

1. TseCache (no processo): responde sempre com o valor em memória. Vencido (TTL), dispara UMA
   recarga em segundo plano: busca no TSE (refresh) e relê o snapshot do banco (read).
   Servidor recém-acordado, sem valor: lê só o último snapshot do banco, nunca o TSE.
2. Claim no banco (tse_snapshots.fetched_at): no máximo uma busca no TSE por escopo a cada
   TSE_REFRESH_SECONDS, mesmo com várias instâncias.
TSE fora do ar ou com formato novo: fica o último resultado bom, com o horário de totalização
do próprio TSE ("Atualizado em…"), então o dado velho nunca parece novo (e o log diz por quê).
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


async def refresh_results(repo: TseRepository, client: TseClient, settings: Settings) -> None:
    """Busca no TSE os escopos vencidos e grava (só em segundo plano, nunca na requisição)."""
    # Claims em sequência (mesma sessão do banco); as buscas no TSE, em paralelo.
    scopes = settings.tse_scope_list
    claimed = [s for s in scopes if await repo.claim(s, settings.tse_refresh_seconds)]
    fetched = await asyncio.gather(*(_fetch(client, s) for s in claimed))
    for scope, result in zip(claimed, fetched, strict=True):
        if result is not None:
            await repo.save(scope, to_payload(result), result.totalizado_em)


async def load_results(
    repo: TseRepository, client: TseClient, settings: Settings
) -> list[ScopeResult]:
    """Busca e depois lê (o que a recarga em segundo plano faz)."""
    await refresh_results(repo, client, settings)
    return await read_results(repo, settings)


async def read_results(repo: TseRepository, settings: Settings) -> list[ScopeResult]:
    """Último snapshot gravado de cada escopo (só banco)."""
    election = str(settings.tse_election_code)
    scopes = settings.tse_scope_list
    saved = {s.scope: s.payload for s in await repo.get_all()}
    out = []
    for scope in scopes:
        payload = saved.get(scope)
        # Snapshot de outra eleição (ensaio com o 1º turno) não aparece.
        valid = payload is not None and payload.get("eleicao") == election
        out.append(ScopeResult(scope, payload if valid else None))
    return out


async def _fetch(client: TseClient, scope: str) -> TseResult | None:
    started = time.monotonic()
    try:
        result = parse_result(await client.fetch(scope))
        # Uma linha por busca real no TSE: conta quantas o single-flight deixou passar.
        logger.info("tse_fetch_ok", extra={"scope": scope, "fetch_ms": _ms(started)})
        return result
    except TseUnavailableError as exc:
        logger.warning("tse_fetch_failed", extra={"scope": scope, "error": str(exc)})
    except TseFormatError as exc:
        logger.error("tse_format_error", extra={"scope": scope, "error": str(exc)})
    except Exception:  # bug no parser não derruba a página: fica o último resultado bom
        logger.exception("tse_parse_failed", extra={"scope": scope})
    return None


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class TseCache[T]:
    """Valor em memória; vencido, recarrega em segundo plano (uma recarga por vez).

    get(read, refresh): read monta o valor a partir do banco (rápido); refresh busca no TSE e
    grava. A requisição nunca espera o refresh: na 1ª vez (sem valor) espera só o read.
    """

    def __init__(self, ttl: float = 5.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._value: T | None = None
        self._has_value = False
        self._expires_at = float("-inf")
        self._task: asyncio.Task[None] | None = None

    async def get(
        self, read: Callable[[], Awaitable[T]], refresh: Callable[[], Awaitable[None]]
    ) -> T:
        if not self._has_value:
            async with self._lock:  # servidor recém-acordado: um read só, para todos
                if not self._has_value:
                    self._value = await read()  # falha sem valor nenhum: propaga (500)
                    self._has_value = True
        if self._clock() >= self._expires_at and (self._task is None or self._task.done()):
            self._expires_at = self._clock() + self._ttl
            self._task = asyncio.create_task(self._revalidate(read, refresh))
        return self._value  # type: ignore[return-value]

    async def _revalidate(
        self, read: Callable[[], Awaitable[T]], refresh: Callable[[], Awaitable[None]]
    ) -> None:
        try:
            await refresh()
        except Exception:  # TSE ou banco fora: o último valor bom continua no ar
            logger.warning("tse_refresh_failed", exc_info=True)
        try:
            self._value = await read()
        except Exception:
            logger.warning("tse_load_failed", exc_info=True)

    async def idle(self) -> None:
        """Espera a recarga em andamento (testes e desligamento)."""
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
