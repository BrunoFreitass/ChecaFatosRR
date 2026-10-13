"""Orquestração de uma checagem: entrada → cache/limites → single-flight → fontes → Gemini.

Nenhuma transação fica aberta durante chamadas externas (download, Fact Check, Gemini), e
nenhum timeout do asyncio envolve chamadas ao banco (cancelar no meio de uma transação
deixaria a sessão inutilizável para o refund/release logo depois).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from uuid import UUID

from app.config import DEFAULT_OPEN_PLATFORMS
from app.errors import AppError
from app.repositories.checks import (
    AGENCIES_ONLY_VERSION,
    CacheHit,
    CheckRepository,
    LimitExceededError,
    Limits,
    LinkSubject,
    NewCheck,
    Started,
    StoredCheck,
    SubjectRef,
    TextSubject,
)
from app.services.extractor import extract_page
from app.services.factcheck import AgencyCheck, FactCheckClient, build_queries
from app.services.fetcher import PageFetcher, PageUnreadableError
from app.services.gemini import (
    AnalysisInput,
    GeminiContentBlockedError,
    GeminiQuotaExhaustedError,
    GeminiUnavailableError,
    VerdictAnalyzer,
)
from app.services.moderation.local_filter import check_text
from app.services.text_input import TextTooLongError, TextTooShortError, prepare_text
from app.services.url_guard import (
    BlockedAddressError,
    InvalidUrlError,
    NormalizedUrl,
    is_social_media,
    normalize_url,
)
from app.services.verdict_policy import agencies_only_result, is_open_platform

logger = logging.getLogger(__name__)

# O Render responde atrás do Cloudflare, que corta a requisição em ~100 s (524): nenhuma
# requisição de checagem passa disso, com folga.
REQUEST_LIMIT_SECONDS = 90
# Folga para as chamadas ao banco, que ficam fora dos tetos das etapas (begin_check, claim,
# reserva da cota, gravação; Supabase Free pode demorar).
DB_MARGIN_SECONDS = 8
# Tetos das etapas externas. O Gemini tem o próprio orçamento (GEMINI_BUDGET_SECONDS, 48 s:
# tentativa de 18 s no modelo lento e o resto nos próximos). Pior caso: 20 + 12 + 48 = 80 s.
READ_LINK_TIMEOUT_SECONDS = 20  # download (10 s) + extração (fila do semáforo)
AGENCIES_TIMEOUT_SECONDS = 12  # até 2 buscas de 5 s
WORST_CHECK_SECONDS = 80
# Quem perdeu o claim espera a checagem de quem ganhou (medido pelo relógio, desde o início
# da requisição)...
WAIT_POLL_SECONDS = 1.5
WAIT_MAX_SECONDS = REQUEST_LIMIT_SECONDS - DB_MARGIN_SECONDS
# ...mas só assume o claim (se o outro falhou) enquanto ainda couber a checagem inteira:
# 2 + 80 + 8 = 90 s, ou seja, só em falha imediata do outro. Depois disso, 409 na hora: o
# frontend repete a requisição sozinho (RETRY_BUSY_MS) e a nova pega o claim livre.
TAKEOVER_LIMIT_SECONDS = REQUEST_LIMIT_SECONDS - DB_MARGIN_SECONDS - WORST_CHECK_SECONDS


# db = begin_check (com o claim), save_result (a reserva da cota fica em gemini).
TIMED_STAGES = ("db", "wait", "fetch", "extract", "factcheck", "gemini")


class StageTimer:
    """Tempo de cada etapa da checagem, para o log check_timing (onde vão os segundos)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.started = clock()  # início da requisição (a espera também mede a partir dele)
        self.ms: dict[str, int] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = self._clock()
        try:
            yield
        finally:
            elapsed = int((self._clock() - started) * 1000)
            self.ms[name] = self.ms.get(name, 0) + elapsed

    def log(self, outcome: str, model: str | None = None) -> None:
        stages = {f"{name}_ms": self.ms.get(name, 0) for name in TIMED_STAGES}
        total = int((self._clock() - self.started) * 1000)
        logger.info(
            "check_timing",
            extra={**stages, "total_ms": total, "outcome": outcome, "model": model},
        )


@dataclass(frozen=True)
class CheckSettings:
    limits: Limits
    daily_gemini_checks: int
    model: str  # primeiro da cadeia; vale quando o analisador não informa
    open_platforms: tuple[str, ...] = DEFAULT_OPEN_PLATFORMS


@dataclass(frozen=True)
class CheckTools:
    fetcher: PageFetcher
    factcheck: FactCheckClient
    analyzer: VerdictAnalyzer
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class CheckOutcome:
    check: StoredCheck
    cached: bool
    agencies_only: bool = False  # Gemini indisponível: só o que as agências publicaram


@dataclass(frozen=True)
class LinkInput:
    url: NormalizedUrl


@dataclass(frozen=True)
class TextInput:
    body: str
    urls: list[str]


@dataclass(frozen=True)
class _LinkRead:
    data: AnalysisInput  # data.published_at é a data da página (uma fonte só)
    final_domain: str


def _unreadable(reason: str) -> AppError:
    return AppError(
        422,
        "pagina_ilegivel",
        "Não conseguimos ler esse link (redes sociais e alguns sites bloqueiam a leitura). "
        "Cole o texto da mensagem para checar.",
        {"sugestao": "colar_texto", "motivo": reason},
    )


def _unavailable() -> AppError:
    return AppError(
        503,
        "checagem_indisponivel",
        "A checagem automática está indisponível agora. Tente de novo em alguns minutos.",
    )


def parse_input(
    url: str | None, texto: str | None
) -> tuple[LinkInput | TextInput, LinkSubject | TextSubject]:
    if url is not None:
        try:
            normalized = normalize_url(url)
        except BlockedAddressError as exc:
            raise AppError(
                422, "url_bloqueada", "Esse endereço não pode ser checado (rede interna ou local)."
            ) from exc
        except InvalidUrlError as exc:
            raise AppError(
                422, "url_invalida", "Esse link não parece válido. Confira e cole de novo."
            ) from exc
        if is_social_media(normalized):
            # Sem download: redes sociais não entregam o conteúdo sem login.
            raise _unreadable("rede_social")
        subject = LinkSubject(
            url=normalized.url, url_hash=normalized.url_hash, domain=normalized.domain
        )
        return LinkInput(url=normalized), subject

    try:
        prepared = prepare_text(texto or "")
    except TextTooShortError as exc:
        raise AppError(
            422, "texto_curto", "O texto é curto demais para checar. Cole a mensagem completa."
        ) from exc
    except TextTooLongError as exc:
        raise AppError(
            422, "texto_longo", "O texto passa de 5.000 caracteres. Cole só a parte principal."
        ) from exc
    subject = TextSubject(body=prepared.body, text_hash=prepared.text_hash)
    return TextInput(body=prepared.body, urls=prepared.urls), subject


async def run_check(
    *,
    user_id: UUID,
    url: str | None,
    texto: str | None,
    repo: CheckRepository,
    tools: CheckTools,
    settings: CheckSettings,
) -> CheckOutcome:
    timer = StageTimer(tools.clock)
    outcome = "erro"
    model: str | None = None
    try:
        result = await _run_check(user_id, url, texto, repo, tools, settings, timer)
        outcome = "agencias" if result.agencies_only else "cache" if result.cached else "nova"
        if outcome == "nova":
            model = result.check.result.get("model")  # qual modelo do pool respondeu
        return result
    except AppError as exc:
        outcome = f"erro:{exc.code}"
        raise
    finally:
        timer.log(outcome, model)


async def _run_check(
    user_id: UUID,
    url: str | None,
    texto: str | None,
    repo: CheckRepository,
    tools: CheckTools,
    settings: CheckSettings,
    timer: StageTimer,
) -> CheckOutcome:
    parsed, subject = parse_input(url, texto)

    try:
        with timer.stage("db"):
            begun = await repo.begin_check(user_id, subject, settings.limits)
    except LimitExceededError as exc:
        raise _limit_error(exc) from exc
    if isinstance(begun, CacheHit):
        return CheckOutcome(check=begun.check, cached=True)
    started = begun

    # O begin_check já tomou o claim (e conferiu o cache depois dele, se preciso).
    token = started.claim_token
    took_over = token is None
    if token is None:
        with timer.stage("wait"):
            waited = await _wait_for_other(started, repo, tools, settings, timer.started)
        if isinstance(waited, CheckOutcome):
            return waited
        token = waited
        # Quem estava checando desistiu (falhou) e o claim passou para nós.

    # Daqui em diante o claim é nosso: o finally sempre o solta se não houver resultado salvo.
    saved = False
    try:
        if took_over:
            # A outra checagem pode ter terminado entre a última consulta da espera e o claim.
            with timer.stage("db"):
                fresh = await repo.find_fresh(
                    started.subject, settings.limits.cache_hours, settings.limits.prompt_version
                )
            if fresh is not None:
                await repo.mark_cache_hit(started.request_id)
                return CheckOutcome(check=fresh, cached=True)
        result = await _analyze(parsed, started, user_id, repo, tools, settings, token, timer)
        saved = not result.cached  # análise anterior servida: nada salvo, solta o claim
        return result
    finally:
        if not saved:
            await _release_safely(repo, started.subject, token)


async def _wait_for_other(
    started: Started,
    repo: CheckRepository,
    tools: CheckTools,
    settings: CheckSettings,
    request_started: float,
) -> CheckOutcome | datetime:
    """Espera quem está checando o mesmo conteúdo. Devolve o resultado dele, ou o token do
    claim se ele ficou livre (o outro falhou) e ainda cabe uma checagem inteira."""
    polled = 0.0
    while True:
        await tools.sleep(WAIT_POLL_SECONDS)
        polled += WAIT_POLL_SECONDS
        fresh = await repo.find_fresh(
            started.subject, settings.limits.cache_hours, settings.limits.prompt_version
        )
        if fresh is not None:
            # O resultado de outra pessoa: conta como consulta ao cache, não como checagem.
            await repo.mark_cache_hit(started.request_id)
            return CheckOutcome(check=fresh, cached=True)
        token = await repo.claim(started.subject)
        # Relógio real (as consultas ao banco também gastam tempo); as pausas contadas
        # garantem o fim mesmo com o relógio parado.
        elapsed = max(polled, tools.clock() - request_started)
        if token is not None:
            if elapsed <= TAKEOVER_LIMIT_SECONDS:
                return token
            # O outro falhou, mas não cabe mais uma checagem inteira: solta e para de esperar.
            await _release_safely(repo, started.subject, token)
            break
        if elapsed >= WAIT_MAX_SECONDS:
            break
    await repo.refund(started.request_id)
    raise AppError(
        409,
        "checagem_em_andamento",
        "Esse conteúdo está sendo checado agora. Tente de novo em alguns segundos.",
        headers={"Retry-After": "5"},
    )


async def _release_safely(repo: CheckRepository, subject: SubjectRef, token: datetime) -> None:
    # shield: termina mesmo se a requisição for cancelada (cliente fechou a aba). Se falhar,
    # o claim expira sozinho (CLAIM_TTL_SECONDS) — não pode esconder o erro original.
    try:
        await asyncio.shield(repo.release(subject, token))
    except Exception:
        logger.warning("claim_release_failed", exc_info=True)


async def _analyze(
    parsed: LinkInput | TextInput,
    started: Started,
    user_id: UUID,
    repo: CheckRepository,
    tools: CheckTools,
    settings: CheckSettings,
    claim_token: datetime,
    timer: StageTimer,
) -> CheckOutcome:
    published_at: date | None = None
    listed = False
    if isinstance(parsed, LinkInput):
        read = await _read_link(parsed, tools, timer, settings.open_platforms)
        data, published_at = read.data, read.data.published_at
        # Redirect para outro domínio (ex.: redirect aberto num site conhecido): o conteúdo
        # não é daquele domínio, então não vai para "Últimas" com o nome dele.
        same_site = read.final_domain == parsed.url.domain
        listed = same_site and _listable(data.title, parsed.url.domain)
    else:
        data = AnalysisInput(
            kind="texto",
            content=parsed.body,
            title=None,
            url=None,
            agency_checks=[],
            content_urls=parsed.urls,
        )

    with timer.stage("factcheck"):
        agency_checks, agencies_failed = await _search_agencies(tools, data)
        data = replace(data, agency_checks=agency_checks)

    async def release() -> None:
        # Nenhuma requisição chegou ao Google (todos no limite por minuto): a unidade volta.
        await repo.release_gemini("checagem")

    async def reserve() -> bool:
        # A cota só é gasta depois que o conteúdo foi lido (link ilegível não gasta), e a
        # cada tentativa real de chamada ao modelo.
        return await repo.reserve_gemini("checagem", settings.daily_gemini_checks)

    try:
        with timer.stage("gemini"):
            result = await tools.analyzer.analyze(
                data, reserve, release, model_budget=repo.reserve_gemini_model
            )
    except GeminiContentBlockedError as exc:
        # Recusa de segurança do modelo: tentar de novo dá no mesmo, e uma página pública
        # "tente mais tarde" seria enganosa. Devolve o limite do usuário (como antes).
        await repo.refund(started.request_id)
        raise _unavailable() from exc
    except (GeminiQuotaExhaustedError, GeminiUnavailableError) as exc:
        # Sem cota (nossa ou do Google), no limite por minuto ou com todos os modelos fora:
        # 1º a análise de IA de uma versão anterior do prompt (a tela avisa "análise
        # anterior"; continua contando como checagem nova, senão escaparia do limite de
        # consultas); 2º o modo "só agências", na hora, sem esperar.
        reason = type(exc).__name__
        try:
            previous = await repo.find_previous(started.subject, settings.limits.cache_hours)
            if previous is not None:
                logger.info("gemini_unavailable_served_previous_analysis", extra={"reason": reason})
                return CheckOutcome(check=previous, cached=True)
        except Exception:  # o fallback é um extra: se falhar, segue para o modo agências
            logger.exception("previous_analysis_lookup_failed")
        return await _agencies_only(
            _AgenciesOnly(data, started, user_id, claim_token, published_at, agencies_failed),
            repo,
            settings,
            timer,
            reason,
        )

    with timer.stage("db"):
        saved = await repo.save_result(
            NewCheck(
                subject=started.subject,
                user_id=user_id,
                veredito=result.veredito,
                confianca=result.confianca,
                # O título vai junto no result: é o desta checagem, mesmo se ela não for listada.
                # O modelo também: o eval e o log mostram a qualidade por modelo do pool.
                result={**result.to_json(), "title": data.title, "model": result.model},
                model=result.model or settings.model,
                listed=listed,
                title=data.title,
                published_at=published_at,
                claim_token=claim_token,
                prompt_version=settings.limits.prompt_version,
            )
        )
    return CheckOutcome(check=saved, cached=False)


@dataclass(frozen=True)
class _AgenciesOnly:
    data: AnalysisInput
    started: Started
    user_id: UUID
    claim_token: datetime
    published_at: date | None
    search_failed: bool


async def _agencies_only(
    job: _AgenciesOnly,
    repo: CheckRepository,
    settings: CheckSettings,
    timer: StageTimer,
    reason: str,
) -> CheckOutcome:
    """Resultado sem IA (só o que a Fact Check API trouxe), salvo com a versão reservada:
    tem link público, nunca vira cache e não é "Últimas". O pedido vira consulta: não conta
    no limite de checagens com IA, mas conta no de consultas (Gemini fora não libera geral)."""
    data, started = job.data, job.started
    try:
        with timer.stage("db"):
            retry = await repo.convert_to_lookup(
                started.request_id, job.user_id, settings.limits.lookups_per_hour
            )
            if retry is not None:
                raise _limit_error(LimitExceededError("consultas", retry))
            saved = await repo.save_result(
                NewCheck(
                    subject=started.subject,
                    user_id=job.user_id,
                    veredito="nao_verificavel",
                    confianca=0,
                    result=agencies_only_result(
                        data.agency_checks, data.title, search_failed=job.search_failed
                    ),
                    model=AGENCIES_ONLY_VERSION,
                    listed=False,
                    title=data.title,
                    published_at=job.published_at,
                    claim_token=job.claim_token,
                    prompt_version=AGENCIES_ONLY_VERSION,
                )
            )
    except BaseException:
        # Nada salvo: o pedido não conta em limite nenhum (o finally de _run_check solta o
        # claim). shield: termina mesmo se a requisição foi cancelada.
        await asyncio.shield(repo.refund(started.request_id))
        raise
    logger.info(
        "agencies_only_mode",
        extra={
            "reason": reason,
            "agency_checks": len(data.agency_checks),
            "search_failed": job.search_failed,
        },
    )
    return CheckOutcome(check=saved, cached=False, agencies_only=True)


async def _read_link(
    parsed: LinkInput, tools: CheckTools, timer: StageTimer, open_platforms: tuple[str, ...]
) -> _LinkRead:
    try:
        async with asyncio.timeout(READ_LINK_TIMEOUT_SECONDS):
            with timer.stage("fetch"):
                page = await tools.fetcher.fetch(parsed.url)
            with timer.stage("extract"):  # inclui a fila do semáforo (0,1 CPU no Render)
                extracted = await extract_page(page)
        # Endereço final (depois de redirects) que o guard recusa: ilegível, nunca 500.
        final_domain = normalize_url(page.url).domain
    except PageUnreadableError as exc:
        # Conta no limite: protege contra quem manda links quebrados em massa.
        logger.info("page_unreadable", extra={"reason": exc.reason})
        raise _unreadable(exc.reason) from exc
    except TimeoutError as exc:
        raise _unreadable("timeout") from exc
    except BlockedAddressError as exc:
        # DNS ou redirect para a rede interna. Mesma resposta de "não deu para ler": não
        # revelar quais nomes apontam para dentro.
        logger.warning("fetch_blocked_address")
        raise _unreadable("rede") from exc
    except InvalidUrlError as exc:
        raise _unreadable("redirect_invalido") from exc
    data = AnalysisInput(
        kind="link",
        content=extracted.text,
        title=extracted.title,
        url=page.url,
        agency_checks=[],
        content_urls=extracted.urls,
        # Vai ao prompt: o modelo precisa saber que a notícia é de hoje, não "do futuro".
        published_at=extracted.published_at,
        # Origem confirmada: o domínio de onde a página veio de fato (depois de redirects).
        source_domain=final_domain,
        open_platform=is_open_platform(final_domain, open_platforms),
    )
    return _LinkRead(data=data, final_domain=final_domain)


async def _search_agencies(
    tools: CheckTools, data: AnalysisInput
) -> tuple[list[AgencyCheck], bool]:
    """As checagens encontradas e se a busca falhou (no modo "só agências", "não
    encontramos" e "não conseguimos consultar" são mensagens diferentes)."""
    try:
        async with asyncio.timeout(AGENCIES_TIMEOUT_SECONDS):
            return await tools.factcheck.search(build_queries(data.title, data.content)), False
    except Exception as exc:  # a Fact Check API nunca derruba a checagem
        logger.warning("factcheck_failed", extra={"error": type(exc).__name__})
        return [], True


def _listable(title: str | None, domain: str) -> bool:
    """Título ou domínio ofensivo não aparece em "Últimas" (continua acessível pelo link)."""
    return not any(check_text(value).blocked for value in (title or "", domain) if value)


def _limit_error(exc: LimitExceededError) -> AppError:
    headers = {"Retry-After": str(exc.retry_after_seconds)}
    minutes = max(1, round(exc.retry_after_seconds / 60))
    extra: dict[str, object] = {"retry_after": exc.retry_after_seconds}
    if exc.kind == "diario":
        hours = max(1, round(exc.retry_after_seconds / 3600))
        return AppError(
            429,
            "limite_diario",
            "Você chegou ao limite de checagens novas por dia. Resultados que já estão no site "
            f"continuam abrindo normalmente. Novas checagens em cerca de {hours} h.",
            extra,
            headers,
        )
    if exc.kind == "consultas":
        return AppError(
            429,
            "limite_consultas",
            f"Você abriu muitas checagens na última hora. Tente de novo em {minutes} min.",
            extra,
            headers,
        )
    return AppError(
        429,
        "limite_checagens",
        f"Você chegou ao limite de checagens por hora. Tente de novo em {minutes} min.",
        extra,
        headers,
    )
