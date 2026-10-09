"""Dublês de teste: chaves/tokens JWT, repositório de perfis em memória e Admin API falsa."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from app.repositories.profiles import NicknameTakenError, Profile, ProfileExistsError

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
        self.blocked_nicknames: list[tuple[str, str]] = []

    async def get(self, user_id: UUID) -> Profile | None:
        return self.profiles.get(user_id)

    async def nickname_taken(self, apelido: str) -> bool:
        return any(p.apelido.casefold() == apelido.casefold() for p in self.profiles.values())

    async def google_sub(self, user_id: UUID) -> str | None:
        return self.google_subs.get(user_id)

    async def is_banned(self, sub_hash: bytes) -> bool:
        return sub_hash in self.banned_hashes

    async def create(self, user_id: UUID, apelido: str, terms_version: str) -> Profile:
        if user_id in self.profiles:
            raise ProfileExistsError
        if await self.nickname_taken(apelido):
            raise NicknameTakenError
        profile = Profile(
            id=user_id,
            apelido=apelido,
            status="active",
            suspended_until=None,
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
