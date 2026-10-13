import json
import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.logging_setup import JsonFormatter
from tests.conftest import ALLOWED_ORIGIN


def test_unhandled_error_returns_friendly_500_with_headers(
    app: FastAPI, client: TestClient
) -> None:
    async def boom() -> None:
        raise RuntimeError("falha interna com detalhe sensível")

    app.add_api_route("/boom", boom)

    response = client.get("/boom", headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "sensível" not in response.text
    assert response.headers["x-request-id"]
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


def test_http_exception_keeps_its_headers(app: FastAPI, client: TestClient) -> None:
    async def limited() -> None:
        raise HTTPException(status_code=429, headers={"Retry-After": "30"})

    app.add_api_route("/limited", limited)

    response = client.get("/limited")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.headers["retry-after"] == "30"


def test_app_error_keeps_its_headers(app: FastAPI, client: TestClient) -> None:
    async def needs_login() -> None:
        raise AppError(401, "unauthorized", "Entre.", headers={"WWW-Authenticate": "Bearer"})

    app.add_api_route("/needs-login", needs_login)

    response = client.get("/needs-login")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_settings_reject_cors_wildcard() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_origins="*")


def test_settings_convert_supabase_url_to_asyncpg() -> None:
    settings = Settings(_env_file=None, database_url="postgresql://u:p@host:6543/postgres")

    assert settings.database_url == "postgresql+asyncpg://u:p@host:6543/postgres"


def test_log_extras_cannot_override_fixed_fields() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "mensagem", None, None)
    record.severity = "FALSO"
    record.path = "/health"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["severity"] == "INFO"
    assert payload["path"] == "/health"


def test_request_log_never_records_the_client_ip(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """LGPD: IP é dado pessoal; o log de acesso não o grava (nem de cabeçalho forjado)."""
    caplog.set_level(logging.INFO, logger="app.request")
    client.get(
        "/health", headers={"cf-connecting-ip": "203.0.113.77", "x-forwarded-for": "198.51.100.9"}
    )

    records = [r for r in caplog.records if r.name == "app.request"]
    assert records
    for record in records:
        assert not hasattr(record, "client_ip")
        assert "203.0.113.77" not in JsonFormatter().format(record)
        assert "198.51.100.9" not in JsonFormatter().format(record)
