"""Snapshots da apuração (tabela tse_snapshots): o último resultado bom por escopo.

fetched_at também é o "claim" do refresh: só quem consegue atualizar fetched_at (passados
stale_seconds desde a última tentativa) busca no TSE. Funciona entre requisições e entre
instâncias, sem lock no processo.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tse.mapa import UfSnapshot


@dataclass(frozen=True)
class Snapshot:
    scope: str
    payload: dict[str, Any]  # {} = escopo reivindicado, mas ainda sem resultado
    tse_updated_at: datetime | None
    fetched_at: datetime


class TseRepository(Protocol):
    async def claim(self, scope: str, stale_seconds: int) -> bool: ...
    async def save(
        self, scope: str, payload: dict[str, Any], tse_updated_at: datetime | None
    ) -> None: ...
    async def get_all(self) -> list[Snapshot]: ...


class SqlTseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, scope: str, stale_seconds: int) -> bool:
        """True = esta requisição busca no TSE agora (a linha nasce no primeiro claim)."""
        sql = (
            "insert into public.tse_snapshots (scope, payload) values (:scope, '{}'::jsonb) "
            "on conflict (scope) do update set fetched_at = now() "
            "where public.tse_snapshots.fetched_at "
            "< now() - cast(:stale as integer) * interval '1 second' "
            "returning 1"
        )
        async with self._session.begin():
            row = (
                await self._session.execute(text(sql), {"scope": scope, "stale": stale_seconds})
            ).first()
        return row is not None

    async def save(
        self, scope: str, payload: dict[str, Any], tse_updated_at: datetime | None
    ) -> None:
        """Grava se for mais novo que o salvo (ou de outra eleição: troca do ensaio)."""
        sql = (
            "update public.tse_snapshots set payload = cast(:payload as jsonb), "
            "tse_updated_at = cast(:ts as timestamptz) "
            "where scope = :scope and (tse_updated_at is null "
            "or cast(:ts as timestamptz) is null "
            "or cast(:ts as timestamptz) >= tse_updated_at "
            "or payload ->> 'eleicao' is distinct from cast(:eleicao as text))"
        )
        params = {
            "scope": scope,
            "payload": json.dumps(payload, ensure_ascii=False),
            "ts": tse_updated_at,
            "eleicao": payload.get("eleicao"),
        }
        async with self._session.begin():
            await self._session.execute(text(sql), params)

    async def get_all(self) -> list[Snapshot]:
        sql = "select scope, payload, tse_updated_at, fetched_at from public.tse_snapshots"
        async with self._session.begin():
            rows = (await self._session.execute(text(sql))).mappings().all()
        return [
            Snapshot(
                scope=row["scope"],
                payload=_payload(row["payload"]),
                tse_updated_at=row["tse_updated_at"],
                fetched_at=row["fetched_at"],
            )
            for row in rows
        ]


def _payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):  # asyncpg devolve jsonb como texto
        raw = json.loads(raw)
    return raw if isinstance(raw, dict) else {}


# Mapa por UF (tabela tse_uf_snapshots, migration 20261015000000). As 27 linhas já existem.
# "skip locked": duas requisições ao mesmo tempo nunca pegam a mesma UF. A condição de vencida
# vai também no update externo (revisão de banco, 14/10/2026). Nunca ler fetched_at no Python:
# a linha nunca buscada tem '-infinity' (datetime não representa).
_CLAIM_UFS = """
update public.tse_uf_snapshots set fetched_at = now()
where uf in (
  select uf from public.tse_uf_snapshots
  where fetched_at < now() - cast(:stale as integer) * interval '1 second'
  order by fetched_at
  limit :lote
  for update skip locked
)
and fetched_at < now() - cast(:stale as integer) * interval '1 second'
returning uf
"""

# Grava se for mais novo que o salvo, ou de outra eleição (troca do ensaio). Um comando só.
_SAVE_UFS = """
update public.tse_uf_snapshots t
set resumo = n.resumo, eleicao = n.eleicao, tse_updated_at = n.ts
from (
  select * from unnest(
    cast(:ufs as text[]), cast(:eleicoes as text[]), cast(:resumos as jsonb[]),
    cast(:tss as timestamptz[])
  ) as x(uf, eleicao, resumo, ts)
) n
where t.uf = n.uf
  and (t.tse_updated_at is null or n.ts is null or n.ts >= t.tse_updated_at
       or t.eleicao is distinct from n.eleicao)
"""


class SqlTseMapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_ufs(self, stale_seconds: int, limit: int) -> list[str]:
        async with self._session.begin():
            rows = await self._session.execute(
                text(_CLAIM_UFS), {"stale": stale_seconds, "lote": limit}
            )
            return [row[0] for row in rows]

    async def save_ufs(self, items: list[UfSnapshot]) -> None:
        params = {
            "ufs": [i.uf for i in items],
            "eleicoes": [i.eleicao for i in items],
            "resumos": [json.dumps(i.resumo, ensure_ascii=False) for i in items],
            "tss": [i.tse_updated_at for i in items],
        }
        async with self._session.begin():
            await self._session.execute(text(_SAVE_UFS), params)

    async def get_ufs(self) -> list[UfSnapshot]:
        async with self._session.begin():
            rows = (
                (
                    await self._session.execute(
                        text(
                            "select uf, eleicao, resumo, tse_updated_at "
                            "from public.tse_uf_snapshots"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [
            UfSnapshot(r["uf"], r["eleicao"], _payload(r["resumo"]), r["tse_updated_at"])
            for r in rows
        ]
