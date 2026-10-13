"""Publicar, denunciar e apagar comentários (etapas 3+4).

Ordem do POST: alvo (feed, checagem de LINK ou resposta de 1 nível) → limites (status,
intervalo, teto diário, cooldown de ofensivo/spam, teto de moderação pela IA) → filtro local
→ IA (Flash-Lite; fail-open) → decisão (pipeline.decide) → publica ou registra o bloqueio.
A IA é chamada fora de qualquer transação.
"""

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from app.errors import AppError
from app.repositories.comments import (
    AccountRestrictedError,
    SqlCommentRepository,
    StoredComment,
)
from app.repositories.moderation import Block, SqlModerationRepository, StrikeOutcome
from app.repositories.profiles import ProfileRepository
from app.services.bans import ban_hash
from app.services.moderation.local_filter import check_text
from app.services.moderation.pipeline import Decision, decide

logger = logging.getLogger(__name__)

# 5 bloqueios de "ofensivo"/"spam" em 24 h → 1 h sem comentar (decisão de 12/10/2026).
OFFENSIVE_BLOCKS_TO_PAUSE = 5
# Um comentário em andamento por usuário (neste processo; o Render roda uma instância): uma
# rajada em paralelo passaria pela checagem sem lock e gastaria a cota de moderação.
_IN_FLIGHT: set[UUID] = set()


class Moderator(Protocol):
    async def classify(
        self, body: str, reserve: Any, model_budget: Any = None, model_release: Any = None
    ) -> Any: ...


@dataclass(frozen=True)
class CommentLimits:
    min_interval_seconds: int
    daily: int
    gemini_daily_moderation: int


@dataclass(frozen=True)
class CommentRepos:
    comments: SqlCommentRepository
    moderation: SqlModerationRepository
    profiles: ProfileRepository
    quota: Any  # SqlCheckRepository: reserve_gemini("moderacao") e reserve_gemini_model


def _too_many(code: str, message: str, retry: int | None) -> AppError:
    seconds = max(1, int(retry or 1))
    return AppError(429, code, message, {"retry_after": seconds}, {"Retry-After": str(seconds)})


async def _target(
    repos: CommentRepos, checagem: str | None, resposta_a: str | None
) -> tuple[int | None, int | None]:
    link_id: int | None = None
    if checagem is not None:
        found, link_id = await repos.comments.link_of_check(checagem)
        if not found:
            raise AppError(404, "checagem_nao_encontrada", "Checagem não encontrada.")
        if link_id is None:  # texto colado (decisão de 09/10/2026)
            raise AppError(
                422,
                "comentario_nao_permitido",
                "Comentários só em checagens de links e na Comunidade.",
            )
    if resposta_a is None:
        return link_id, None
    parent = await repos.comments.parent(resposta_a)
    if parent is None:
        raise AppError(404, "comentario_nao_encontrado", "Comentário não encontrado.")
    if parent.is_reply or not parent.visible or (checagem and parent.link_id != link_id):
        raise AppError(422, "resposta_invalida", "Só dá para responder a um comentário principal.")
    return parent.link_id, parent.id


async def _check_limits(repos: CommentRepos, user_id: UUID, limits: CommentLimits) -> None:
    state = await repos.comments.precheck(user_id)
    if state is None:
        raise AppError(404, "profile_not_found", "Escolha um apelido para continuar.")
    if state.status == "banned":
        raise AppError(403, "conta_banida", "Esta conta foi banida por violar as regras.")
    now = datetime.now(UTC)
    if state.status == "suspended" and state.suspended_until and state.suspended_until > now:
        raise AppError(
            403,
            "conta_suspensa",
            "Sua conta está suspensa e não pode comentar por enquanto.",
            {"suspenso_ate": state.suspended_until.isoformat()},
        )
    since = state.seconds_since_last
    if since is not None and since < limits.min_interval_seconds:
        raise _too_many(
            "limite_comentarios",
            "Espere alguns segundos antes de comentar de novo.",
            math.ceil(limits.min_interval_seconds - since),
        )
    # Um limite só (13/10/2026): publicados + tentativas bloqueadas pela IA (gastam a cota).
    if state.day_count + state.ai_blocks >= limits.daily:
        raise _too_many(
            "limite_diario_comentarios",
            "Você chegou ao limite de comentários por hoje.",
            state.day_retry or 3600,
        )
    if state.offensive_blocks >= OFFENSIVE_BLOCKS_TO_PAUSE and (state.offensive_retry or 0) > 0:
        raise _too_many(
            "comentarios_pausados",
            "Vários comentários seus foram bloqueados. Você poderá comentar de novo em 1 hora.",
            state.offensive_retry,
        )


async def _sub_hash(repos: CommentRepos, user_id: UUID, ban_salt: str | None) -> bytes:
    sub = await repos.profiles.google_sub(user_id)
    if not ban_salt or sub is None:
        logger.error("comment_strike_without_identity")
        raise AppError(503, "comentarios_indisponiveis", "Comentários indisponíveis no momento.")
    return ban_hash(sub, salt=ban_salt)


def _violation(decision: Decision, outcome: StrikeOutcome) -> AppError:
    suspended = outcome.status == "suspended" and outcome.suspended_until
    return AppError(
        422,
        "violacao",
        f"Seu comentário não foi publicado: {decision.rule}.",
        {
            "regra": decision.rule,
            "explicacao": decision.explanation,
            "strikes": outcome.active_strikes,
            "status": outcome.status,
            "suspenso_ate": outcome.suspended_until.isoformat() if suspended else None,
        },
    )


async def post_comment(
    *,
    user_id: UUID,
    body: str,
    checagem: str | None,
    resposta_a: str | None,
    repos: CommentRepos,
    moderator: Moderator,
    limits: CommentLimits,
    ban_salt: str | None,
) -> StoredComment:
    if user_id in _IN_FLIGHT:
        raise _too_many(
            "comentario_em_andamento", "Seu comentário anterior ainda está sendo analisado.", 5
        )
    _IN_FLIGHT.add(user_id)
    try:
        return await _post(user_id, body, checagem, resposta_a, repos, moderator, limits, ban_salt)
    finally:
        _IN_FLIGHT.discard(user_id)


async def _post(
    user_id: UUID,
    body: str,
    checagem: str | None,
    resposta_a: str | None,
    repos: CommentRepos,
    moderator: Moderator,
    limits: CommentLimits,
    ban_salt: str | None,
) -> StoredComment:
    link_id, parent_id = await _target(repos, checagem, resposta_a)
    await _check_limits(repos, user_id, limits)
    target = {"link_id": link_id, "parent_id": parent_id}

    local = check_text(body)
    if local.blocked:
        decision = decide(local, None)
    else:

        async def reserve() -> bool:
            return await repos.quota.reserve_gemini("moderacao", limits.gemini_daily_moderation)

        ai = await moderator.classify(
            body,
            reserve,
            model_budget=repos.quota.reserve_gemini_model,
            model_release=repos.quota.release_gemini_model,
        )
        decision = decide(local, ai)

    if not decision.publish:
        if decision.actor is None or decision.category is None:  # decide() sempre preenche
            raise RuntimeError("bloqueio sem autor ou categoria")
        sub_hash = await _sub_hash(repos, user_id, ban_salt) if decision.strike else None
        outcome = await repos.moderation.record_block(
            user_id,
            Block(
                actor=decision.actor,
                category=decision.category,
                snapshot=body,
                strike=decision.strike,
                confidence=decision.confidence,
                ask_admin=decision.queue == "ai_low_confidence",
                target=target,
            ),
            sub_hash=sub_hash,
        )
        logger.info(
            "comment_blocked",
            extra={
                "category": decision.category,
                "actor": decision.actor,
                "strike": decision.strike,
            },
        )
        raise _violation(decision, outcome)

    queue = (decision.queue, decision.category, decision.confidence) if decision.queue else None
    try:
        created = await repos.comments.create(
            user_id,
            link_id=link_id,
            parent_id=parent_id,
            body=body,
            min_interval_seconds=limits.min_interval_seconds,
            queue=queue,
        )
    except AccountRestrictedError:
        raise AppError(403, "conta_restrita", "Sua conta não pode comentar no momento.") from None
    if created is None:
        raise _too_many(
            "limite_comentarios",
            "Espere alguns segundos antes de comentar de novo.",
            limits.min_interval_seconds,
        )
    return created


REPORT_THRESHOLD = 3
# Quem pode denunciar (13/10/2026): conta com mais de 24 h E (1 comentário publicado OU 3 dias
# de conta); no máximo 10 denúncias em 24 h. Contém brigadas de contas novas.
REPORT_MIN_ACCOUNT_HOURS = 24
REPORT_MIN_HISTORY_DAYS = 3
REPORT_DAILY_LIMIT = 10


async def report_comment(
    repos: CommentRepos, public_id: str, reporter_id: UUID, reason: str | None
) -> str:
    outcome, retry = await repos.comments.report(
        public_id,
        reporter_id,
        reason=reason,
        threshold=REPORT_THRESHOLD,
        min_account_hours=REPORT_MIN_ACCOUNT_HOURS,
        history_days=REPORT_MIN_HISTORY_DAYS,
        daily_limit=REPORT_DAILY_LIMIT,
    )
    if outcome == "ok":
        return "recebida"
    if outcome == "duplicate":
        return "ja_recebida"
    if outcome == "not_found":
        raise AppError(404, "comentario_nao_encontrado", "Comentário não encontrado.")
    if outcome == "own":
        raise AppError(422, "denuncia_propria", "Você não pode denunciar o próprio comentário.")
    if outcome == "no_profile":
        raise AppError(404, "profile_not_found", "Escolha um apelido para continuar.")
    if outcome == "daily_limit":
        raise _too_many(
            "limite_denuncias_diario",
            f"Você chegou ao limite de {REPORT_DAILY_LIMIT} denúncias por dia.",
            retry or 3600,
        )
    if outcome in ("needs_history", "too_new"):
        raise AppError(
            403,
            "conta_inelegivel_denuncia",
            "Sua conta não atende aos requisitos para realizar denúncias.",
        )
    raise AppError(403, "conta_restrita", "Sua conta não pode denunciar no momento.")


async def delete_comment(repos: CommentRepos, public_id: str, user_id: UUID) -> None:
    if not await repos.comments.delete_own(public_id, user_id):
        raise AppError(404, "comentario_nao_encontrado", "Comentário não encontrado.")
