import base64
import binascii
import json
import logging
import re
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, current_user
from app.config import Settings, get_settings
from app.db import get_session
from app.errors import AppError
from app.legal import CURRENT_TERMS_VERSION
from app.repositories.checks import (
    AGENCIES_ONLY_VERSION,
    CheckRepository,
    Limits,
    SqlCheckRepository,
    StoredCheck,
)
from app.repositories.profiles import Profile, ProfileRepository
from app.routers.profiles import get_profile_repository
from app.schemas.checks import (
    AgencyCheckOut,
    CheckCreate,
    CheckOut,
    CheckSummaryOut,
    LatestChecksOut,
)
from app.services.check_service import CheckSettings, CheckTools, run_check
from app.services.factcheck import AgencyCheck, HttpFactCheckClient
from app.services.fetcher import HttpPageFetcher
from app.services.gemini import PROMPT_VERSION, GeminiAnalyzer, google_generate
from app.services.model_limits import ModelLimits
from app.services.verdict_policy import neutral_summary, read_verdict

logger = logging.getLogger(__name__)
router = APIRouter(tags=["checagem"])

_PUBLIC_ID = re.compile(r"^[0-9a-hjkmnp-tv-z]{12}$")
_LATEST_PAGE = 20


def get_check_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CheckRepository:
    return SqlCheckRepository(session)


class _NoFactCheck:
    """Sem GOOGLE_FACTCHECK_API_KEY: segue sem checagens de agências."""

    async def search(self, queries: list[str]) -> list[AgencyCheck]:
        return []


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


def to_out(check: StoredCheck, settings: CheckSettings, *, cached: bool = False) -> CheckOut:
    result = check.result
    agencies_only = check.prompt_version == AGENCIES_ONLY_VERSION
    agencies = [item for item in result.get("checagens_agencias") or [] if isinstance(item, dict)]
    # Regras de hoje também na leitura: corrige checagens salvas antes delas (ex.: "verdadeiro"
    # sem agência), sem mexer no banco.
    veredito, downgraded = read_verdict(
        check.veredito, [str(item.get("veredito", "")) for item in agencies]
    )
    resumo = str(result.get("resumo", ""))
    if downgraded:
        # Só checagens de antes de 11/10 chegam aqui: elas não guardaram o domínio final
        # (redirect), então nada de "origem confirmada" sem esse dado.
        origin = result.get("origem_confirmada")
        resumo = neutral_summary(
            check.kind,
            origin if isinstance(origin, str) else check.domain,
            check.title,
            confirmed_origin=isinstance(origin, str),
            agency_checks=bool(agencies),
        )
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
    )


@router.post("/checks", response_model=CheckOut)
async def create_check(
    payload: CheckCreate,
    profile: Annotated[Profile, Depends(current_profile)],
    repo: Repo,
    tools: Annotated[CheckTools, Depends(get_check_tools)],
    settings: Annotated[CheckSettings, Depends(get_check_settings)],
) -> CheckOut:
    # Suspensão vale para comentários; banido não usa o site.
    if profile.effective_status(datetime.now(UTC)) == "banned":
        raise AppError(
            403, "conta_banida", "Esta conta foi banida por violar as regras do ChecaFatosRR."
        )
    outcome = await run_check(
        user_id=profile.id,
        url=payload.url,
        texto=payload.texto,
        repo=repo,
        tools=tools,
        settings=settings,
    )
    return to_out(outcome.check, settings, cached=outcome.cached)


@router.get("/checks/latest", response_model=LatestChecksOut)
async def latest_checks(
    repo: Repo,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = _LATEST_PAGE,
) -> LatestChecksOut:
    items = await repo.latest(_decode_cursor(cursor) if cursor else None, limit)
    next_cursor = (
        _encode_cursor(items[-1].created_at, items[-1].public_id) if len(items) == limit else None
    )
    return LatestChecksOut(
        items=[
            CheckSummaryOut(
                id=item.public_id,
                domain=item.domain,
                title=item.title,
                veredito=read_verdict(item.veredito, item.agency_ratings)[0],  # type: ignore[arg-type]
                created_at=item.created_at,
                total_agencias=item.agency_checks,
            )
            for item in items
        ],
        next_cursor=next_cursor,
    )


@router.get("/checks/{public_id}", response_model=CheckOut)
async def read_check(
    public_id: str,
    repo: Repo,
    settings: Annotated[CheckSettings, Depends(get_check_settings)],
) -> CheckOut:
    # Formato inválido (ex.: /checks/1) nem chega ao banco.
    if not _PUBLIC_ID.match(public_id):
        raise _not_found()
    check = await repo.get(public_id)
    if check is None:
        raise _not_found()
    return to_out(check, settings)


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
