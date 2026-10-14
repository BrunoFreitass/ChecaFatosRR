"""Mapa do Brasil na Apuração: resumo mais recente do TSE por UF (plano mapa-e-painel-admin).

Mesmo desenho da apuração BR/RR (service.py): busca preguiçosa (só quando alguém pede o mapa),
sempre em segundo plano (TseCache: o usuário recebe o que está em memória) e claim no banco.
Aqui o claim é em lote: até TSE_MAPA_LOTE UFs vencidas por recarga, cada UF no máximo 1
busca a cada TSE_MAPA_REFRESH_SECONDS (com 1 visitante e polling de 60 s, as 27 buscadas em
~3 min; como a busca roda depois da resposta, o visitante vê cada lote no polling seguinte).
Uma UF que falha no TSE fica com o último resumo bom (ou sem dado): nunca derruba as outras.
O mapa nunca diz "Eleito": não existe eleito por UF na eleição presidencial.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.config import Settings
from app.services.tse.client import TseClient, TseUnavailableError
from app.services.tse.parser import TseFormatError, TseResult, parse_result

logger = logging.getLogger(__name__)

# Ordem fixa (a mesma do seed da migration e da resposta).
UFS: tuple[str, ...] = (
    "ac",
    "al",
    "am",
    "ap",
    "ba",
    "ce",
    "df",
    "es",
    "go",
    "ma",
    "mg",
    "ms",
    "mt",
    "pa",
    "pb",
    "pe",
    "pi",
    "pr",
    "rj",
    "rn",
    "ro",
    "rr",
    "rs",
    "sc",
    "se",
    "sp",
    "to",
)


@dataclass(frozen=True)
class UfSnapshot:
    uf: str
    eleicao: str | None
    resumo: dict[str, Any]
    tse_updated_at: datetime | None


class TseMapRepository(Protocol):
    async def claim_ufs(self, stale_seconds: int, limit: int) -> list[str]: ...
    async def save_ufs(self, items: list[UfSnapshot]) -> None: ...
    async def get_ufs(self) -> list[UfSnapshot]: ...


def summarize(result: TseResult) -> dict[str, Any]:
    """Só o que o mapa usa: número, nome e % de cada candidato, seções apuradas e se acabou."""
    return {
        "candidatos": [
            {"numero": c.numero, "nome": c.nome, "pct": c.pct} for c in result.candidatos
        ],
        "secoes_pct": result.secoes_pct,
        "final": result.final,
        "totalizado_em": result.totalizado_em.isoformat() if result.totalizado_em else None,
    }


async def load_map(
    repo: TseMapRepository, client: TseClient, settings: Settings
) -> list[tuple[str, dict[str, Any] | None]]:
    """Busca e depois lê (o que a recarga em segundo plano faz)."""
    await refresh_map(repo, client, settings)
    return await read_map(repo, settings)


async def refresh_map(repo: TseMapRepository, client: TseClient, settings: Settings) -> None:
    """Busca no TSE as UFs vencidas (em lote) e grava; só em segundo plano."""
    election = str(settings.tse_election_code)
    claimed = await repo.claim_ufs(settings.tse_mapa_refresh_seconds, settings.tse_mapa_lote)
    fetched = await asyncio.gather(*(_fetch(client, uf) for uf in claimed))
    fresh: list[UfSnapshot] = []
    for uf, result in zip(claimed, fetched, strict=True):
        if result is None:
            continue
        # Arquivo de outra eleição (ou sem "ele") não entra: além de não aparecer, quebraria o
        # CHECK de eleicao e derrubaria a gravação das outras UFs do lote.
        if result.eleicao != election:
            logger.error("tse_map_wrong_election", extra={"scope": uf, "eleicao": result.eleicao})
            continue
        fresh.append(UfSnapshot(uf, result.eleicao, summarize(result), result.totalizado_em))
    if fresh:
        try:
            await repo.save_ufs(fresh)
        except Exception:  # falha ao gravar: serve o que já estava salvo
            logger.exception("tse_map_save_failed", extra={"ufs": [s.uf for s in fresh]})


async def read_map(
    repo: TseMapRepository, settings: Settings
) -> list[tuple[str, dict[str, Any] | None]]:
    """Último resumo gravado de cada UF (só banco), na ordem de UFS."""
    election = str(settings.tse_election_code)
    saved = {s.uf: s for s in await repo.get_ufs()}
    out: list[tuple[str, dict[str, Any] | None]] = []
    for uf in UFS:
        snapshot = saved.get(uf)
        # Resumo de outra eleição (ensaio com o 1º turno) não aparece: a UF fica sem dado.
        valid = snapshot is not None and snapshot.eleicao == election and bool(snapshot.resumo)
        out.append((uf, snapshot.resumo if valid and snapshot is not None else None))
    return out


async def _fetch(client: TseClient, uf: str) -> TseResult | None:
    started = time.monotonic()
    try:
        result = parse_result(await client.fetch(uf))
        # Uma linha por busca real no TSE (mesmo nome da apuração BR/RR: dá para somar tudo).
        logger.info(
            "tse_fetch_ok",
            extra={"scope": uf, "fetch_ms": int((time.monotonic() - started) * 1000)},
        )
        return result
    except TseUnavailableError as exc:
        logger.warning("tse_map_fetch_failed", extra={"scope": uf, "error": str(exc)})
    except TseFormatError as exc:
        logger.error("tse_map_format_error", extra={"scope": uf, "error": str(exc)})
    except Exception:  # bug no parser numa UF não derruba o mapa
        logger.exception("tse_map_parse_failed", extra={"scope": uf})
    return None
