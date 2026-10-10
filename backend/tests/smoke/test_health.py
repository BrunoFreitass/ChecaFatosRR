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
