"""Regras de criação de perfil (apelido + aceite dos termos) e de disponibilidade do apelido."""

import logging
from datetime import UTC, datetime
from typing import Literal

from app.auth import CurrentUser
from app.errors import AppError
from app.legal import CURRENT_TERMS_VERSION
from app.repositories.profiles import (
    AuthUserGoneError,
    NicknameTakenError,
    Profile,
    ProfileExistsError,
    ProfileRepository,
)
from app.services.bans import ban_hash
from app.services.nicknames import NicknameRejection, validate_nickname

logger = logging.getLogger(__name__)

AvailabilityReason = NicknameRejection | Literal["em_uso"]


def _nickname_taken() -> AppError:
    return AppError(409, "apelido_em_uso", "Esse apelido já está em uso. Escolha outro.")


def _profile_exists() -> AppError:
    return AppError(409, "perfil_existente", "Você já escolheu seu apelido.")


async def create_profile(
    *,
    user: CurrentUser,
    apelido: str,
    terms_version: str,
    repo: ProfileRepository,
    ban_salt: str | None,
) -> Profile:
    if await repo.get(user.id) is not None:
        raise _profile_exists()

    if terms_version != CURRENT_TERMS_VERSION:
        raise AppError(
            422,
            "termos_desatualizados",
            "Os Termos de Uso foram atualizados. Recarregue a página e aceite a versão atual.",
            {"terms_version": CURRENT_TERMS_VERSION},
        )

    verdict = validate_nickname(apelido)
    if verdict.reason == "formato":
        raise AppError(
            422,
            "apelido_invalido",
            "Use de 3 a 20 caracteres: letras sem acento, números ou _ (sublinhado).",
        )
    if verdict.reason == "reservado":
        raise AppError(422, "apelido_reservado", "Esse apelido é reservado. Escolha outro.")
    result = verdict.filter_result
    if result is not None and result.category and result.rule:
        await repo.log_nickname_blocked(apelido, result.category, result.rule)
        logger.info("nickname_blocked", extra={"category": result.category})
        raise AppError(
            422,
            "apelido_proibido",
            "Esse apelido não é permitido.",
            {"regra": result.rule, "explicacao": result.explanation},
        )

    suspended_until, sub_hash = await _ensure_not_banned(user, repo, ban_salt)

    try:
        # Conta excluída e recriada com o mesmo Google: nasce suspensa até a data guardada e
        # recebe de volta as advertências guardadas (30 dias).
        return await repo.create(
            user.id,
            apelido,
            terms_version,
            suspended_until=suspended_until,
            sub_hash=sub_hash,
        )
    except NicknameTakenError as exc:
        raise _nickname_taken() from exc
    except ProfileExistsError as exc:
        raise _profile_exists() from exc
    except AuthUserGoneError as exc:
        raise AppError(
            401,
            "unauthorized",
            "Sua sessão expirou. Entre novamente.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def _ensure_not_banned(
    user: CurrentUser, repo: ProfileRepository, ban_salt: str | None
) -> tuple[datetime | None, bytes]:
    """Recusa a identidade banida; devolve o fim de uma suspensão guardada (ou None) e o hash
    do sub (para devolver as advertências guardadas)."""
    if not ban_salt:
        logger.error("ban_hash_salt_missing")
        raise AppError(503, "cadastro_indisponivel", "Cadastro indisponível no momento.")
    google_sub = await repo.google_sub(user.id)
    if google_sub is None:
        raise AppError(
            403, "login_google_obrigatorio", "Entre com uma conta Google para participar."
        )
    sub_hash = ban_hash(google_sub, salt=ban_salt)
    if await repo.is_banned(sub_hash):
        logger.info("banned_identity_blocked")
        raise AppError(
            403,
            "conta_banida",
            "Esta conta Google foi banida por violar as regras do ChecaFatosRR.",
        )
    return await repo.suspension_until(sub_hash), sub_hash


async def keep_moderation_on_delete(
    user: CurrentUser, repo: ProfileRepository, ban_salt: str | None
) -> None:
    """Antes de excluir a conta: a suspensão e as advertências ficam guardadas pelo hash do
    sub do Google (plano §10; advertências por 30 dias, decisão de 13/10/2026). Banida já
    está em banned_identities."""
    profile = await repo.get(user.id)
    if profile is None:
        return
    status = profile.effective_status(datetime.now(UTC))
    if status == "banned":
        return
    suspended = profile.suspended_until if status == "suspended" else None
    strikes = await repo.active_strikes(user.id)
    if suspended is None and strikes == 0:
        return
    if not ban_salt:
        logger.error("ban_hash_salt_missing")
        raise AppError(503, "exclusao_indisponivel", "Exclusão de conta indisponível no momento.")
    google_sub = await repo.google_sub(user.id)
    if google_sub is None:
        # Conta sem identidade Google (não deveria existir): nada a guardar, mas fica registrado.
        logger.warning(
            "moderation_not_kept_without_google_sub",
            extra={"suspended": suspended is not None, "strikes": strikes},
        )
        return
    sub_hash = ban_hash(google_sub, salt=ban_salt)
    if suspended is not None:
        await repo.keep_suspension(sub_hash, suspended)
    if strikes:
        await repo.keep_strikes(sub_hash, strikes)


async def nickname_availability(
    apelido: str, repo: ProfileRepository
) -> tuple[bool, AvailabilityReason | None]:
    verdict = validate_nickname(apelido)
    if not verdict.ok:
        return False, verdict.reason
    if await repo.nickname_taken(apelido):
        return False, "em_uso"
    return True, None
