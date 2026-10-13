from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import TokenVerifier, get_token_verifier
from app.legal import CURRENT_TERMS_VERSION
from app.routers.profiles import get_profile_repository
from app.services.bans import ban_hash
from app.services.supabase_admin import get_supabase_admin
from tests.conftest import TEST_ADMIN_EMAIL, TEST_BAN_SALT
from tests.fakes import (
    TEST_SUPABASE_URL,
    FakeProfileRepository,
    FakeSupabaseAdmin,
    StaticJwks,
    make_es256_keys,
    make_token,
)

PRIVATE_KEY, PUBLIC_KEY = make_es256_keys()


@pytest.fixture
def repo() -> FakeProfileRepository:
    return FakeProfileRepository()


@pytest.fixture
def admin_api() -> FakeSupabaseAdmin:
    return FakeSupabaseAdmin()


@pytest.fixture
def api(
    app: FastAPI, repo: FakeProfileRepository, admin_api: FakeSupabaseAdmin
) -> Iterator[TestClient]:
    app.dependency_overrides[get_token_verifier] = lambda: TokenVerifier(
        supabase_url=TEST_SUPABASE_URL, jwks=StaticJwks(PUBLIC_KEY)
    )
    app.dependency_overrides[get_profile_repository] = lambda: repo
    app.dependency_overrides[get_supabase_admin] = lambda: admin_api
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def login(repo: FakeProfileRepository, email: str = "pessoa@example.com") -> tuple[UUID, dict]:
    user_id = uuid4()
    repo.google_subs[user_id] = f"google-{user_id.hex}"
    token = make_token(PRIVATE_KEY, sub=str(user_id), email=email)
    return user_id, {"Authorization": f"Bearer {token}"}


def body(apelido: str = "bruno_rr", **overrides: object) -> dict[str, object]:
    return {"apelido": apelido, "aceito_termos": True, "terms_version": CURRENT_TERMS_VERSION} | (
        overrides
    )


def test_me_requires_token(api: TestClient) -> None:
    response = api.get("/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_rejects_invalid_token(api: TestClient) -> None:
    response = api.get("/me", headers={"Authorization": "Bearer lixo"})

    assert response.status_code == 401


def test_me_without_profile_asks_for_onboarding(
    api: TestClient, repo: FakeProfileRepository
) -> None:
    _, headers = login(repo)

    response = api.get("/me", headers=headers)

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "profile_not_found"
    assert error["terms_version"] == CURRENT_TERMS_VERSION


def test_create_profile_then_read_it(api: TestClient, repo: FakeProfileRepository) -> None:
    _, headers = login(repo)

    created = api.post("/me", json=body(), headers=headers)
    read = api.get("/me", headers=headers)

    assert created.status_code == 201
    assert read.status_code == 200
    profile = read.json()
    assert profile["apelido"] == "bruno_rr"
    assert profile["status"] == "active"
    assert profile["is_admin"] is False
    assert "email" not in profile


def test_admin_flag_for_admin_email(api: TestClient, repo: FakeProfileRepository) -> None:
    _, headers = login(repo, email=TEST_ADMIN_EMAIL.upper())

    response = api.post("/me", json=body("dono_rr"), headers=headers)

    assert response.json()["is_admin"] is True


def test_duplicate_nickname_is_409(api: TestClient, repo: FakeProfileRepository) -> None:
    _, first = login(repo)
    _, second = login(repo)
    api.post("/me", json=body("mesmo_nome"), headers=first)

    response = api.post("/me", json=body("MESMO_NOME"), headers=second)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "apelido_em_uso"


def test_second_profile_for_same_user_is_409(api: TestClient, repo: FakeProfileRepository) -> None:
    _, headers = login(repo)
    api.post("/me", json=body("primeiro"), headers=headers)

    response = api.post("/me", json=body("segundo"), headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "perfil_existente"


def test_forbidden_nickname_shows_violation_and_is_logged(
    api: TestClient, repo: FakeProfileRepository
) -> None:
    _, headers = login(repo)

    response = api.post("/me", json=body("p0rr4_rr"), headers=headers)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "apelido_proibido"
    assert error["regra"]
    assert error["explicacao"]
    assert repo.blocked_nicknames == [("p0rr4_rr", "palavrao")]


@pytest.mark.parametrize(
    ("apelido", "code"),
    [("ab", "apelido_invalido"), ("joão", "apelido_invalido"), ("admin", "apelido_reservado")],
)
def test_invalid_or_reserved_nickname(
    api: TestClient, repo: FakeProfileRepository, apelido: str, code: str
) -> None:
    _, headers = login(repo)

    response = api.post("/me", json=body(apelido), headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


def test_outdated_terms_is_422(api: TestClient, repo: FakeProfileRepository) -> None:
    _, headers = login(repo)

    response = api.post("/me", json=body(terms_version="2000-01-01"), headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "termos_desatualizados"


def test_terms_must_be_accepted(api: TestClient, repo: FakeProfileRepository) -> None:
    _, headers = login(repo)

    response = api.post("/me", json=body(aceito_termos=False), headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_banned_google_identity_cannot_create_profile(
    api: TestClient, repo: FakeProfileRepository
) -> None:
    user_id, headers = login(repo)
    repo.banned_hashes.add(ban_hash(repo.google_subs[user_id], salt=TEST_BAN_SALT))

    response = api.post("/me", json=body(), headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "conta_banida"
    assert user_id not in repo.profiles


def test_user_without_google_identity_is_rejected(
    api: TestClient, repo: FakeProfileRepository
) -> None:
    user_id, headers = login(repo)
    del repo.google_subs[user_id]

    response = api.post("/me", json=body(), headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "login_google_obrigatorio"


def test_expired_suspension_reads_as_active(api: TestClient, repo: FakeProfileRepository) -> None:
    user_id, headers = login(repo)
    api.post("/me", json=body(), headers=headers)
    repo.set_status(user_id, "suspended", datetime.now(UTC) - timedelta(minutes=1))

    assert api.get("/me", headers=headers).json()["status"] == "active"


@pytest.mark.parametrize(
    ("q", "disponivel", "motivo"),
    [
        ("livre_rr", True, None),
        ("ocupado", False, "em_uso"),
        ("ab", False, "formato"),
        ("moderador", False, "reservado"),
        ("caralho", False, "conteudo_proibido"),
    ],
)
def test_nickname_availability(
    api: TestClient,
    repo: FakeProfileRepository,
    q: str,
    disponivel: bool,
    motivo: str | None,
) -> None:
    _, owner = login(repo)
    api.post("/me", json=body("ocupado"), headers=owner)
    _, headers = login(repo)

    response = api.get("/apelido/disponivel", params={"q": q}, headers=headers)

    assert response.status_code == 200
    result = response.json()
    assert result["disponivel"] is disponivel
    assert result["motivo"] == motivo


def test_delete_account_calls_supabase_admin(
    api: TestClient, repo: FakeProfileRepository, admin_api: FakeSupabaseAdmin
) -> None:
    user_id, headers = login(repo)
    api.post("/me", json=body(), headers=headers)

    response = api.delete("/me", headers=headers)

    assert response.status_code == 204
    assert admin_api.deleted == [user_id]


def test_nickname_check_is_rate_limited_per_user(
    api: TestClient, repo: FakeProfileRepository
) -> None:
    _, headers = login(repo)
    for _ in range(30):
        ok = api.get("/apelido/disponivel", params={"q": "bruno_rr"}, headers=headers)
        assert ok.status_code == 200

    blocked = api.get("/apelido/disponivel", params={"q": "bruno_rr"}, headers=headers)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) >= 1

    _, other = login(repo)  # outro usuário não é afetado
    assert api.get("/apelido/disponivel", params={"q": "x_rr"}, headers=other).status_code == 200


def test_profile_creation_is_rate_limited(api: TestClient, repo: FakeProfileRepository) -> None:
    _, headers = login(repo)
    for _ in range(10):  # apelidos proibidos: cada tentativa grava no log de moderação
        api.post("/me", json=body("porra_rr"), headers=headers)

    blocked = api.post("/me", json=body("bruno_rr"), headers=headers)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"


def test_delete_account_keeps_strikes_by_sub_hash(
    api: TestClient, repo: FakeProfileRepository
) -> None:
    user_id, headers = login(repo)
    api.post("/me", json=body(), headers=headers)
    repo.strikes[user_id] = 2

    assert api.delete("/me", headers=headers).status_code == 204

    sub_hash = ban_hash(repo.google_subs[user_id], salt=TEST_BAN_SALT)
    assert repo.kept_strikes == {sub_hash: 2}


def test_delete_account_without_google_sub_logs_the_loss(
    api: TestClient, repo: FakeProfileRepository, caplog: pytest.LogCaptureFixture
) -> None:
    user_id, headers = login(repo)
    api.post("/me", json=body(), headers=headers)
    repo.strikes[user_id] = 1
    del repo.google_subs[user_id]

    assert api.delete("/me", headers=headers).status_code == 204

    assert repo.kept_strikes == {}
    assert "moderation_not_kept_without_google_sub" in caplog.messages


def test_legal_version_is_public_and_matches_the_backend(api: TestClient) -> None:
    response = api.get("/legal/version")

    assert response.status_code == 200
    assert response.json() == {"version": CURRENT_TERMS_VERSION}
    # Sem cache: depois de um deploy com Termos novos, o "tentar de novo" já vê a versão nova.
    assert response.headers["Cache-Control"] == "no-cache"
