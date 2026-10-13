"""Rotas de comentários (públicas para ler; com login para comentar, denunciar e apagar) e da
fila de moderação do admin (só ADMIN_EMAIL)."""

import ipaddress
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, current_user, require_admin
from app.config import Settings, get_settings
from app.db import get_session
from app.errors import AppError
from app.repositories.checks import SqlCheckRepository
from app.repositories.comments import SqlCommentRepository, StoredComment, Thread
from app.repositories.moderation import QueueItem, SqlModerationRepository
from app.repositories.profiles import SqlProfileRepository
from app.routers.checks import _decode_cursor, _encode_cursor
from app.schemas.comments import (
    AdminActionIn,
    AdminActionOut,
    CommentCreate,
    CommentListOut,
    CommentOut,
    PublicId,
    QueueItemOut,
    QueueListOut,
    ReportIn,
    ReportOut,
)
from app.services.bans import ban_hash
from app.services.comments import (
    CommentLimits,
    CommentRepos,
    Moderator,
    delete_comment,
    post_comment,
    report_comment,
)
from app.services.moderation.pipeline import AiUnavailable

router = APIRouter(tags=["comentários"])
PAGE_SIZE = 20


class _NoModeration:
    """Sem GEMINI_API_KEY: fail-open (publica e vai para a fila do admin)."""

    async def classify(self, body: str, reserve: Any, model_budget: Any = None) -> Any:
        return AiUnavailable("indisponivel")


@lru_cache(maxsize=2)
def _moderator(api_key: str, models: tuple[str, ...], limits: tuple[tuple[str, Any], ...]) -> Any:
    from app.services.moderation.ai_filter import google_moderator

    return google_moderator(api_key, models, limits=dict(limits))


def get_moderator(settings: Annotated[Settings, Depends(get_settings)]) -> Moderator:
    if not settings.gemini_api_key:
        return _NoModeration()
    return _moderator(  # type: ignore[no-any-return]
        settings.gemini_api_key,
        settings.gemini_moderation_models_list,
        tuple(sorted(settings.model_limits.items())),
    )


def get_comment_repos(session: Annotated[AsyncSession, Depends(get_session)]) -> CommentRepos:
    return CommentRepos(
        comments=SqlCommentRepository(session),
        moderation=SqlModerationRepository(session),
        profiles=SqlProfileRepository(session),
        quota=SqlCheckRepository(session),
    )


Repos = Annotated[CommentRepos, Depends(get_comment_repos)]
User = Annotated[CurrentUser, Depends(current_user)]
Admin = Annotated[CurrentUser, Depends(require_admin)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _limit(request: Request, name: str, key: str) -> None:
    retry_after = getattr(request.app.state.rate_limiters, name).hit(key)
    if retry_after is not None:
        raise AppError(
            429,
            "rate_limited",
            "Muitas tentativas. Aguarde um pouco e tente de novo.",
            {"retry_after": retry_after},
            {"Retry-After": str(retry_after)},
        )


def _client_key(request: Request, settings: Settings) -> str:
    """Chave do limite por cliente.

    CLIENT_IP_HEADER configurado (só depois de confirmar em /admin/diagnostico/ip que a
    plataforma sobrescreve o cabeçalho): usa o IP dele, se for um IP válido. Sem ele, a chave é
    a cadeia inteira do X-Forwarded-For (o proxy acrescenta o IP real ao fim): quem forja o IP
    de outra pessoa gera outra cadeia e não a bloqueia. Forjar valores novos ainda escapa do
    limite; ele só contém robôs simples.
    """
    if settings.client_ip_header:
        value = request.headers.get(settings.client_ip_header, "")[:64].strip()
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            pass  # ausente ou lixo: cai no comportamento padrão
        else:
            # IPv6: um /64 é de uma pessoa só (trocar o fim do endereço não escapa do limite).
            if ip.version == 6:
                return str(ipaddress.ip_network(f"{ip}/64", strict=False))
            return str(ip)
    forwarded = request.headers.get("x-forwarded-for", "")[:200]
    return forwarded or (request.client.host if request.client else "?")


# async: o limitador roda no event loop (uma thread), sem disputa no dict/deque.
async def _read_limit(request: Request, settings: AppSettings) -> None:
    _limit(request, "comments_read", _client_key(request, settings))


async def _report_limit(request: Request, user: User) -> None:
    _limit(request, "comment_report", str(user.id))


def _out(comment: StoredComment, replies: list[StoredComment] | None = None) -> CommentOut:
    return CommentOut(
        id=comment.public_id,
        apelido=comment.apelido,
        texto=comment.body,
        criado_em=comment.created_at,
        removido_pelo_autor=comment.status == "removed_by_author",
        respostas=[_out(r) for r in replies or []],
    )


def _thread_out(thread: Thread) -> CommentOut:
    return _out(thread.root, thread.replies)


@router.get("/comments", response_model=CommentListOut, dependencies=[Depends(_read_limit)])
async def list_comments(
    repos: Repos,
    response: Response,
    checagem: PublicId | None = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> CommentListOut:
    position = _decode_cursor(cursor) if cursor else None
    if checagem is not None:
        found = await repos.comments.threads_of_check(checagem, position, PAGE_SIZE + 1)
        if found is None:
            return CommentListOut(items=[], next_cursor=None)
        threads = found
    else:
        threads = await repos.comments.threads(None, position, PAGE_SIZE + 1)
    page, more = threads[:PAGE_SIZE], len(threads) > PAGE_SIZE
    last = page[-1].root if page else None
    response.headers["Cache-Control"] = "no-store"
    return CommentListOut(
        items=[_thread_out(t) for t in page],
        next_cursor=_encode_cursor(last.created_at, last.public_id) if more and last else None,
    )


@router.post("/comments", response_model=CommentOut, status_code=201)
async def create_comment(
    payload: CommentCreate,
    user: User,
    repos: Repos,
    settings: AppSettings,
    moderator: Annotated[Moderator, Depends(get_moderator)],
) -> CommentOut:
    created = await post_comment(
        user_id=user.id,
        body=payload.texto,
        checagem=payload.checagem,
        resposta_a=payload.resposta_a,
        repos=repos,
        moderator=moderator,
        limits=CommentLimits(
            min_interval_seconds=settings.comment_min_interval_seconds,
            daily=settings.comments_daily_limit,
            gemini_daily_moderation=settings.gemini_daily_moderation,
        ),
        ban_salt=settings.ban_hash_salt,
    )
    return _out(created)


@router.post(
    "/comments/{public_id}/denuncia",
    response_model=ReportOut,
    dependencies=[Depends(_report_limit)],
)
async def report(public_id: PublicId, payload: ReportIn, user: User, repos: Repos) -> ReportOut:
    status = await report_comment(repos, public_id, user.id, payload.motivo)
    return ReportOut(status=status)  # type: ignore[arg-type]


@router.delete("/comments/{public_id}", status_code=204)
async def delete(public_id: PublicId, user: User, repos: Repos) -> Response:
    await delete_comment(repos, public_id, user.id)
    return Response(status_code=204)


# ---------- diagnóstico do IP (admin) ----------


@router.get("/admin/diagnostico/ip", tags=["admin"])
async def ip_diagnostics(
    request: Request, _admin: Admin, settings: AppSettings, response: Response
) -> dict[str, str | None]:
    """Mostra os cabeçalhos de IP que chegam ao servidor, para decidir CLIENT_IP_HEADER: mande
    um CF-Connecting-IP inventado; se ele voltar igual, o cabeçalho não é confiável."""
    response.headers["Cache-Control"] = "no-store"
    headers = request.headers
    return {
        "cf_connecting_ip": headers.get("cf-connecting-ip"),
        "true_client_ip": headers.get("true-client-ip"),
        "x_forwarded_for": headers.get("x-forwarded-for"),
        "client_host": request.client.host if request.client else None,
        "client_ip_header": settings.client_ip_header,
        "chave_usada": _client_key(request, settings),
    }


# ---------- fila do admin ----------


def _item_out(item: QueueItem) -> QueueItemOut:
    return QueueItemOut(
        id=item.id,
        tipo=item.kind,
        motivo=item.reason,
        categoria=item.category,
        confianca=item.confidence,
        criado_em=item.created_at,
        apelido=item.apelido,
        comentario=item.comment_public_id,
        status_comentario=item.comment_status,
        texto=item.text,
    )


@router.get("/admin/moderacao/fila", response_model=QueueListOut, tags=["admin"])
async def moderation_queue(_admin: Admin, repos: Repos, response: Response) -> QueueListOut:
    response.headers["Cache-Control"] = "no-store"
    return QueueListOut(items=[_item_out(i) for i in await repos.moderation.queue()])


@router.post("/admin/moderacao/fila/{queue_id}", response_model=AdminActionOut, tags=["admin"])
async def moderation_action(
    queue_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
    payload: AdminActionIn,
    _admin: Admin,
    repos: Repos,
    settings: AppSettings,
) -> AdminActionOut:
    item = await repos.moderation.queue_item(queue_id)
    if item is None:
        raise AppError(404, "item_nao_encontrado", "Item da fila não encontrado ou já resolvido.")
    blocked = item.kind == "strike_confirmation"
    acao = payload.acao
    if acao == "aprovar":
        ok = (
            await repos.moderation.publish_from_log(queue_id)
            if blocked
            else await repos.moderation.approve_comment(queue_id)
        )
    elif acao == "remover":
        ok = (
            await repos.moderation.resolve(queue_id, "dismissed")
            if blocked
            else await repos.moderation.remove_comment(queue_id)
        )
    elif acao == "banir":
        ok = await _ban(item, repos, settings, queue_id)
    elif acao == "confirmar_strike" and blocked and item.user_id is not None:
        sub_hash = await _sub_hash(item, repos, settings)
        ok = await repos.moderation.confirm_strike(queue_id, sub_hash=sub_hash) is not None
    elif acao == "descartar" and blocked:
        # Só bloqueios: num comentário publicado, descartar o deixaria oculto sem revisão.
        ok = await repos.moderation.resolve(queue_id, "dismissed")
    else:
        raise AppError(422, "acao_invalida", "Essa ação não vale para este item.")
    if not ok:
        raise AppError(409, "acao_nao_concluida", "Não foi possível concluir. Recarregue a fila.")
    return AdminActionOut(status="ok")


async def _sub_hash(item: QueueItem, repos: CommentRepos, settings: Settings) -> bytes:
    sub = await repos.profiles.google_sub(item.user_id) if item.user_id else None
    if sub is None or not settings.ban_hash_salt:
        raise AppError(409, "sem_identidade", "Conta sem identidade Google para registrar.")
    return ban_hash(sub, salt=settings.ban_hash_salt)


async def _ban(item: QueueItem, repos: CommentRepos, settings: Settings, queue_id: int) -> bool:
    if item.user_id is None:
        raise AppError(409, "sem_usuario", "O autor não existe mais.")
    sub_hash = await _sub_hash(item, repos, settings)
    await repos.moderation.ban(item.user_id, sub_hash=sub_hash, reason=f"fila {queue_id}")
    if item.kind == "comment_review":
        return await repos.moderation.remove_comment(queue_id)
    return await repos.moderation.resolve(queue_id, "banned")
