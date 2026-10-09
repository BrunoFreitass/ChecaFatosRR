from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app

ALLOWED_ORIGIN = "https://checafatosrr.test"
TEST_ADMIN_EMAIL = "admin@example.com"
TEST_BAN_SALT = "sal-de-teste"


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    # Isola os testes de qualquer backend/.env local.
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORS_ORIGINS", ALLOWED_ORIGIN)
    monkeypatch.setenv("SUPABASE_URL", "https://test-ref.supabase.co")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "")
    monkeypatch.setenv("ADMIN_EMAIL", TEST_ADMIN_EMAIL)
    monkeypatch.setenv("BAN_HASH_SALT", TEST_BAN_SALT)
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
