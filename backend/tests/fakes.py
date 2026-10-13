"""Dublês de teste: chaves/tokens JWT, repositório de perfis em memória e Admin API falsa."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from app.repositories.checks import (
    AGENCIES_ONLY_VERSION,
    CacheHit,
    CheckSummary,
    LimitExceededError,
    Limits,
    LinkSubject,
    NewCheck,
    Started,
    StoredCheck,
    SubjectRef,
    TextSubject,
    new_public_id,
)
from app.repositories.profiles import NicknameTakenError, Profile, ProfileExistsError
from app.services.factcheck import AgencyCheck
from app.services.fetcher import FetchedPage
from app.services.gemini import AnalysisInput, CheckResult, GeminiQuotaExhaustedError
from app.services.url_guard import NormalizedUrl

TEST_SUPABASE_URL = "https://test-ref.supabase.co"
TEST_ISSUER = f"{TEST_SUPABASE_URL}/auth/v1"


class StaticJwks:
    """Substitui o PyJWKClient: devolve sempre a mesma chave pública."""

    def __init__(self, public_key: Any) -> None:
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        return SimpleNamespace(key=self.public_key)


def make_es256_keys() -> tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, private_key.public_key()


def make_token(
    private_key: Any,
    *,
    sub: str,
    email: str | None = "pessoa@example.com",
    provider: str = "google",
    issuer: str = TEST_ISSUER,
    audience: str = "authenticated",
    role: str = "authenticated",
    expires_in: timedelta = timedelta(hours=1),
    algorithm: str = "ES256",
    extra: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": sub,
        "aud": audience,
        "iss": issuer,
        "role": role,
        "iat": now,
        "exp": now + expires_in,
        "app_metadata": {"provider": provider, "providers": [provider]},
        "user_metadata": {},
    }
    if email is not None:
        claims["email"] = email
    claims.update(extra or {})
    return jwt.encode(claims, private_key, algorithm=algorithm)


class FakeProfileRepository:
    def __init__(self) -> None:
        self.profiles: dict[UUID, Profile] = {}
        self.google_subs: dict[UUID, str] = {}
        self.banned_hashes: set[bytes] = set()
        self.kept_suspensions: dict[bytes, datetime] = {}
        self.blocked_nicknames: list[tuple[str, str]] = []

    async def get(self, user_id: UUID) -> Profile | None:
        return self.profiles.get(user_id)

    async def nickname_taken(self, apelido: str) -> bool:
        return any(p.apelido.casefold() == apelido.casefold() for p in self.profiles.values())

    async def google_sub(self, user_id: UUID) -> str | None:
        return self.google_subs.get(user_id)

    async def is_banned(self, sub_hash: bytes) -> bool:
        return sub_hash in self.banned_hashes

    async def suspension_until(self, sub_hash: bytes) -> datetime | None:
        until = self.kept_suspensions.get(sub_hash)
        return until if until and until > datetime.now(UTC) else None

    async def keep_suspension(self, sub_hash: bytes, until: datetime) -> None:
        if sub_hash not in self.banned_hashes:
            self.kept_suspensions[sub_hash] = max(until, self.kept_suspensions.get(sub_hash, until))

    async def create(
        self,
        user_id: UUID,
        apelido: str,
        terms_version: str,
        *,
        suspended_until: datetime | None = None,
    ) -> Profile:
        if user_id in self.profiles:
            raise ProfileExistsError
        if await self.nickname_taken(apelido):
            raise NicknameTakenError
        profile = Profile(
            id=user_id,
            apelido=apelido,
            status="suspended" if suspended_until else "active",
            suspended_until=suspended_until,
            terms_version=terms_version,
            created_at=datetime.now(UTC),
        )
        self.profiles[user_id] = profile
        return profile

    async def log_nickname_blocked(self, apelido: str, category: str, rule: str) -> None:
        self.blocked_nicknames.append((apelido, category))

    def set_status(self, user_id: UUID, status: str, suspended_until: datetime | None) -> None:
        self.profiles[user_id] = replace(
            self.profiles[user_id], status=status, suspended_until=suspended_until
        )


class FakeSupabaseAdmin:
    def __init__(self) -> None:
        self.deleted: list[UUID] = []

    async def delete_user(self, user_id: UUID) -> None:
        self.deleted.append(user_id)


# ---------------------------------------------------------------------------------------
# Checagem (etapa 2)
# ---------------------------------------------------------------------------------------


@dataclass
class _FakeRow:
    ref: SubjectRef
    check: StoredCheck
    listed: bool
    created_by: UUID
    prompt_version: str = ""


def _agency_ratings(result: dict[str, object]) -> tuple[str, ...]:
    agencies = result.get("checagens_agencias")
    if not isinstance(agencies, list):
        return ()
    return tuple(a["veredito"] for a in agencies if isinstance(a, dict) and "veredito" in a)


def _agency_count(result: dict[str, object]) -> int:
    # Igual ao SQL de latest(): só lista conta; null ou objeto viram 0.
    agencies = result.get("checagens_agencias")
    return len(agencies) if isinstance(agencies, list) else 0


class FakeCheckRepository:
    """Repositório em memória com as mesmas regras do SQL (cache, limites, claim, cota)."""

    def __init__(self) -> None:
        self.link_ids: dict[bytes, int] = {}
        self.text_ids: dict[bytes, int] = {}
        self.link_data: dict[int, LinkSubject] = {}
        self.text_data: dict[int, TextSubject] = {}
        self.rows: list[_FakeRow] = []
        self.requests: dict[int, tuple[UUID, bool]] = {}
        # assunto -> token do claim (como o checking_until no banco)
        self.claimed: dict[SubjectRef, datetime] = {}
        self.busy = False  # simula outra instância checando o mesmo conteúdo
        self.gemini_used = 0
        self.model_used: dict[str, int] = {}
        self.saved: list[NewCheck] = []
        self._next_id = 0
        self._clock = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _fresh(self, ref: SubjectRef, prompt_version: str) -> StoredCheck | None:
        matches = [
            row.check
            for row in self.rows
            if row.ref == ref and row.prompt_version == prompt_version
        ]
        return matches[-1] if matches else None

    def _used(self, user_id: UUID, cache_hit: bool) -> int:
        return sum(1 for uid, hit in self.requests.values() if uid == user_id and hit == cache_hit)

    async def begin_check(
        self, user_id: UUID, subject: LinkSubject | TextSubject, limits: Limits
    ) -> CacheHit | Started:
        if isinstance(subject, LinkSubject):
            subject_id = self.link_ids.setdefault(subject.url_hash, self._id())
            self.link_data.setdefault(subject_id, subject)
            ref = SubjectRef("link", subject_id)
        else:
            subject_id = self.text_ids.setdefault(subject.text_hash, self._id())
            self.text_data.setdefault(subject_id, subject)
            ref = SubjectRef("texto", subject_id)

        fresh = self._fresh(ref, limits.prompt_version)
        if fresh is not None:
            if self._used(user_id, True) >= limits.lookups_per_hour:
                raise LimitExceededError("consultas", 1800)
            self.requests[self._id()] = (user_id, True)
            return CacheHit(fresh)
        if self._used(user_id, False) >= limits.checks_per_day:  # o fake não tem relógio
            raise LimitExceededError("diario", 43200)
        if self._used(user_id, False) >= limits.checks_per_hour:
            raise LimitExceededError("checagens", 1800)
        # Como no SQL: o claim vem junto, e o cache só é conferido de novo se havia outro
        # claim ativo antes (ele pode ter acabado e salvo no meio).
        busy = self.busy or ref in self.claimed
        request_id = self._id()
        self.requests[request_id] = (user_id, False)
        token = await self.claim(ref)
        if token is not None and busy:
            fresh = self._fresh(ref, limits.prompt_version)
            if fresh is not None:
                self.requests[request_id] = (user_id, True)
                await self.release(ref, token)
                return CacheHit(fresh)
        return Started(ref, request_id, token)

    async def claim(self, subject: SubjectRef) -> datetime | None:
        if self.busy or subject in self.claimed:
            return None
        self._clock += timedelta(seconds=1)
        self.claimed[subject] = self._clock
        return self._clock

    async def release(self, subject: SubjectRef, token: datetime) -> None:
        if self.claimed.get(subject) == token:  # só o dono solta
            del self.claimed[subject]

    async def find_fresh(
        self, subject: SubjectRef, cache_hours: int, prompt_version: str
    ) -> StoredCheck | None:
        return self._fresh(subject, prompt_version)

    async def find_previous(self, subject: SubjectRef, cache_hours: int) -> StoredCheck | None:
        matches = [
            row.check
            for row in self.rows
            if row.ref == subject and row.prompt_version != AGENCIES_ONLY_VERSION
        ]
        return matches[-1] if matches else None

    async def reserve_gemini(self, purpose: str, daily_limit: int) -> bool:
        if self.gemini_used >= daily_limit:
            return False
        self.gemini_used += 1
        return True

    async def save_result(self, check: NewCheck) -> StoredCheck:
        self.saved.append(check)
        if check.claim_token is not None and self.claimed.get(check.subject) == check.claim_token:
            del self.claimed[check.subject]
        self._clock += timedelta(minutes=1)
        link = self.link_data.get(check.subject.id) if check.subject.kind == "link" else None
        text = self.text_data.get(check.subject.id) if check.subject.kind == "texto" else None
        stored = StoredCheck(
            public_id=new_public_id(),
            kind=check.subject.kind,
            url=link.url if link else None,
            domain=link.domain if link else None,
            title=check.title if link else None,
            published_at=check.published_at if link else None,
            texto=text.body if text else None,
            veredito=check.veredito,
            confianca=check.confianca,
            result=check.result,
            created_at=self._clock,
            prompt_version=check.prompt_version,
        )
        self.rows.append(
            _FakeRow(check.subject, stored, check.listed, check.user_id, check.prompt_version)
        )
        return stored

    async def mark_cache_hit(self, request_id: int) -> None:
        if request_id in self.requests:
            self.requests[request_id] = (self.requests[request_id][0], True)

    async def convert_to_lookup(
        self, request_id: int, user_id: UUID, lookups_per_hour: int
    ) -> int | None:
        if self._used(user_id, True) >= lookups_per_hour:
            return 1800
        self.requests[request_id] = (user_id, True)
        return None

    async def reserve_gemini_model(self, model: str, daily_limit: int) -> bool:
        used = self.model_used.get(model, 0)
        if used >= daily_limit:
            return False
        self.model_used[model] = used + 1
        return True

    async def release_gemini(self, purpose: str) -> None:
        self.gemini_used = max(0, self.gemini_used - 1)

    async def refund(self, request_id: int) -> None:
        self.requests.pop(request_id, None)

    async def get(self, public_id: str) -> StoredCheck | None:
        return next((r.check for r in self.rows if r.check.public_id == public_id), None)

    async def latest(self, cursor: tuple[datetime, str] | None, limit: int) -> list[CheckSummary]:
        rows = sorted(
            (r for r in self.rows if r.listed and r.ref.kind == "link"),
            key=lambda r: (r.check.created_at, r.check.public_id),
            reverse=True,
        )
        newest: dict[SubjectRef, _FakeRow] = {}
        for r in rows:  # já em ordem decrescente: o primeiro de cada link é o mais novo
            newest.setdefault(r.ref, r)
        rows = list(newest.values())
        if cursor is not None:
            rows = [r for r in rows if (r.check.created_at, r.check.public_id) < cursor]
        return [
            CheckSummary(
                public_id=r.check.public_id,
                domain=r.check.domain or "",
                title=r.check.title,
                veredito=r.check.veredito,
                created_at=r.check.created_at,
                agency_checks=_agency_count(r.check.result),
                agency_ratings=_agency_ratings(r.check.result),
            )
            for r in rows[:limit]
        ]


ARTICLE_TEXT = (
    "A Justiça Eleitoral confirmou nesta segunda-feira o calendário do segundo turno das "
    "eleições em Roraima. Segundo o tribunal, a votação acontece normalmente no domingo, "
    "das 8h às 17h, em todas as seções eleitorais do estado. "
) * 3


def article_html(title: str = "Calendário do 2º turno é mantido em Roraima") -> bytes:
    return (
        f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
        f"<p>{ARTICLE_TEXT}</p></article></body></html>"
    ).encode()


class FakeFetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.html = article_html()
        self.error: Exception | None = None
        self.final_url: str | None = None  # simula redirect para outro endereço

    async def fetch(self, url: NormalizedUrl) -> FetchedPage:
        self.calls.append(url.url)
        if self.error is not None:
            raise self.error
        return FetchedPage(url=self.final_url or url.url, content=self.html, truncated=False)


class FakeFactCheck:
    def __init__(self) -> None:
        self.queries: list[list[str]] = []
        self.results: list[AgencyCheck] = []
        self.error: Exception | None = None

    async def search(self, queries: list[str]) -> list[AgencyCheck]:
        self.queries.append(queries)
        if self.error is not None:
            raise self.error
        return self.results


class FakeAnalyzer:
    def __init__(self) -> None:
        self.inputs: list[AnalysisInput] = []
        self.error: Exception | None = None
        self.veredito = "falso"
        self.confianca = 85
        self.model: str | None = None  # modelo que "respondeu" (None = não informado)

    async def analyze(
        self,
        data: AnalysisInput,
        reserve: Callable[[], Awaitable[bool]] | None = None,
        release: Callable[[], Awaitable[None]] | None = None,
        model_budget: Callable[[str, int], Awaitable[bool]] | None = None,
    ) -> CheckResult:
        if reserve is not None and not await reserve():
            raise GeminiQuotaExhaustedError
        self.inputs.append(data)
        if self.error is not None:
            raise self.error
        return CheckResult(
            veredito=self.veredito,  # type: ignore[arg-type]
            veredito_ia=self.veredito,  # type: ignore[arg-type]
            rebaixado_por_confianca=False,
            confianca=self.confianca,
            resumo="A votação não foi adiada.",
            pontos_suspeitos=["Sem fonte oficial"],
            contexto_faltante=None,
            checagens_agencias=data.agency_checks,
            fontes_sugeridas=["Site oficial do TSE"],
            model=self.model,
        )
