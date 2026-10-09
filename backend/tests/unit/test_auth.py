from datetime import timedelta
from uuid import UUID, uuid4

import jwt
import pytest

from app.auth import CurrentUser, InvalidTokenError, TokenVerifier, is_admin
from tests.fakes import TEST_SUPABASE_URL, StaticJwks, make_es256_keys, make_token

PRIVATE_KEY, PUBLIC_KEY = make_es256_keys()


def verifier(jwt_secret: str | None = None) -> TokenVerifier:
    return TokenVerifier(
        supabase_url=TEST_SUPABASE_URL, jwt_secret=jwt_secret, jwks=StaticJwks(PUBLIC_KEY)
    )


def test_accepts_valid_es256_token() -> None:
    sub = str(uuid4())

    user = verifier().verify(make_token(PRIVATE_KEY, sub=sub, email="Pessoa@Example.com"))

    assert user == CurrentUser(id=UUID(sub), email="pessoa@example.com", provider="google")


@pytest.mark.parametrize(
    "overrides",
    [
        {"expires_in": timedelta(minutes=-5)},
        {"audience": "outro"},
        {"issuer": "https://atacante.example/auth/v1"},
        {"role": "anon"},
        {"sub": "nao-e-uuid"},
    ],
)
def test_rejects_invalid_claims(overrides: dict[str, object]) -> None:
    params: dict[str, object] = {"sub": str(uuid4())} | overrides
    token = make_token(PRIVATE_KEY, **params)  # type: ignore[arg-type]

    with pytest.raises(InvalidTokenError):
        verifier().verify(token)


def test_rejects_signature_from_other_key() -> None:
    other_private, _ = make_es256_keys()

    with pytest.raises(InvalidTokenError):
        verifier().verify(make_token(other_private, sub=str(uuid4())))


def test_rejects_alg_none() -> None:
    token = jwt.encode(
        {"sub": str(uuid4()), "aud": "authenticated", "role": "authenticated"},
        key=None,
        algorithm="none",
    )

    with pytest.raises(InvalidTokenError):
        verifier().verify(token)


def test_rejects_hs256_when_no_secret_configured() -> None:
    token = make_token(
        "segredo-qualquer-com-32-bytes-ou-mais!", sub=str(uuid4()), algorithm="HS256"
    )

    with pytest.raises(InvalidTokenError):
        verifier().verify(token)


def test_accepts_hs256_with_configured_legacy_secret() -> None:
    secret = "segredo-legado-do-supabase-com-32-bytes!"
    sub = str(uuid4())

    user = verifier(jwt_secret=secret).verify(make_token(secret, sub=sub, algorithm="HS256"))

    assert user.id == UUID(sub)


def test_rejects_garbage() -> None:
    with pytest.raises(InvalidTokenError):
        verifier().verify("isto.nao.e-um-jwt")


class CountingJwksClient:
    """Imita PyJWKClient.get_signing_keys: conta quantas vezes o JWKS é buscado na rede."""

    def __init__(self, keys: list[object]) -> None:
        self.keys = keys
        self.network_fetches = 0

    def get_signing_keys(self, refresh: bool = False) -> list[object]:
        if refresh:
            self.network_fetches += 1
        return self.keys


def test_unknown_kid_refetches_jwks_at_most_once_per_interval() -> None:
    from types import SimpleNamespace

    from app.auth import ThrottledJwks

    client = CountingJwksClient([SimpleNamespace(key_id="kid-certo", key=PUBLIC_KEY)])
    jwks = ThrottledJwks(client, min_refresh_seconds=60)  # type: ignore[arg-type]
    forged = jwt.encode(
        {"sub": str(uuid4())}, PRIVATE_KEY, algorithm="ES256", headers={"kid": "kid-falso"}
    )

    for _ in range(20):
        with pytest.raises(InvalidTokenError):
            jwks.get_signing_key_from_jwt(forged)

    assert client.network_fetches == 1


def test_known_kid_uses_cache() -> None:
    from types import SimpleNamespace

    from app.auth import ThrottledJwks

    client = CountingJwksClient([SimpleNamespace(key_id="kid-certo", key=PUBLIC_KEY)])
    jwks = ThrottledJwks(client, min_refresh_seconds=60)  # type: ignore[arg-type]
    token = jwt.encode({"sub": "x"}, PRIVATE_KEY, algorithm="ES256", headers={"kid": "kid-certo"})

    assert jwks.get_signing_key_from_jwt(token).key is PUBLIC_KEY
    assert client.network_fetches == 0


ADMIN = "admin@example.com"


@pytest.mark.parametrize(
    ("email", "provider", "expected"),
    [
        ("admin@example.com", "google", True),
        ("outra@example.com", "google", False),
        ("admin@example.com", "email", False),
        (None, "google", False),
    ],
)
def test_is_admin(email: str | None, provider: str, expected: bool) -> None:
    user = CurrentUser(id=uuid4(), email=email, provider=provider)

    assert is_admin(user, ADMIN) is expected


def test_is_admin_false_when_not_configured() -> None:
    user = CurrentUser(id=uuid4(), email="admin@example.com", provider="google")

    assert is_admin(user, None) is False
