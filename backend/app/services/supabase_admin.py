"""Chamadas à Admin API do Supabase Auth (usa a chave secreta — só no backend)."""

import logging
from typing import Annotated, Protocol
from uuid import UUID

import httpx
from fastapi import Depends

from app.config import Settings, get_settings
from app.errors import AppError

logger = logging.getLogger(__name__)


class SupabaseAdmin(Protocol):
    async def delete_user(self, user_id: UUID) -> None: ...


class HttpSupabaseAdmin:
    def __init__(self, supabase_url: str, secret_key: str) -> None:
        self._base_url = f"{supabase_url.rstrip('/')}/auth/v1/admin"
        self._secret_key = secret_key

    async def delete_user(self, user_id: UUID) -> None:
        """Apaga o usuário do Auth; a cascata no banco apaga perfil, comentários etc."""
        url = f"{self._base_url}/users/{user_id}"
        # Chaves legadas (service_role) são JWT e vão também no Authorization. As novas
        # (sb_secret_...) não são JWT: se o gateway recusar o Bearer, repete só com apikey.
        with_bearer = {"apikey": self._secret_key, "Authorization": f"Bearer {self._secret_key}"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.delete(url, headers=with_bearer)
                if response.status_code == 401:
                    response = await client.delete(url, headers={"apikey": self._secret_key})
        except httpx.HTTPError as exc:
            logger.error("supabase_admin_unreachable", extra={"error": type(exc).__name__})
            raise _deletion_failed() from exc
        # 404: já não existe — o objetivo (conta apagada) foi atingido.
        if response.status_code not in (200, 204, 404):
            logger.error("supabase_admin_delete_failed", extra={"status": response.status_code})
            raise _deletion_failed()


def _deletion_failed() -> AppError:
    return AppError(
        502,
        "exclusao_falhou",
        "Não foi possível excluir sua conta agora. Tente de novo em alguns minutos.",
    )


def get_supabase_admin(settings: Annotated[Settings, Depends(get_settings)]) -> SupabaseAdmin:
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise AppError(503, "exclusao_indisponivel", "Exclusão de conta indisponível no momento.")
    return HttpSupabaseAdmin(settings.supabase_url, settings.supabase_secret_key)
