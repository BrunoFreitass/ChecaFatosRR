"""Autenticação: verificação do JWT emitido pelo Supabase Auth e dependências do FastAPI."""

import logging
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Protocol
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.errors import AppError

logger = logging.getLogger(__name__)

_ASYMMETRIC_ALGORITHMS = frozenset({"ES256", "RS256"})
_LEEWAY_SECONDS = 30
_AUDIENCE = "authenticated"


class InvalidTokenError(Exception):
    """Token ausente, malformado, expirado ou com assinatura/claims inválidos."""


class AuthUnavailableError(Exception):
    """Não foi possível buscar as chaves públicas (JWKS) do Supabase."""


class SigningKeySource(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class JwksClient(Protocol):
    def get_signing_keys(self, refresh: bool = False) -> list[Any]: ...


class ThrottledJwks:
    """Envolve o PyJWKClient para evitar DoS por `kid` inventado.

    Sozinho, o PyJWKClient busca o JWKS de novo a cada `kid` desconhecido — um atacante sem
    login poderia forçar uma requisição ao Supabase por token falso. Aqui, a busca forçada
    acontece no máximo uma vez por intervalo; fora disso, `kid` desconhecido é 401 sem I/O.
    """

    def __init__(self, client: JwksClient, *, min_refresh_seconds: float = 60) -> None:
        self._client = client
        self._min_refresh_seconds = min_refresh_seconds
        self._last_forced_refresh = float("-inf")
        self._lock = threading.Lock()

    @staticmethod
    def _find(keys: list[Any], kid: str) -> Any | None:
        return next((key for key in keys if getattr(key, "key_id", None) == kid), None)

    def get_signing_key_from_jwt(self, token: str) -> Any:
        kid = jwt.get_unverified_header(token).get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidTokenError("kid ausente")
        # Conjunto em cache (renovado pelo próprio PyJWKClient a cada `lifespan`).
        key = self._find(self._client.get_signing_keys(), kid)
        if key is not None:
            return key
        with self._lock:
            now = time.monotonic()
            if now - self._last_forced_refresh < self._min_refresh_seconds:
                raise InvalidTokenError("kid desconhecido")
            self._last_forced_refresh = now
        # Pode ser rotação de chave no Supabase: busca uma vez.
        key = self._find(self._client.get_signing_keys(refresh=True), kid)
        if key is None:
            raise InvalidTokenError("kid desconhecido")
        return key


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    email: str | None
    # app_metadata.provider — definido pelo Supabase; o usuário não consegue alterar.
    provider: str | None


class TokenVerifier:
    def __init__(
        self,
        *,
        supabase_url: str,
        jwt_secret: str | None = None,
        jwks: SigningKeySource | None = None,
    ) -> None:
        self._issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        self._jwt_secret = jwt_secret
        self._jwks = jwks or ThrottledJwks(
            jwt.PyJWKClient(f"{self._issuer}/.well-known/jwks.json", lifespan=600, timeout=5)
        )

    def verify(self, token: str) -> CurrentUser:
        try:
            algorithm = jwt.get_unverified_header(token).get("alg")
            if algorithm in _ASYMMETRIC_ALGORITHMS:
                key: Any = self._jwks.get_signing_key_from_jwt(token).key
            elif algorithm == "HS256" and self._jwt_secret:
                key = self._jwt_secret
            else:
                raise InvalidTokenError(f"algoritmo não aceito: {algorithm}")
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=_AUDIENCE,
                issuer=self._issuer,
                leeway=_LEEWAY_SECONDS,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except jwt.PyJWKClientConnectionError as exc:
            raise AuthUnavailableError(str(exc)) from exc
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(type(exc).__name__) from exc

        if claims.get("role") != "authenticated":
            raise InvalidTokenError("role")
        try:
            user_id = UUID(str(claims["sub"]))
        except ValueError as exc:
            raise InvalidTokenError("sub") from exc

        email = claims.get("email")
        app_metadata = claims.get("app_metadata")
        provider = app_metadata.get("provider") if isinstance(app_metadata, dict) else None
        return CurrentUser(
            id=user_id,
            email=email.strip().lower() if isinstance(email, str) and email else None,
            provider=provider if isinstance(provider, str) else None,
        )


def is_admin(user: CurrentUser, admin_email: str | None) -> bool:
    """Admin = e-mail igual a ADMIN_EMAIL numa conta Google (e-mail verificado pelo Google)."""
    return bool(admin_email) and user.provider == "google" and user.email == admin_email


@lru_cache
def _build_verifier(supabase_url: str, jwt_secret: str | None) -> TokenVerifier:
    return TokenVerifier(supabase_url=supabase_url, jwt_secret=jwt_secret)


def get_token_verifier(settings: Annotated[Settings, Depends(get_settings)]) -> TokenVerifier:
    if not settings.supabase_url:
        raise AppError(503, "auth_not_configured", "Login indisponível no momento.")
    return _build_verifier(settings.supabase_url, settings.supabase_jwt_secret)


_bearer = HTTPBearer(auto_error=False)


def _unauthorized() -> AppError:
    return AppError(
        401,
        "unauthorized",
        "Você precisa entrar com sua conta Google.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
) -> CurrentUser:
    if credentials is None:
        raise _unauthorized()
    try:
        # PyJWKClient faz I/O síncrono quando precisa buscar as chaves.
        return await run_in_threadpool(verifier.verify, credentials.credentials)
    except InvalidTokenError as exc:
        logger.info("invalid_token", extra={"reason": str(exc)})
        raise _unauthorized() from exc
    except AuthUnavailableError as exc:
        logger.error("jwks_unavailable", extra={"reason": str(exc)})
        raise AppError(503, "auth_unavailable", "Login indisponível no momento.") from exc


async def require_admin(
    user: Annotated[CurrentUser, Depends(current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CurrentUser:
    if not is_admin(user, settings.admin_email):
        raise AppError(403, "forbidden", "Você não tem permissão para isso.")
    return user
