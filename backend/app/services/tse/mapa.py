"""Mapa do Brasil na Apuração: resumo mais recente do TSE por UF (plano mapa-e-painel-admin).

Mesmo desenho da apuração BR/RR (service.py): busca preguiçosa (só quando alguém pede o mapa),
single-flight no processo (TseCache) e claim no banco. Aqui o claim é em lote: até
TSE_MAPA_LOTE UFs vencidas por requisição, cada UF no máximo 1 busca a cada
TSE_MAPA_REFRESH_SECONDS (com 1 visitante e polling de 60 s, as 27 em ~3 min). Uma UF que
falha no TSE fica com o último resumo bom (ou sem dado): nunca derruba as outras.
O mapa nunca diz "Eleito": não existe eleito por UF na eleição presidencial.
"""

import asyncio
import logging
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
    election = str(settings.tse_election_code)
    claimed = await repo.claim_ufs(settings.tse_mapa_refresh_seconds, settings.tse_mapa_lote)
    fetched = await asyncio.gather(*(_fetch(client, uf) for uf in claimed))
    fresh = [
        UfSnapshot(uf, result.eleicao, summarize(result), result.totalizado_em)
        for uf, result in zip(claimed, fetched, strict=True)
        if result is not None
    ]
    if fresh:
        await repo.save_ufs(fresh)
    saved = {s.uf: s for s in await repo.get_ufs()}
    out: list[tuple[str, dict[str, Any] | None]] = []
    for uf in UFS:
        snapshot = saved.get(uf)
        # Resumo de outra eleição (ensaio com o 1º turno) não aparece: a UF fica sem dado.
        valid = snapshot is not None and snapshot.eleicao == election and bool(snapshot.resumo)
        out.append((uf, snapshot.resumo if valid and snapshot is not None else None))
    return out


async def _fetch(client: TseClient, uf: str) -> TseResult | None:
    try:
        return parse_result(await client.fetch(uf))
    except TseUnavailableError as exc:
        logger.warning("tse_map_fetch_failed", extra={"scope": uf, "error": str(exc)})
    except TseFormatError as exc:
        logger.error("tse_map_format_error", extra={"scope": uf, "error": str(exc)})
    except Exception:  # bug no parser numa UF não derruba o mapa
        logger.exception("tse_map_parse_failed", extra={"scope": uf})
    return None
