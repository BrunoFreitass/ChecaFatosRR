import base64
import binascii
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, current_user
from app.config import Settings, get_settings
from app.db import get_session, session_scope
from app.errors import AppError
from app.legal import CURRENT_TERMS_VERSION
from app.repositories.checks import (
    AGENCIES_ONLY_VERSION,
    CheckRepository,
    CheckSummary,
    Limits,
    SqlCheckRepository,
    StoredCheck,
)
from app.repositories.profiles import Profile, ProfileRepository
from app.routers.profiles import get_profile_repository
from app.schemas.checks import (
    AgencyCheckOut,
    AgencyPageOut,
    CheckCreate,
    CheckOut,
    CheckSummaryOut,
    LatestChecksOut,
)
from app.services.check_service import CheckSettings, CheckTools, run_check
from app.services.factcheck import (
    AgencyCheck,
    FactCheckUnavailableError,
    HttpFactCheckClient,
    same_page,
)
from app.services.fetcher import HttpPageFetcher
from app.services.gemini import PROMPT_VERSION, GeminiAnalyzer, google_generate
from app.services.model_limits import ModelLimits
from app.services.moderation.local_filter import check_text
from app.services.read_cache import ReadCaches, get_read_caches
from app.services.relevance import content_keys
from app.services.verdict_policy import (
    AGENCIES_ONLY_SEARCH_FAILED,
    AGENCY_PAGE_MODE,
    NO_EXACT_CHECK_SENTENCE,
    OLD_NOT_CONFIRMED_SENTENCE,
    agency_page_summary,
    enforce_relevance,
    neutral_summary,
    relevant_verdict,
    split_relevant,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["checagem"])

_PUBLIC_ID = re.compile(r"^[0-9a-hjkmnp-tv-z]{12}$")
_LATEST_PAGE = 20


def get_check_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CheckRepository:
    return SqlCheckRepository(session)


class _NoFactCheck:
    """Sem GOOGLE_FACTCHECK_API_KEY: a checagem segue, mas a busca nas agências NÃO foi feita,
    então fica registrada como falha (nunca como "não encontramos")."""

    async def search(self, queries: list[str]) -> list[AgencyCheck]:
        raise FactCheckUnavailableError


@lru_cache(maxsize=2)
def _analyzer(
    api_key: str,
    models: tuple[str, ...],
    thinking_budget: int,
    thinking_level: str,
    attempt_timeout: float,
    max_retries: int,
    budget_seconds: float,
    limits: tuple[tuple[str, ModelLimits], ...],
) -> GeminiAnalyzer:
    # Um cliente do SDK por processo (reaproveita conexões).
    generate = google_generate(
        api_key,
        models,
        thinking_budget=thinking_budget,
        thinking_level=thinking_level,
        attempt_timeout=attempt_timeout,
        max_retries=max_retries,
        budget_seconds=budget_seconds,
        limits=dict(limits),
    )
    return GeminiAnalyzer(generate, budget_seconds=budget_seconds)


def get_check_tools(settings: Annotated[Settings, Depends(get_settings)]) -> CheckTools:
    if not settings.gemini_api_key:
        logger.error("gemini_api_key_missing")
        raise AppError(
            503, "checagem_indisponivel", "A checagem automática está indisponível no momento."
        )
    if settings.google_factcheck_api_key:
        factcheck = HttpFactCheckClient(settings.google_factcheck_api_key)
    else:
        logger.warning("factcheck_api_key_missing")
        factcheck = _NoFactCheck()
    return CheckTools(
        fetcher=HttpPageFetcher(
            timeout_seconds=settings.fetch_timeout_seconds, max_bytes=settings.fetch_max_bytes
        ),
        factcheck=factcheck,
        analyzer=_analyzer(
            settings.gemini_api_key,
            settings.gemini_models,
            settings.gemini_thinking_budget,
            settings.gemini_thinking_level,
            settings.gemini_attempt_timeout_seconds,
            settings.gemini_max_retries,
            settings.gemini_budget_seconds,
            tuple(sorted(settings.model_limits.items())),  # hashable para o lru_cache
        ),
    )


def get_check_settings(settings: Annotated[Settings, Depends(get_settings)]) -> CheckSettings:
    return CheckSettings(
        limits=Limits(
            cache_hours=settings.check_cache_hours,
            checks_per_hour=settings.check_rate_limit_per_hour,
            checks_per_day=settings.check_daily_limit_per_user,
            lookups_per_hour=settings.cache_lookup_limit_per_hour,
            prompt_version=PROMPT_VERSION,
        ),
        daily_gemini_checks=settings.gemini_daily_checks,
        # Rótulo de reserva: a checagem grava o modelo que respondeu (result.model).
        model=settings.gemini_models[0],
        open_platforms=settings.open_platforms,
    )


async def current_profile(
    user: Annotated[CurrentUser, Depends(current_user)],
    profiles: Annotated[ProfileRepository, Depends(get_profile_repository)],
) -> Profile:
    profile = await profiles.get(user.id)
    if profile is None:
        raise AppError(
            404,
            "profile_not_found",
            "Escolha um apelido para continuar.",
            {"terms_version": CURRENT_TERMS_VERSION},
        )
    return profile


Repo = Annotated[CheckRepository, Depends(get_check_repository)]
CheckRepos = Callable[[], AbstractAsyncContextManager[CheckRepository]]


@asynccontextmanager
async def _own_check_repo() -> AsyncIterator[CheckRepository]:
    async with session_scope() as session:
        yield SqlCheckRepository(session)


def get_check_repos() -> CheckRepos:
    """Repositório com sessão própria: a leitura do cache serve vários pedidos ao mesmo tempo
    e não pode usar a sessão de um deles (fecharia se ele desistisse)."""
    return _own_check_repo


def _search_status(result: dict[str, Any], agencies_only: bool) -> Literal["ok", "falhou"] | None:
    """Status gravado na checagem. Antigas do modo "só agências" (de antes do registro) têm o
    resumo honesto, escrito por nós, que já distinguia a falha; as outras antigas: None."""
    status = result.get("busca_agencias")
    if status in ("ok", "falhou"):
        return status  # type: ignore[no-any-return]
    if agencies_only:
        return "falhou" if result.get("resumo") == AGENCIES_ONLY_SEARCH_FAILED else "ok"
    return None


def _keys(result: dict[str, Any]) -> list[str] | None:
    """Palavras-chave gravadas (desde 19/10). None = checagem antiga, sem o campo; lista vazia
    gravada continua vazia (o critério é o do momento da checagem)."""
    raw = result.get("palavras_chave")
    return [k for k in raw if isinstance(k, str)] if isinstance(raw, list) else None


def _or_fallback(keys: list[str] | None, title: str | None, texto: str | None) -> list[str] | None:
    """Antigas (sem palavras-chave gravadas): título e texto. "Últimas" só lista links, e link
    não tem texto: página e lista usam o mesmo critério. None = antiga sem palavra-chave
    nenhuma: sem informação, a salvaguarda não se aplica."""
    if keys is not None:
        return keys
    return content_keys(title, texto) or None


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def agency_page_from(kind: str, url: str | None, result: Mapping[str, Any]) -> AgencyPageOut | None:
    """O link é a própria checagem de uma agência? A mesma regra na página e em "Últimas".

    Gravada assim desde 23/10 (a classificação pode faltar: link de agência conhecida fora do
    índice, 26/10). As de antes (IA chamada, "não verificável") são reconhecidas pela URL igual
    à de uma checagem gravada (citada ou relacionada), sem reescrever o banco."""
    stored = result.get("checagem_de_agencia")
    if result.get("modo") == AGENCY_PAGE_MODE and isinstance(stored, dict):
        agencia, page_url = _text(stored.get("agencia")), _text(stored.get("url"))
        rating, titulo = _text(stored.get("veredito")), _text(stored.get("titulo"))
        # Com classificação, o título da checagem é obrigatório (a página e o WhatsApp o
        # citam); sem ela (site fora do índice), o título pode faltar.
        if not (agencia and page_url) or (rating and not titulo):
            return None
        return AgencyPageOut(
            agencia=agencia,
            url=page_url,
            classificacao=rating,
            alegacao=_text(stored.get("alegacao")),
            titulo=titulo or "",
        )
    if kind != "link":
        return None
    for key in ("checagens_agencias", "checagens_relacionadas"):
        items = result.get(key)
        for item in items if isinstance(items, list) else []:
            if not (isinstance(item, dict) and same_page(url, item.get("url"))):
                continue
            agencia, rating, titulo = (
                _text(item.get(field)) for field in ("agencia", "veredito", "titulo")
            )
            if agencia and rating and titulo:
                return AgencyPageOut(
                    agencia=agencia,
                    url=str(item["url"]),
                    classificacao=rating,
                    alegacao=_text(item.get("alegacao")),
                    titulo=titulo,
                )
    return None


def _agency_page_out(check: StoredCheck, page: AgencyPageOut, *, cached: bool) -> CheckOut:
    """Sem veredito nem confiança nossos: só o que a agência publicou nessa página."""
    return CheckOut(
        id=check.public_id,
        tipo=check.kind,
        url=check.url,
        domain=check.domain,
        title=check.title,
        published_at=check.published_at,
        texto=check.texto,
        veredito="nao_verificavel",
        rebaixado_por_confianca=False,
        confianca=0,
        resumo=agency_page_summary(page.agencia, classificacao=page.classificacao),
        pontos_suspeitos=[],
        contexto_faltante=None,
        checagens_agencias=[],
        fontes_sugeridas=[],
        created_at=check.created_at,
        cache=cached,
        modo=AGENCY_PAGE_MODE,
        checagem_de_agencia=page,
        # O status gravado (a busca pode ter falhado no link de agência fora do índice).
        busca_agencias=_search_status(check.result, False) or "ok",
    )


def to_out(check: StoredCheck, settings: CheckSettings, *, cached: bool = False) -> CheckOut:
    page = agency_page_from(check.kind, check.url, check.result)
    if page is not None:
        return _agency_page_out(check, page, cached=cached)
    result = check.result
    agencies_only = check.prompt_version == AGENCIES_ONLY_VERSION
    busca_agencias = _search_status(result, agencies_only)
    # Regras de hoje também na leitura: corrige checagens salvas antes delas ("verdadeiro" sem
    # agência; checagem citada de outro assunto sustentando "Verdadeiro" ou "Falso" < 70%), sem
    # mexer no banco. Palavras-chave gravadas (desde 19/10) ou, nas antigas, título/texto.
    keys = _or_fallback(_keys(result), check.title, check.texto)
    enforced = enforce_relevance(
        veredito=check.veredito,
        confianca=check.confianca,
        result={**result, "busca_agencias": busca_agencias},
        keys=keys,
        # Antiga sem palavra-chave nenhuma no título/texto: sem informação para julgar o
        # assunto, a salvaguarda não se aplica (só a lista vazia GRAVADA é "tudo irrelevante").
        skip=keys is None,
        kind=check.kind,
        domain=check.domain,
        title=check.title,
        agencies_only=agencies_only,
    )
    result = enforced.result
    veredito = enforced.veredito
    downgraded = enforced.reason == "verdadeiro"
    agencies = [item for item in result.get("checagens_agencias") or [] if isinstance(item, dict)]
    resumo = str(result.get("resumo", ""))
    # "Verdadeiro" já rebaixado pelo finalize (gravado como nao_verificavel com o texto neutro
    # "não encontramos"), mas com a busca nas agências FALHADA: refaz o texto neutro dizendo
    # que a busca falhou. Na leitura, com o que está gravado (finalize e prompt intocados).
    rewritten_failed_search = (
        busca_agencias == "falhou"
        and veredito == "nao_verificavel"
        and result.get("veredito_ia") == "verdadeiro"
        and not agencies
    )
    if rewritten_failed_search:
        # Checagens de antes de 11/10 não guardaram o domínio final (redirect): sem esse dado,
        # nada de "origem confirmada".
        origin = result.get("origem_confirmada")
        resumo = neutral_summary(
            check.kind,
            origin if isinstance(origin, str) else check.domain,
            check.title,
            confirmed_origin=isinstance(origin, str),
            agency_checks=bool(agencies),
            search_failed=busca_agencias == "falhou",
        )
    if veredito == "nao_verificavel":
        # Checagens de antes de 23/10: a frase "não confirmam" sugeria que a agência desmentia.
        resumo = resumo.replace(OLD_NOT_CONFIRMED_SENTENCE, NO_EXACT_CHECK_SENTENCE)
    return CheckOut(
        id=check.public_id,
        tipo=check.kind,
        url=check.url,
        domain=check.domain,
        title=check.title,
        published_at=check.published_at,
        texto=check.texto,
        veredito=veredito,  # type: ignore[arg-type]
        rebaixado_por_confianca=bool(result.get("rebaixado_por_confianca", False)),
        rebaixado_sem_agencia=downgraded or bool(result.get("rebaixado_sem_agencia", False)),
        confianca=check.confianca,
        resumo=resumo,
        pontos_suspeitos=list(result.get("pontos_suspeitos") or []),
        contexto_faltante=result.get("contexto_faltante"),
        checagens_agencias=[AgencyCheckOut(**item) for item in agencies],
        checagens_relacionadas=[
            AgencyCheckOut(**item)
            for item in result.get("checagens_relacionadas") or []
            if isinstance(item, dict)
        ],
        fontes_sugeridas=list(result.get("fontes_sugeridas") or []),
        created_at=check.created_at,
        cache=cached,
        modo="agencias" if agencies_only else "ia",
        # Versão diferente OU desconhecida (checagem de antes da coluna, None): avisa. O modo
        # "só agências" não é análise, nem anterior.
        analise_anterior=not agencies_only
        and check.prompt_version != settings.limits.prompt_version,
        busca_agencias=busca_agencias,
    )


@router.post("/checks", response_model=CheckOut)
async def create_check(
    payload: CheckCreate,
    profile: Annotated[Profile, Depends(current_profile)],
    repo: Repo,
    tools: Annotated[CheckTools, Depends(get_check_tools)],
    settings: Annotated[CheckSettings, Depends(get_check_settings)],
    caches: Annotated[ReadCaches, Depends(get_read_caches)],
) -> CheckOut:
    # Suspensão vale para comentários; banido não usa o site.
    if profile.effective_status(datetime.now(UTC)) == "banned":
        raise AppError(
            403, "conta_banida", "Esta conta foi banida por violar as regras do ChecaFatosRR."
        )
    outcome = None
    try:
        outcome = await run_check(
            user_id=profile.id,
            url=payload.url,
            texto=payload.texto,
            repo=repo,
            tools=tools,
            settings=settings,
        )
    finally:
        caches.latest.invalidate()  # checagem nova aparece na home na hora
        # Análise nova (inclusive reanálise do mesmo link) pode mudar título/data que as páginas
        # antigas mostram; falha no meio também limpa, por segurança. Reaproveitar a análise
        # existente (cached) não muda nada: não esvazia o cache num pico de acessos.
        if outcome is None or not outcome.cached:
            caches.checks.invalidate()
    return to_out(outcome.check, settings, cached=outcome.cached)


@router.get("/checks/latest", response_model=LatestChecksOut)
async def latest_checks(
    repos: Annotated[CheckRepos, Depends(get_check_repos)],
    caches: Annotated[ReadCaches, Depends(get_read_caches)],
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = _LATEST_PAGE,
) -> LatestChecksOut:
    position = _decode_cursor(cursor) if cursor else None  # cursor inválido: 400 antes do cache

    async def load() -> LatestChecksOut:
        async with repos() as repo:
            return await _latest(repo, position, limit)

    # Lista pública, igual para todos: a mesma leitura serve todo mundo por alguns segundos.
    return await caches.latest.get((position, limit), load)  # cursor já decodificado


async def _latest(
    repo: CheckRepository, position: tuple[datetime, str] | None, limit: int
) -> LatestChecksOut:
    items = await repo.latest(position, limit)
    next_cursor = (
        _encode_cursor(items[-1].created_at, items[-1].public_id) if len(items) == limit else None
    )
    summaries = [_summary(item) for item in items]
    return LatestChecksOut(
        # Checagem de agência entra pelo modo gravado (as de antes de 26/10 foram gravadas sem
        # listar): o mesmo filtro de título e domínio ofensivos que decide o "listed".
        items=[s for s in summaries if s.modo != AGENCY_PAGE_MODE or _listable_summary(s)],
        next_cursor=next_cursor,
    )


def _listable_summary(item: CheckSummaryOut) -> bool:
    return not any(check_text(value).blocked for value in (item.title or "", item.domain) if value)


def _summary(item: CheckSummary) -> CheckSummaryOut:
    """Item de "Últimas" com as mesmas regras da página: checagem de agência (selo "Checagem de
    <agência>" e a classificação dela) e salvaguarda de relevância."""
    page = agency_page_from("link", item.url, item.agency_view)
    if page is not None:
        return CheckSummaryOut(
            id=item.public_id,
            domain=item.domain,
            title=item.title or page.titulo or None,
            veredito="nao_verificavel",
            created_at=item.created_at,
            modo=AGENCY_PAGE_MODE,
            agencia=page.agencia,
            classificacao=page.classificacao,
        )
    keys = _or_fallback(None if item.keywords is None else list(item.keywords), item.title, None)
    cited = [{"titulo": t, "veredito": r} for t, r in item.agency_pairs]
    same, _ = split_relevant(cited, keys) if keys is not None else (cited, [])
    veredito, _reason = relevant_verdict(item.veredito, item.confianca, same)
    return CheckSummaryOut(
        id=item.public_id,
        domain=item.domain,
        title=item.title,
        veredito=veredito,  # type: ignore[arg-type]
        created_at=item.created_at,
        total_agencias=len(same),
    )


@router.get("/checks/{public_id}", response_model=CheckOut)
async def read_check(
    public_id: str,
    repos: Annotated[CheckRepos, Depends(get_check_repos)],
    settings: Annotated[CheckSettings, Depends(get_check_settings)],
    caches: Annotated[ReadCaches, Depends(get_read_caches)],
) -> CheckOut:
    # Formato inválido (ex.: /checks/1) nem chega ao banco.
    if not _PUBLIC_ID.match(public_id):
        raise _not_found()

    async def load() -> CheckOut:
        async with repos() as repo:
            check = await repo.get(public_id)
        if check is None:
            raise _not_found()  # não fica no cache: o próximo pedido procura de novo
        return to_out(check, settings)

    # Página pública, igual para todos (o handler não lê login): pode ser compartilhada. A versão
    # do prompt entra na chave: "análise anterior" depende dela (troca de versão = página nova).
    return await caches.checks.get((public_id, settings.limits.prompt_version), load)


def _not_found() -> AppError:
    return AppError(404, "checagem_nao_encontrada", "Checagem não encontrada.")


def _encode_cursor(created_at: datetime, public_id: str) -> str:
    raw = json.dumps([created_at.isoformat(), public_id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at, public_id = json.loads(base64.urlsafe_b64decode(padded))
        parsed = datetime.fromisoformat(created_at)
        # Sem fuso, o asyncpg não compara com timestamptz (500): só cursores que nós geramos.
        if parsed.tzinfo is None:
            raise ValueError("cursor sem fuso")
        if not isinstance(public_id, str) or not _PUBLIC_ID.match(public_id):
            raise ValueError("cursor")
    except (ValueError, TypeError, binascii.Error) as exc:
        raise AppError(422, "cursor_invalido", "Não foi possível carregar mais itens.") from exc
    return parsed, public_id
