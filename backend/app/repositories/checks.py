"""Checagens de links e de textos: cache, limites por usuário, single-flight e cota diária."""

import json
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

SubjectKind = Literal["link", "texto"]
LimitKind = Literal["checagens", "consultas", "diario"]
GeminiPurpose = Literal["checagem", "moderacao"]

# Base32 de Crockford em minúsculas: sem i, l, o, u (não se confundem ao ditar/copiar).
PUBLIC_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
PUBLIC_ID_LENGTH = 12
_PUBLIC_ID_ATTEMPTS = 3
# Versão reservada do resultado "só agências" (Gemini sem cota ou no limite por minuto):
# nunca é cache (a chave é a versão do prompt) nem "análise anterior" (não é análise de IA).
AGENCIES_ONLY_VERSION = "agencias"


class GlobalQuotaRefused:
    """Tipo do sentinela GLOBAL_QUOTA_REFUSED (instância única)."""

    def __repr__(self) -> str:
        return "GLOBAL_QUOTA_REFUSED"


# reserve_gemini_and_model: a cota GLOBAL do dia acabou (nada foi gasto).
GLOBAL_QUOTA_REFUSED = GlobalQuotaRefused()


class _ModelQuotaRefused(Exception):
    """Interno: desfaz a unidade global quando a do modelo é recusada (rollback)."""


# Validade do claim de single-flight: maior que a pior checagem (20 + 12 + 60 = 92 s).
CLAIM_TTL_SECONDS = 120


def new_public_id() -> str:
    return "".join(secrets.choice(PUBLIC_ID_ALPHABET) for _ in range(PUBLIC_ID_LENGTH))


@dataclass(frozen=True)
class LinkSubject:
    url: str
    url_hash: bytes
    domain: str


@dataclass(frozen=True)
class TextSubject:
    body: str
    text_hash: bytes


@dataclass(frozen=True)
class SubjectRef:
    kind: SubjectKind
    id: int


@dataclass(frozen=True)
class StoredCheck:
    public_id: str
    kind: SubjectKind
    url: str | None
    domain: str | None
    title: str | None
    published_at: date | None
    texto: str | None
    veredito: str
    confianca: int
    result: dict[str, Any]
    created_at: datetime
    # Versão do prompt que gerou a análise (None = antes de existir a coluna).
    prompt_version: str | None = None


@dataclass(frozen=True)
class CheckSummary:
    public_id: str
    domain: str
    title: str | None
    veredito: str
    created_at: datetime
    # Quantas checagens de agência a checagem tem: 0 + "nao_verificavel" = "Ainda sem checagem".
    agency_checks: int = 0
    # Classificações dessas checagens: a leitura reaplica a regra do "verdadeiro".
    agency_ratings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CacheHit:
    check: StoredCheck


@dataclass(frozen=True)
class Started:
    subject: SubjectRef
    request_id: int
    # Claim tomado junto com o begin_check; None = outra requisição está checando.
    # Obrigatório: esquecê-lo mandaria a requisição para a espera sem aviso.
    claim_token: datetime | None


class LimitExceededError(Exception):
    def __init__(self, kind: LimitKind, retry_after_seconds: int) -> None:
        super().__init__(kind)
        self.kind: LimitKind = kind
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Limits:
    cache_hours: int
    checks_per_hour: int
    lookups_per_hour: int
    # Checagens novas em 24 h (janela móvel). Consultas ao cache não contam.
    checks_per_day: int = 20
    # Cache só vale para resultados desta versão do prompt (gemini.PROMPT_VERSION). Sem
    # padrão de propósito: esquecer a versão quebraria o cache em silêncio.
    prompt_version: str = field(kw_only=True)


@dataclass(frozen=True)
class NewCheck:
    subject: SubjectRef
    user_id: UUID
    veredito: str
    confianca: int
    result: dict[str, Any]
    model: str
    listed: bool
    title: str | None = None
    published_at: date | None = None
    # Token do claim (o checking_until gravado por ESTA requisição): só ela o solta.
    claim_token: datetime | None = None
    # Versão do prompt que produziu o resultado (chave do cache). Obrigatória.
    prompt_version: str = field(kw_only=True)


class CheckRepository(Protocol):
    async def begin_check(
        self, user_id: UUID, subject: LinkSubject | TextSubject, limits: Limits
    ) -> CacheHit | Started: ...
    async def claim(self, subject: SubjectRef) -> datetime | None: ...
    async def release(self, subject: SubjectRef, token: datetime) -> None: ...
    async def find_fresh(
        self, subject: SubjectRef, cache_hours: int, prompt_version: str
    ) -> StoredCheck | None: ...
    async def find_previous(self, subject: SubjectRef, cache_hours: int) -> StoredCheck | None: ...
    async def reserve_gemini(self, purpose: GeminiPurpose, daily_limit: int) -> bool: ...
    async def save_result(self, check: NewCheck) -> StoredCheck: ...
    async def mark_cache_hit(self, request_id: int) -> None: ...
    async def convert_to_lookup(
        self, request_id: int, user_id: UUID, lookups_per_hour: int
    ) -> int | None: ...
    async def release_gemini(self, purpose: GeminiPurpose) -> None: ...
    async def reserve_gemini_model(self, model: str, daily_limit: int) -> date | None: ...
    async def release_gemini_model(self, model: str, day: date) -> None: ...
    async def reserve_gemini_and_model(
        self, purpose: GeminiPurpose, purpose_limit: int, model: str, model_limit: int
    ) -> date | GlobalQuotaRefused | None: ...
    async def save_lookup_result(
        self, request_id: int, user_id: UUID, lookups_per_hour: int, check: NewCheck
    ) -> StoredCheck | int: ...
    async def refund(self, request_id: int) -> None: ...
    async def get(self, public_id: str) -> StoredCheck | None: ...
    async def latest(
        self, cursor: tuple[datetime, str] | None, limit: int
    ) -> list[CheckSummary]: ...


# Nomes de tabela e coluna vêm SEMPRE destes mapas (nunca da entrada): sem risco de injeção.
_SUBJECT_TABLE = {"link": "public.links", "texto": "public.texts"}
_SUBJECT_COLUMN = {"link": "link_id", "texto": "text_id"}

_SELECT_CHECK = """
select c.public_id,
       case when c.link_id is not null then 'link' else 'texto' end as kind,
       l.url_normalized as url, l.domain, l.title, l.published_at,
       t.body as texto,
       c.veredito, c.confianca, c.result, c.created_at, c.prompt_version
from public.checks c
left join public.links l on l.id = c.link_id
left join public.texts t on t.id = c.text_id
"""

# Cada ida ao banco custa ~130 ms (Render na Virginia, Supabase em São Paulo; o ensaio de
# 12/10/2026 mediu db_ms=5252). Por isso o begin_check faz 3 comandos numa transação e o
# save_result, um só.

# 1º comando: lock único por usuário (requisições paralelas não estouram os limites) + id do
# assunto. "no key update" (e não "update"): não conflita com o FOR KEY SHARE que as FKs para
# profiles tomam (insert em checks no save_result), o que evitava deadlock entre duas abas.
# O insert só roda se o assunto não existe (links populares: sem escrita, sem bloat); na
# corrida de duas inserções, "do nothing" devolve vazio e _FIND_SUBJECT busca de novo.
_LOCK_AND_SUBJECT = """
with lock as (select 1 from public.profiles where id = :uid for no key update),
ins as (
  insert into {table} ({hash_column}, {columns})
  select :hash, {values}
  where not exists (select 1 from {table} where {hash_column} = :hash)
  on conflict ({hash_column}) do nothing
  returning id
)
select (select count(*) from lock) as locked,  -- referência que faz o lock rodar
       coalesce((select id from ins),
                (select id from {table} where {hash_column} = :hash)) as id
"""
_SUBJECT_SQL = {
    "link": ("url_hash", "url_normalized, domain", ":url, :domain"),
    "texto": ("text_hash", "body", ":body"),
}
_FIND_SUBJECT = "select id from {table} where {hash_column} = :hash"

# 2º comando (depois do lock): uso nas janelas + resultado em cache + se há claim ativo.
# Uso: quantos e em quantos segundos vence o mais antigo (Retry-After); o índice
# (user_id, created_at desc) atende. clock_timestamp() (e não now(), que é o início da
# transação): quem esperou o lock começou antes de a outra requisição gravar, e now() daria
# um Retry-After maior que a janela.
_USAGE_WINDOWS = (
    ("lookups", "cache_hit and created_at > now() - interval '1 hour'", "1 hour"),
    ("daily", "not cache_hit", "24 hours"),
    ("hourly", "not cache_hit and created_at > now() - interval '1 hour'", "1 hour"),
)
# Só constantes (nunca entrada) na montagem: sem risco de injeção.
_USAGE = (
    "select "  # noqa: S608
    + ", ".join(
        f"count(*) filter (where {cond}) as {name}_used, "
        f"ceil(extract(epoch from (min(created_at) filter (where {cond}) "
        f"+ interval '{window}' - clock_timestamp())))::int as {name}_retry"
        for name, cond, window in _USAGE_WINDOWS
    )
    + (
        " from public.check_requests "
        "where user_id = :uid and created_at > now() - interval '24 hours'"
    )
)

# Claim do single-flight, o mesmo no begin_check e no claim() (a espera assume por ele).
# clock_timestamp() (e não now(), o início da transação, que no begin_check inclui a espera
# do lock do usuário): o claim dura mesmo CLAIM_TTL_SECONDS a partir de agora.
_CLAIM = """
update {table} set checking_until = clock_timestamp() + make_interval(secs => :ttl)
where id = :sid and (checking_until is null or checking_until < clock_timestamp())
returning checking_until
"""
# 3º comando do begin_check: o pedido conta no limite e o claim é tomado, juntos.
_INSERT_REQUEST_AND_CLAIM = """
with r as (
  insert into public.check_requests (user_id, {column}, cache_hit)
  values (:uid, :sid, false) returning id
),
c as ({claim})
select (select id from r) as request_id, (select checking_until from c) as token
"""

# Pedido vira consulta (modo "só agências"), dentro do limite de consultas por hora.
_CONVERT_TO_LOOKUP = """
with u as (
  select count(*) as used,
         ceil(extract(epoch from (
           min(created_at) + interval '1 hour' - clock_timestamp()
         )))::int as retry
  from public.check_requests
  where user_id = :uid and cache_hit and created_at > now() - interval '1 hour'
),
c as (
  update public.check_requests set cache_hit = true
  where id = :rid and (select used from u) < :limit
  returning 1
)
select exists (select 1 from c) as converted, (select retry from u) as retry
"""

# Contador diário POR MODELO (RPD do nível gratuito): modelo esgotado nem é chamado.
_RESERVE_GEMINI_MODEL = """
insert into public.gemini_model_usage as u (day, model, calls)
values ((now() at time zone 'America/Los_Angeles')::date, :model, 1)
on conflict (day, model) do update set calls = u.calls + 1
where u.calls < :limit
returning day
"""

# Devolução (503/500: o Google recusou sem processar). Dia da RESERVA, não now(): a chamada
# pode ter cruzado a meia-noite do Pacífico. Nunca abaixo de 0.
_RELEASE_GEMINI_MODEL = """
update public.gemini_model_usage set calls = calls - 1
where day = :day and model = :model and calls > 0
"""

_RELEASE_GEMINI = """
update public.gemini_usage set calls = calls - 1
where day = (now() at time zone 'America/Los_Angeles')::date
  and purpose = :purpose and calls > 0
"""

_RESERVE_GEMINI = """
insert into public.gemini_usage as u (day, purpose, calls)
values ((now() at time zone 'America/Los_Angeles')::date, :purpose, 1)
on conflict (day, purpose) do update set calls = u.calls + 1
where u.calls < :limit
returning calls
"""


def _ratings(raw: Any) -> tuple[str, ...]:
    """Classificações das agências vindas do jsonb (texto no asyncpg); só strings valem."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    return tuple(r for r in raw if isinstance(r, str)) if isinstance(raw, list) else ()


def _row_to_check(row: Any) -> StoredCheck:
    result = row["result"]
    if isinstance(result, str):  # asyncpg devolve jsonb como texto
        result = json.loads(result)
    # Título da própria checagem (o da tabela links é o da última checagem listada).
    own_title = result.get("title") if isinstance(result, dict) else None
    return StoredCheck(
        public_id=row["public_id"],
        kind=row["kind"],
        url=row["url"],
        domain=row["domain"],
        title=own_title or row["title"],
        published_at=row["published_at"],
        texto=row["texto"],
        veredito=row["veredito"],
        confianca=row["confianca"],
        result=result,
        created_at=row["created_at"],
        prompt_version=row["prompt_version"],
    )


def _enforce(state: RowMapping, window: str, limit: int, kind: LimitKind) -> None:
    if state[f"{window}_used"] >= limit:
        raise LimitExceededError(kind, max(1, int(state[f"{window}_retry"] or 1)))


# Gravação num comando só: solta o claim (se ainda for nosso), atualiza o título do link (só
# checagem LISTADA: título moderado, mesmo domínio; o título de cada checagem fica no result)
# e insere a checagem. Os dados do assunto voltam do próprio update (o select de fora não
# veria o título novo).
_SAVE_SUBJECT = {
    "link": """
update public.links set
  checking_until = case when checking_until = :token then null else checking_until end,
  title = case when :listed then coalesce(:title, title) else title end,
  published_at = case when :listed then coalesce(:published_at, published_at)
                      else published_at end
where id = :sid
returning 'link'::text as kind, url_normalized as url, domain, title, published_at,
          null::text as texto
""",
    "texto": """
update public.texts set
  checking_until = case when checking_until = :token then null else checking_until end
where id = :sid
returning 'texto'::text as kind, null::text as url, null::text as domain, null::text as title,
          null::date as published_at, body as texto
""",
}
_SAVE_CHECK = """
with s as ({subject}),
c as (
  insert into public.checks ({column}, veredito, confianca, result, model, created_by,
                             public_id, listed, prompt_version)
  values (:sid, :veredito, :confianca, cast(:result as jsonb), :model, :uid, :public_id,
          :listed, :prompt_version)
  returning public_id, veredito, confianca, result, created_at, prompt_version
)
select c.public_id, s.kind, s.url, s.domain, s.title, s.published_at, s.texto,
       c.veredito, c.confianca, c.result, c.created_at, c.prompt_version
from c cross join s
"""


class SqlCheckRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def begin_check(
        self, user_id: UUID, subject: LinkSubject | TextSubject, limits: Limits
    ) -> CacheHit | Started:
        """Limites + cache + claim numa transação de 3 comandos (lock+assunto, uso+cache,
        pedido+claim). Started.claim_token None = outra requisição está checando."""
        async with self._session.begin():
            ref = await self._lock_and_subject(user_id, subject)
            state = await self._usage_and_fresh(user_id, ref, limits)

            if state["public_id"] is not None:
                _enforce(state, "lookups", limits.lookups_per_hour, "consultas")
                await self._insert_request(user_id, ref, cache_hit=True)
                return CacheHit(check=_row_to_check(state))

            # O teto diário primeiro: se os dois estouraram, o Retry-After certo é o maior.
            _enforce(state, "daily", limits.checks_per_day, "diario")
            _enforce(state, "hourly", limits.checks_per_hour, "checagens")
            row = (
                (
                    await self._session.execute(
                        text(
                            _INSERT_REQUEST_AND_CLAIM.format(
                                column=_SUBJECT_COLUMN[ref.kind],
                                claim=_CLAIM.format(table=_SUBJECT_TABLE[ref.kind]),
                            )
                        ),
                        {"uid": user_id, "sid": ref.id, "ttl": CLAIM_TTL_SECONDS},
                    )
                )
                .mappings()
                .one()
            )
            request_id, token = int(row["request_id"]), row["token"]
            if token is not None and state["busy"]:
                # Havia outro claim ativo no 2º comando e ele acabou antes do nosso: pode ter
                # deixado resultado. Sem claim ativo no 2º comando, ninguém checa e salva até
                # o 3º (uma checagem leva segundos, e o claim dura mais que a pior delas).
                fresh = await self._find_fresh(ref, limits.cache_hours, limits.prompt_version)
                if fresh is not None:
                    await self._reuse_finished(ref, request_id, token)
                    return CacheHit(check=fresh)
            return Started(subject=ref, request_id=request_id, claim_token=token)

    async def _lock_and_subject(
        self, user_id: UUID, subject: LinkSubject | TextSubject
    ) -> SubjectRef:
        if isinstance(subject, LinkSubject):
            kind: SubjectKind = "link"
            params: dict[str, Any] = {
                "hash": subject.url_hash,
                "url": subject.url,
                "domain": subject.domain,
            }
        else:
            kind = "texto"
            params = {"hash": subject.text_hash, "body": subject.body}
        hash_column, columns, values = _SUBJECT_SQL[kind]
        names = {"table": _SUBJECT_TABLE[kind], "hash_column": hash_column}
        sql = _LOCK_AND_SUBJECT.format(**names, columns=columns, values=values)
        result = await self._session.execute(text(sql), params | {"uid": user_id})
        subject_id = result.mappings().one()["id"]
        if subject_id is None:  # outra transação inseriu ao mesmo tempo
            find = _FIND_SUBJECT.format(**names)
            subject_id = await self._session.scalar(text(find), {"hash": params["hash"]})
        return SubjectRef(kind=kind, id=int(subject_id))

    async def _usage_and_fresh(self, user_id: UUID, ref: SubjectRef, limits: Limits) -> RowMapping:
        """Uma linha: uso nas janelas, se há claim ativo e o resultado em cache (colunas de
        _SELECT_CHECK, todas nulas sem cache)."""
        table = _SUBJECT_TABLE[ref.kind]
        sql = (
            f"select u.*, coalesce((select s.checking_until > clock_timestamp() from {table} s "  # noqa: S608
            "where s.id = :sid), false) as busy, f.* "
            f"from ({_USAGE}) u left join lateral ({self._fresh_sql(ref)}) f on true"
        )
        params = {
            "uid": user_id,
            "sid": ref.id,
            "hours": limits.cache_hours,
            "prompt_version": limits.prompt_version,
        }
        return (await self._session.execute(text(sql), params)).mappings().one()

    async def _reuse_finished(self, ref: SubjectRef, request_id: int, token: datetime) -> None:
        """Resultado de quem acabou de checar: o pedido vira consulta e o claim é solto."""
        await self._session.execute(
            text(
                "with m as (update public.check_requests set cache_hit = true where id = :rid) "  # noqa: S608
                f"update {_SUBJECT_TABLE[ref.kind]} set checking_until = null "
                "where id = :sid and checking_until = :token"
            ),
            {"rid": request_id, "sid": ref.id, "token": token},
        )

    async def _insert_request(self, user_id: UUID, ref: SubjectRef, *, cache_hit: bool) -> int:
        column = _SUBJECT_COLUMN[ref.kind]
        request_id = await self._session.scalar(
            text(
                f"insert into public.check_requests (user_id, {column}, cache_hit) "  # noqa: S608
                "values (:uid, :sid, :cache_hit) returning id"
            ),
            {"uid": user_id, "sid": ref.id, "cache_hit": cache_hit},
        )
        return int(request_id)

    @staticmethod
    def _fresh_sql(ref: SubjectRef) -> str:
        return (
            _SELECT_CHECK + f"where c.{_SUBJECT_COLUMN[ref.kind]} = :sid "
            "and c.created_at > now() - make_interval(hours => :hours) "
            # Prompt mudou = resultado antigo não é cache (null também não).
            "and c.prompt_version = :prompt_version "
            "order by c.created_at desc limit 1"
        )

    async def _find_fresh(
        self, ref: SubjectRef, cache_hours: int, prompt_version: str
    ) -> StoredCheck | None:
        row = (
            (
                await self._session.execute(
                    text(self._fresh_sql(ref)),
                    {"sid": ref.id, "hours": cache_hours, "prompt_version": prompt_version},
                )
            )
            .mappings()
            .first()
        )
        return _row_to_check(row) if row else None

    async def find_fresh(
        self, subject: SubjectRef, cache_hours: int, prompt_version: str
    ) -> StoredCheck | None:
        async with self._session.begin():
            return await self._find_fresh(subject, cache_hours, prompt_version)

    async def find_previous(self, subject: SubjectRef, cache_hours: int) -> StoredCheck | None:
        """A análise mais recente do assunto, de QUALQUER versão do prompt (sem cota do
        Gemini, ela é servida com o aviso "análise anterior")."""
        column = _SUBJECT_COLUMN[subject.kind]
        async with self._session.begin():
            row = (
                (
                    await self._session.execute(
                        text(
                            _SELECT_CHECK + f"where c.{column} = :sid "
                            "and c.created_at > now() - make_interval(hours => :hours) "
                            "and c.prompt_version is distinct from :agencies "
                            "order by c.created_at desc limit 1"
                        ),
                        {
                            "sid": subject.id,
                            "hours": cache_hours,
                            "agencies": AGENCIES_ONLY_VERSION,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return _row_to_check(row) if row else None

    async def claim(self, subject: SubjectRef) -> datetime | None:
        """Toma o claim e devolve o token (o checking_until gravado), ou None se ocupado."""
        sql = _CLAIM.format(table=_SUBJECT_TABLE[subject.kind])
        async with self._session.begin():
            token = await self._session.scalar(
                text(sql), {"sid": subject.id, "ttl": CLAIM_TTL_SECONDS}
            )
        return token

    async def release(self, subject: SubjectRef, token: datetime) -> None:
        # Só solta o PRÓPRIO claim: se o nosso expirou e outra requisição tomou o lugar,
        # o checking_until é outro e nada muda.
        table = _SUBJECT_TABLE[subject.kind]
        async with self._session.begin():
            await self._session.execute(
                text(
                    f"update {table} set checking_until = null "  # noqa: S608
                    "where id = :id and checking_until = :token"
                ),
                {"id": subject.id, "token": token},
            )

    async def reserve_gemini(self, purpose: GeminiPurpose, daily_limit: int) -> bool:
        if daily_limit <= 0:
            return False
        async with self._session.begin():
            calls = await self._session.scalar(
                text(_RESERVE_GEMINI), {"purpose": purpose, "limit": daily_limit}
            )
        return calls is not None

    async def save_result(self, check: NewCheck) -> StoredCheck:
        for attempt in range(1, _PUBLIC_ID_ATTEMPTS + 1):
            try:
                return await self._insert_check(check, new_public_id())
            except IntegrityError as exc:
                # 60 bits aleatórios: colisão é rara; tenta outro id.
                if attempt == _PUBLIC_ID_ATTEMPTS or "checks_public_id_key" not in str(exc.orig):
                    raise
        raise AssertionError("inalcançável")

    async def _insert_check(self, check: NewCheck, public_id: str) -> StoredCheck:
        sql, params = self._save_statement(check, public_id)
        async with self._session.begin():
            row = (await self._session.execute(text(sql), params)).mappings().one()
        return _row_to_check(row)

    @staticmethod
    def _save_statement(check: NewCheck, public_id: str) -> tuple[str, dict[str, Any]]:
        kind = check.subject.kind
        sql = _SAVE_CHECK.format(subject=_SAVE_SUBJECT[kind], column=_SUBJECT_COLUMN[kind])
        params: dict[str, Any] = {
            "sid": check.subject.id,
            "token": check.claim_token,
            "veredito": check.veredito,
            "confianca": check.confianca,
            "result": json.dumps(check.result, ensure_ascii=False),
            "model": check.model,
            "uid": check.user_id,
            "public_id": public_id,
            "listed": check.listed,
            "prompt_version": check.prompt_version,
        }
        if kind == "link":
            params |= {"title": check.title, "published_at": check.published_at}
        return sql, params

    async def mark_cache_hit(self, request_id: int) -> None:
        """O resultado veio de outra checagem: vira consulta ao cache (limite de consultas)."""
        async with self._session.begin():
            await self._session.execute(
                text("update public.check_requests set cache_hit = true where id = :id"),
                {"id": request_id},
            )

    async def convert_to_lookup(
        self, request_id: int, user_id: UUID, lookups_per_hour: int
    ) -> int | None:
        """O pedido passa a contar como consulta. None = convertido; senão, os segundos até
        vagar uma consulta (Retry-After). Sob o lock do usuário, como o begin_check."""
        async with self._session.begin():
            await self._session.execute(
                text("select 1 from public.profiles where id = :uid for no key update"),
                {"uid": user_id},
            )
            row = (
                (
                    await self._session.execute(
                        text(_CONVERT_TO_LOOKUP),
                        {"uid": user_id, "rid": request_id, "limit": lookups_per_hour},
                    )
                )
                .mappings()
                .one()
            )
        if row["converted"]:
            return None
        return max(1, int(row["retry"] or 1))

    async def reserve_gemini_model(self, model: str, daily_limit: int) -> date | None:
        """Dia (do Pacífico) em que a unidade foi reservada, ou None sem cota hoje."""
        if daily_limit <= 0:
            return None
        async with self._session.begin():
            day: date | None = await self._session.scalar(
                text(_RESERVE_GEMINI_MODEL), {"model": model, "limit": daily_limit}
            )
        return day

    async def reserve_gemini_and_model(
        self, purpose: GeminiPurpose, purpose_limit: int, model: str, model_limit: int
    ) -> date | GlobalQuotaRefused | None:
        """Cota global + do modelo numa transação (cada uma custa ~0,7 s entre a Virginia e
        São Paulo). Global esgotada: GLOBAL_QUOTA_REFUSED, nada gasto. Modelo esgotado: None,
        e a unidade global é desfeita (rollback). Senão, o dia (do Pacífico) da reserva."""
        if purpose_limit <= 0:  # o upsert inseriria a 1ª unidade do dia sem conferir o limite
            return GLOBAL_QUOTA_REFUSED
        if model_limit <= 0:
            return None
        try:
            async with self._session.begin():
                taken = await self._session.scalar(
                    text(_RESERVE_GEMINI), {"purpose": purpose, "limit": purpose_limit}
                )
                if taken is None:
                    return GLOBAL_QUOTA_REFUSED
                day: date | None = await self._session.scalar(
                    text(_RESERVE_GEMINI_MODEL), {"model": model, "limit": model_limit}
                )
                if day is None:
                    raise _ModelQuotaRefused
                return day
        except _ModelQuotaRefused:
            return None

    async def save_lookup_result(
        self, request_id: int, user_id: UUID, lookups_per_hour: int, check: NewCheck
    ) -> StoredCheck | int:
        """Modo "só agências" numa transação: o pedido vira consulta (sob o lock do usuário,
        como o begin_check) e o resultado é gravado. Sem vaga de consulta: os segundos até
        vagar (Retry-After), sem gravar nada."""
        for attempt in range(1, _PUBLIC_ID_ATTEMPTS + 1):
            sql, params = self._save_statement(check, new_public_id())
            try:
                async with self._session.begin():
                    await self._session.execute(
                        text("select 1 from public.profiles where id = :uid for no key update"),
                        {"uid": user_id},
                    )
                    converted = (
                        (
                            await self._session.execute(
                                text(_CONVERT_TO_LOOKUP),
                                {"uid": user_id, "rid": request_id, "limit": lookups_per_hour},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    if not converted["converted"]:
                        return max(1, int(converted["retry"] or 1))
                    row = (await self._session.execute(text(sql), params)).mappings().one()
                return _row_to_check(row)
            except IntegrityError as exc:
                # 60 bits aleatórios: colisão é rara; a transação inteira volta e tenta outro id.
                if attempt == _PUBLIC_ID_ATTEMPTS or "checks_public_id_key" not in str(exc.orig):
                    raise
        raise AssertionError("inalcançável")

    async def release_gemini_model(self, model: str, day: date) -> None:
        async with self._session.begin():
            await self._session.execute(text(_RELEASE_GEMINI_MODEL), {"model": model, "day": day})

    async def release_gemini(self, purpose: GeminiPurpose) -> None:
        async with self._session.begin():
            await self._session.execute(text(_RELEASE_GEMINI), {"purpose": purpose})

    async def refund(self, request_id: int) -> None:
        async with self._session.begin():
            await self._session.execute(
                text("delete from public.check_requests where id = :id"), {"id": request_id}
            )

    async def get(self, public_id: str) -> StoredCheck | None:
        async with self._session.begin():
            row = (
                (
                    await self._session.execute(
                        text(_SELECT_CHECK + "where c.public_id = :pid"), {"pid": public_id}
                    )
                )
                .mappings()
                .first()
            )
        return _row_to_check(row) if row else None

    async def latest(self, cursor: tuple[datetime, str] | None, limit: int) -> list[CheckSummary]:
        sql = (
            "select c.public_id, l.domain, l.title, c.veredito, c.created_at, "
            # jsonb_array_length falha em null/objeto: um resultado torto não derruba "Últimas".
            "case when jsonb_typeof(c.result -> 'checagens_agencias') = 'array' "
            "then jsonb_array_length(c.result -> 'checagens_agencias') else 0 end "
            "as agency_checks, "
            "case when jsonb_typeof(c.result -> 'checagens_agencias') = 'array' "
            "then jsonb_path_query_array(c.result -> 'checagens_agencias', '$[*].veredito') "
            "else '[]'::jsonb end as agency_ratings "
            "from public.checks c join public.links l on l.id = c.link_id "
            "where c.listed "
            # Um item por link: só a checagem listada mais recente (nova versão do prompt não
            # duplica o link). Anti-join em vez de distinct on: percorre checks_listed_idx em
            # ordem e para no limit; o índice checks_listed_link_idx atende o not exists.
            "and not exists (select 1 from public.checks n where n.link_id = c.link_id "
            "and n.listed and (n.created_at, n.public_id) > (c.created_at, c.public_id)) "
        )
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            sql += "and (c.created_at, c.public_id) < (:cursor_at, :cursor_id) "
            params |= {"cursor_at": cursor[0], "cursor_id": cursor[1]}
        sql += "order by c.created_at desc, c.public_id desc limit :limit"
        async with self._session.begin():
            rows = (await self._session.execute(text(sql), params)).mappings().all()
        return [
            CheckSummary(**{**row, "agency_ratings": _ratings(row.get("agency_ratings"))})
            for row in rows
        ]
