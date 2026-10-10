from collections.abc import Iterator
from typing import NoReturn

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google import genai

from app.config import Settings, get_settings
from app.main import create_app

ALLOWED_ORIGIN = "https://checafatosrr.test"
TEST_ADMIN_EMAIL = "admin@example.com"
TEST_BAN_SALT = "sal-de-teste"


@pytest.fixture(autouse=True)
def _no_real_secrets_or_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regra do projeto: testes só com mocks. Nenhum teste lê backend/.env nem usa chave real,
    # e o cliente do Gemini falha se algum teste esquecer de simulá-lo.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    # Nada do shell do desenvolvedor vaza para Settings() (chaves, banco, cadeia de modelos).
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)

    def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("Teste tentou criar um cliente real do Gemini; use um mock.")

    monkeypatch.setattr(genai, "Client", _forbidden)


@pytest.fixture
def anyio_backend() -> str:
    # Testes async (pytest.mark.anyio) rodam só no asyncio, como o uvicorn em produção.
    return "asyncio"


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
