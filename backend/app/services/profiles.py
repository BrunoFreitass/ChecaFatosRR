"""Regras de criação de perfil (apelido + aceite dos termos) e de disponibilidade do apelido."""

import logging
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

    await _ensure_not_banned(user, repo, ban_salt)

    try:
        return await repo.create(user.id, apelido, terms_version)
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
) -> None:
    if not ban_salt:
        logger.error("ban_hash_salt_missing")
        raise AppError(503, "cadastro_indisponivel", "Cadastro indisponível no momento.")
    google_sub = await repo.google_sub(user.id)
    if google_sub is None:
        raise AppError(
            403, "login_google_obrigatorio", "Entre com uma conta Google para participar."
        )
    if await repo.is_banned(ban_hash(google_sub, salt=ban_salt)):
        logger.info("banned_identity_blocked")
        raise AppError(
            403,
            "conta_banida",
            "Esta conta Google foi banida por violar as regras do ChecaFatosRR.",
        )


async def nickname_availability(
    apelido: str, repo: ProfileRepository
) -> tuple[bool, AvailabilityReason | None]:
    verdict = validate_nickname(apelido)
    if not verdict.ok:
        return False, verdict.reason
    if await repo.nickname_taken(apelido):
        return False, "em_uso"
    return True, None
