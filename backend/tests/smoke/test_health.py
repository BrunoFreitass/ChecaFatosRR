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
