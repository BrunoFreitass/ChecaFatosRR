"""Checagens de links e de textos: cache, limites por usuário, single-flight e cota diária."""

import json
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

SubjectKind = Literal["link", "texto"]
LimitKind = Literal["checagens", "consultas", "diario"]
GeminiPurpose = Literal["checagem", "moderacao"]

# Base32 de Crockford em minúsculas: sem i, l, o, u (não se confundem ao ditar/copiar).
PUBLIC_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
PUBLIC_ID_LENGTH = 12
_PUBLIC_ID_ATTEMPTS = 3
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


@dataclass(frozen=True)
class CacheHit:
    check: StoredCheck


@dataclass(frozen=True)
class Started:
    subject: SubjectRef
    request_id: int


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

# Busca antes de inserir: o assunto quase sempre já existe (links populares), e um upsert
# "do update" reescreveria a linha (bloat) e seguraria lock de escrita nela até o commit.
# "do nothing" + nova busca cobre a corrida de duas inserções simultâneas.
_FIND_LINK = "select id from public.links where url_hash = :hash"
_INSERT_LINK = """
insert into public.links (url_hash, url_normalized, domain)
values (:hash, :url, :domain)
on conflict (url_hash) do nothing
returning id
"""
_FIND_TEXT = "select id from public.texts where text_hash = :hash"
_INSERT_TEXT = """
insert into public.texts (text_hash, body)
values (:hash, :body)
on conflict (text_hash) do nothing
returning id
"""

# Usos na janela (1 h ou 24 h): quantos e em quantos segundos vence o mais antigo
# (Retry-After). O índice (user_id, created_at desc) atende às duas janelas.
# clock_timestamp() (e não now(), que é o início da transação): quem esperou o lock
# começou antes de a outra requisição gravar, e now() daria um Retry-After maior que a janela.
_USAGE = """
select count(*) as used,
       ceil(extract(epoch from (
         min(created_at) + make_interval(hours => :hours) - clock_timestamp()
       )))::int as retry
from public.check_requests
where user_id = :uid and cache_hit = :cache_hit
  and created_at > now() - make_interval(hours => :hours)
"""

_RESERVE_GEMINI = """
insert into public.gemini_usage as u (day, purpose, calls)
values ((now() at time zone 'America/Los_Angeles')::date, :purpose, 1)
on conflict (day, purpose) do update set calls = u.calls + 1
where u.calls < :limit
returning calls
"""


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


class SqlCheckRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def begin_check(
        self, user_id: UUID, subject: LinkSubject | TextSubject, limits: Limits
    ) -> CacheHit | Started:
        async with self._session.begin():
            # Lock único por usuário: requisições paralelas não estouram os limites.
            # "no key update" (e não "update"): não conflita com o FOR KEY SHARE que as FKs
            # para profiles tomam (ex.: insert em checks no save_result), o que evitava
            # deadlock entre duas abas do mesmo usuário checando o mesmo link.
            await self._session.execute(
                text("select 1 from public.profiles where id = :uid for no key update"),
                {"uid": user_id},
            )
            ref = await self._subject_ref(subject)

            fresh = await self._find_fresh(ref, limits.cache_hours, limits.prompt_version)
            if fresh is not None:
                await self._check_usage(user_id, True, limits.lookups_per_hour, "consultas", 1)
                await self._insert_request(user_id, ref, cache_hit=True)
                return CacheHit(check=fresh)

            # O teto diário primeiro: se os dois estouraram, o Retry-After certo é o maior.
            await self._check_usage(user_id, False, limits.checks_per_day, "diario", 24)
            await self._check_usage(user_id, False, limits.checks_per_hour, "checagens", 1)
            request_id = await self._insert_request(user_id, ref, cache_hit=False)
            return Started(subject=ref, request_id=request_id)

    async def _subject_ref(self, subject: LinkSubject | TextSubject) -> SubjectRef:
        if isinstance(subject, LinkSubject):
            kind: SubjectKind = "link"
            find, insert = _FIND_LINK, _INSERT_LINK
            params: dict[str, Any] = {
                "hash": subject.url_hash,
                "url": subject.url,
                "domain": subject.domain,
            }
        else:
            kind = "texto"
            find, insert = _FIND_TEXT, _INSERT_TEXT
            params = {"hash": subject.text_hash, "body": subject.body}
        lookup = {"hash": params["hash"]}
        subject_id = await self._session.scalar(text(find), lookup)
        if subject_id is None:
            subject_id = await self._session.scalar(text(insert), params)
        if subject_id is None:  # outra transação inseriu ao mesmo tempo
            subject_id = await self._session.scalar(text(find), lookup)
        return SubjectRef(kind=kind, id=int(subject_id))

    async def _check_usage(
        self, user_id: UUID, cache_hit: bool, limit: int, kind: LimitKind, hours: int
    ) -> None:
        result = await self._session.execute(
            text(_USAGE), {"uid": user_id, "cache_hit": cache_hit, "hours": hours}
        )
        row = result.mappings().one()
        if row["used"] >= limit:
            raise LimitExceededError(kind, max(1, int(row["retry"] or 1)))

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

    async def _find_fresh(
        self, ref: SubjectRef, cache_hours: int, prompt_version: str
    ) -> StoredCheck | None:
        column = _SUBJECT_COLUMN[ref.kind]
        row = (
            (
                await self._session.execute(
                    text(
                        _SELECT_CHECK + f"where c.{column} = :sid "
                        "and c.created_at > now() - make_interval(hours => :hours) "
                        # Prompt mudou = resultado antigo não é cache (null também não).
                        "and c.prompt_version = :prompt_version "
                        "order by c.created_at desc limit 1"
                    ),
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
                            "order by c.created_at desc limit 1"
                        ),
                        {"sid": subject.id, "hours": cache_hours},
                    )
                )
                .mappings()
                .first()
            )
        return _row_to_check(row) if row else None

    async def claim(self, subject: SubjectRef) -> datetime | None:
        """Toma o claim e devolve o token (o checking_until gravado), ou None se ocupado."""
        table = _SUBJECT_TABLE[subject.kind]
        async with self._session.begin():
            token = await self._session.scalar(
                text(
                    f"update {table} set checking_until = now() + make_interval(secs => :ttl) "  # noqa: S608
                    "where id = :id and (checking_until is null or checking_until < now()) "
                    "returning checking_until"
                ),
                {"id": subject.id, "ttl": CLAIM_TTL_SECONDS},
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
        column = _SUBJECT_COLUMN[check.subject.kind]
        table = _SUBJECT_TABLE[check.subject.kind]
        async with self._session.begin():
            # O título do link é o que "Últimas" mostra: só uma checagem LISTADA (título
            # moderado, mesmo domínio) o atualiza. O título de cada checagem fica no result.
            if check.subject.kind == "link" and check.listed:
                await self._session.execute(
                    text(
                        "update public.links set title = coalesce(:title, title), "
                        "published_at = coalesce(:published_at, published_at) where id = :id"
                    ),
                    {
                        "title": check.title,
                        "published_at": check.published_at,
                        "id": check.subject.id,
                    },
                )
            if check.claim_token is not None:
                await self._session.execute(
                    text(
                        f"update {table} set checking_until = null "  # noqa: S608
                        "where id = :id and checking_until = :token"
                    ),
                    {"id": check.subject.id, "token": check.claim_token},
                )
            await self._session.execute(
                text(
                    f"insert into public.checks ({column}, veredito, confianca, result, "  # noqa: S608
                    "model, created_by, public_id, listed, prompt_version) values (:sid, "
                    ":veredito, :confianca, cast(:result as jsonb), :model, :uid, :public_id, "
                    ":listed, :prompt_version)"
                ),
                {
                    "sid": check.subject.id,
                    "veredito": check.veredito,
                    "confianca": check.confianca,
                    "result": json.dumps(check.result, ensure_ascii=False),
                    "model": check.model,
                    "uid": check.user_id,
                    "public_id": public_id,
                    "listed": check.listed,
                    "prompt_version": check.prompt_version,
                },
            )
            row = (
                (
                    await self._session.execute(
                        text(_SELECT_CHECK + "where c.public_id = :pid"), {"pid": public_id}
                    )
                )
                .mappings()
                .one()
            )
        return _row_to_check(row)

    async def mark_cache_hit(self, request_id: int) -> None:
        """O resultado veio de outra checagem: vira consulta ao cache (limite de consultas)."""
        async with self._session.begin():
            await self._session.execute(
                text("update public.check_requests set cache_hit = true where id = :id"),
                {"id": request_id},
            )

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
            "then jsonb_array_length(c.result -> 'checagens_agencias') else 0 end as agency_checks "
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
        return [CheckSummary(**row) for row in rows]
