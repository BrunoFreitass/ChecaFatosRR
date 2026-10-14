"""Leituras do painel do admin (plano mapa-e-painel-admin §B): só contagens agregadas.

Nada por pessoa: nenhum id, apelido ou e-mail sai daqui. Um comando só, numa transação: cada
ida e volta custa ~0,1 s e cada transação ~0,7 s entre Virgínia (Render) e São Paulo (Supabase).
Dias no horário de Boa Vista (o do público), calculados aqui (zoneinfo): não depende da base
de fusos do servidor. A cota do Gemini no dia do Pacífico, igual às reservas (checks.py).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DAYS = 7
BOA_VISTA = ZoneInfo("America/Boa_Vista")
DAILY = ("checagens", "tokens", "pedidos", "comentarios", "denuncias", "usuarios_novos")

# Janela por dia com início e fim em timestamptz: as consultas usam os índices de created_at.
# Tokens só quando são número (o result é jsonb livre): um valor estranho não derruba o painel.
_PAINEL = """
with dias as (
  select * from unnest(
    cast(:dias as date[]), cast(:inicios as timestamptz[]), cast(:fins as timestamptz[])
  ) as x(dia, inicio, fim)
),
checagens as (
  select d.dia,
         count(c.id) as total,
         count(c.id) filter (where c.veredito = 'verdadeiro') as verdadeiro,
         count(c.id) filter (where c.veredito = 'enganoso') as enganoso,
         count(c.id) filter (where c.veredito = 'falso') as falso,
         count(c.id) filter (where c.veredito = 'fora_de_contexto') as fora_de_contexto,
         count(c.id) filter (where c.veredito = 'nao_verificavel') as nao_verificavel,
         count(c.id) filter (where c.prompt_version = 'agencias') as so_agencias,
         count(c.id) filter (where jsonb_typeof(c.result -> 'tokens') = 'object') as com_tokens,
         coalesce(sum(case when jsonb_typeof(c.result -> 'tokens' -> 'in') = 'number'
                           then (c.result -> 'tokens' ->> 'in')::numeric end), 0)::bigint
           as tokens_in,
         coalesce(sum(case when jsonb_typeof(c.result -> 'tokens' -> 'out') = 'number'
                           then (c.result -> 'tokens' ->> 'out')::numeric end), 0)::bigint
           as tokens_out,
         coalesce(sum(case when jsonb_typeof(c.result -> 'tokens' -> 'thoughts') = 'number'
                           then (c.result -> 'tokens' ->> 'thoughts')::numeric end), 0)::bigint
           as tokens_thoughts
  from dias d
  left join public.checks c on c.created_at >= d.inicio and c.created_at < d.fim
  group by d.dia
),
pedidos as (
  select d.dia, count(r.id) as total, count(r.id) filter (where r.cache_hit) as do_cache
  from dias d
  left join public.check_requests r on r.created_at >= d.inicio and r.created_at < d.fim
  group by d.dia
),
comentarios as (
  select d.dia, count(c.id) as total, count(c.id) filter (where c.status <> 'visible') as ocultos
  from dias d
  left join public.comments c on c.created_at >= d.inicio and c.created_at < d.fim
  group by d.dia
),
denuncias as (
  select d.dia, count(r.comment_id) as total
  from dias d
  left join public.comment_reports r on r.created_at >= d.inicio and r.created_at < d.fim
  group by d.dia
),
usuarios_novos as (
  select d.dia, count(p.id) as total
  from dias d
  left join public.profiles p on p.created_at >= d.inicio and p.created_at < d.fim
  group by d.dia
)
select json_build_object(
  'checagens', (
    select json_agg(json_build_object(
      'dia', x.dia, 'total', x.total, 'verdadeiro', x.verdadeiro, 'enganoso', x.enganoso,
      'falso', x.falso, 'fora_de_contexto', x.fora_de_contexto,
      'nao_verificavel', x.nao_verificavel, 'so_agencias', x.so_agencias) order by x.dia)
    from checagens x),
  'tokens', (
    select json_agg(json_build_object(
      'dia', x.dia, 'checagens', x.com_tokens, 'entrada', x.tokens_in, 'saida', x.tokens_out,
      'raciocinio', x.tokens_thoughts) order by x.dia)
    from checagens x),
  'pedidos', (select json_agg(x order by x.dia) from pedidos x),
  'comentarios', (select json_agg(x order by x.dia) from comentarios x),
  'denuncias', (select json_agg(x order by x.dia) from denuncias x),
  'usuarios_novos', (select json_agg(x order by x.dia) from usuarios_novos x),
  'fila', (
    select json_build_object(
      'pendentes', count(*),
      'revisao', count(*) filter (where kind = 'comment_review'),
      'confirmacao_strike', count(*) filter (where kind = 'strike_confirmation'))
    from public.moderation_queue where status = 'pending'),
  'usuarios', (
    select json_build_object(
      'total', count(*),
      'suspensos', count(*) filter (where status = 'suspended' and suspended_until > now()),
      'banidos', count(*) filter (where status = 'banned'))
    from public.profiles),
  'dia_pacifico', (now() at time zone 'America/Los_Angeles')::date,
  'cota', (
    select coalesce(json_object_agg(purpose, calls), '{}'::json) from public.gemini_usage
    where day = (now() at time zone 'America/Los_Angeles')::date),
  'modelos', (
    select coalesce(json_object_agg(model, calls), '{}'::json) from public.gemini_model_usage
    where day = (now() at time zone 'America/Los_Angeles')::date)
)::text
"""


@dataclass(frozen=True)
class PainelData:
    """Contagens cruas do banco; o router junta os limites da configuração."""

    daily: dict[str, list[dict[str, Any]]]
    fila: dict[str, int]
    usuarios: dict[str, int]
    pacific_day: date
    quota_used: dict[str, int]
    model_used: dict[str, int]


def day_windows(now: datetime) -> dict[str, list[Any]]:
    """Os últimos DAYS dias de Boa Vista (o mais antigo primeiro), com início e fim de cada um."""
    today = now.astimezone(BOA_VISTA).date()
    days = [today - timedelta(days=n) for n in range(DAYS - 1, -1, -1)]
    return {
        "dias": days,
        "inicios": [datetime.combine(d, time(), BOA_VISTA) for d in days],
        "fins": [datetime.combine(d + timedelta(days=1), time(), BOA_VISTA) for d in days],
    }


class SqlPainelRepository:
    def __init__(
        self, session: AsyncSession, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._session = session
        self._now = now

    async def read(self) -> PainelData:
        params = day_windows(self._now())
        async with self._session.begin():
            raw = (await self._session.execute(text(_PAINEL), params)).scalar_one()
        data = json.loads(raw)
        return PainelData(
            daily={key: data[key] for key in DAILY},
            fila=data["fila"],
            usuarios=data["usuarios"],
            pacific_day=date.fromisoformat(data["dia_pacifico"]),
            quota_used={str(k): int(v) for k, v in data["cota"].items()},
            model_used={str(k): int(v) for k, v in data["modelos"].items()},
        )
