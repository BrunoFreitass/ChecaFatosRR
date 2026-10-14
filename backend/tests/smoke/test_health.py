import pytest
from fastapi.testclient import TestClient

from tests.conftest import ALLOWED_ORIGIN


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_sets_request_id_and_security_headers(client: TestClient) -> None:
    response = client.get("/health")

    assert response.headers["x-request-id"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_ready_without_database_returns_friendly_503(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "db_not_configured"
    assert body["error"]["message"]


def test_unknown_route_uses_error_format(client: TestClient) -> None:
    response = client.get("/nao-existe")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_cors_allows_configured_origin(client: TestClient) -> None:
    response = client.options(
        "/health",
        headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


def test_cors_rejects_other_origin(client: TestClient) -> None:
    response = client.options(
        "/health",
        headers={"Origin": "https://malicioso.example", "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in response.headers


def test_api_sends_hsts_and_restrictive_csp(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert (
        response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    )


def test_oversized_body_is_rejected_before_parsing(client: TestClient) -> None:
    # Sem teto, corpos de dezenas de MB (o Cloud Run aceita 32 MiB) derrubam a instância.
    response = client.post(
        "/checks", content=b"x" * (65 * 1024), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "corpo_muito_grande"
    assert response.headers["x-request-id"]


def test_oversized_chunked_body_is_rejected(client: TestClient) -> None:
    def chunks():  # sem Content-Length: conta os bytes recebidos
        for _ in range(70):
            yield b"x" * 1024

    response = client.post(
        "/checks", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "corpo_muito_grande"


def test_normal_body_passes_the_size_limit(client: TestClient) -> None:
    response = client.post("/checks", json={"texto": "a" * 5000})
    assert response.status_code == 401  # passou do limite e chegou na autenticação


def test_invalid_content_length_is_400_not_413(client: TestClient) -> None:
    response = client.post(
        "/checks",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "abc"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "requisicao_invalida"


def test_docs_in_development_are_not_blocked_by_the_api_csp(client: TestClient) -> None:
    # /docs só existe fora de produção e carrega scripts externos: sem a CSP da API.
    response = client.get("/docs")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    assert client.get("/health").headers["content-security-policy"]


def test_ready_hits_the_database_once_per_minute(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Público e sem login: chamadas em massa recebem o cache e não ocupam o pool.
    from app.routers import health

    calls: list[int] = []

    async def probe() -> None:
        calls.append(1)

    monkeypatch.setattr(health, "probe_database", probe)
    for _ in range(5):
        body = client.get("/health/ready").json()
        assert body == {"status": "ok", "database": "ok", "schema": "ok"}
    assert calls == [1]


# ---------- HEAD (o UptimeRobot grátis só faz HEAD; antes era 405, 14/10/2026) ----------


def test_head_health_is_200_without_body(client: TestClient) -> None:
    response = client.head("/health")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["x-request-id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"]


def test_head_ready_is_200_when_the_database_and_schema_are_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import health

    async def probe() -> None:
        return None

    monkeypatch.setattr(health, "probe_database", probe)
    response = client.head("/health/ready")

    assert response.status_code == 200
    assert response.content == b""


def test_head_ready_is_503_without_database(client: TestClient) -> None:
    response = client.head("/health/ready")

    assert response.status_code == 503
    assert response.content == b""


def test_head_ready_is_503_with_outdated_schema(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import health

    async def probe() -> None:
        raise health.SchemaOutdatedError(["20991231000000_futura"])

    monkeypatch.setattr(health, "probe_database", probe)

    assert client.head("/health/ready").status_code == 503
    get = client.get("/health/ready")  # mesmo status no GET, com o corpo de sempre
    assert get.status_code == 503
    assert get.json()["error"]["code"] == "schema_desatualizado"


def test_other_methods_are_still_405(client: TestClient) -> None:
    assert client.post("/health").status_code == 405
    assert client.delete("/health/ready").status_code == 405


def test_openapi_has_no_duplicate_operation_ids() -> None:
    """GET e HEAD na mesma rota geravam operationId duplicado no /docs (aviso no OpenAPI)."""
    import warnings

    from app.main import create_app

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        schema = create_app().openapi()

    assert "get" in schema["paths"]["/health"] and "head" not in schema["paths"]["/health"]


def test_head_ready_uses_the_same_cache_as_get(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import health

    calls: list[int] = []

    async def probe() -> None:
        calls.append(1)

    monkeypatch.setattr(health, "probe_database", probe)
    assert client.get("/health/ready").status_code == 200
    assert client.head("/health/ready").status_code == 200
    assert calls == [1]  # o monitor em HEAD não toca o banco mais que o GET
