"""Deploy: certificado do banco (verify-full) e arquivos que vão para o Cloud Run."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

BACKEND = Path(__file__).parents[2]
PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def test_root_cert_that_does_not_exist_fails_at_startup(tmp_path: Path) -> None:
    # Melhor a revisão do Cloud Run não subir (erro claro no log) do que falhar na 1ª conexão.
    with pytest.raises(ValidationError, match="DATABASE_SSL_ROOT_CERT"):
        Settings(database_ssl_root_cert=str(tmp_path / "nao-existe.crt"))


def test_root_cert_must_be_a_pem_certificate(tmp_path: Path) -> None:
    wrong = tmp_path / "chave.txt"
    wrong.write_text("isto não é certificado", encoding="utf-8")
    with pytest.raises(ValidationError, match="PEM"):
        Settings(database_ssl_root_cert=str(wrong))


def test_valid_root_cert_is_accepted(tmp_path: Path) -> None:
    cert = tmp_path / "supabase-ca.crt"
    cert.write_text(PEM, encoding="utf-8")
    assert Settings(database_ssl_root_cert=str(cert)).database_ssl_root_cert == str(cert)


def test_dockerfile_ships_the_certs_folder() -> None:
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY certs ./certs" in dockerfile


def test_gcloudignore_uploads_certs_but_never_env_files() -> None:
    lines = {
        line.strip()
        for line in (BACKEND / ".gcloudignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert not any("cert" in line for line in lines)
    assert {".env", ".env.*", ".venv"} <= lines


def test_production_requires_the_database_root_cert() -> None:
    # Sem o certificado, o asyncpg cai em ssl="require": criptografa, mas não valida (MITM).
    with pytest.raises(ValidationError, match="DATABASE_SSL_ROOT_CERT"):
        Settings(app_env="production", database_url="postgresql://u:p@db.example.com:6543/postgres")


def test_production_with_root_cert_is_accepted(tmp_path: Path) -> None:
    cert = tmp_path / "supabase-ca.crt"
    cert.write_text(PEM, encoding="utf-8")
    settings = Settings(
        app_env="production",
        database_url="postgresql://u:p@db.example.com:6543/postgres",
        database_ssl_root_cert=str(cert),
    )
    assert settings.database_ssl_root_cert == str(cert)


def test_development_does_not_require_the_root_cert() -> None:
    assert (
        Settings(app_env="development", database_url="postgresql://u:p@x/db").app_env
        == "development"
    )


def test_relative_root_cert_is_resolved_from_the_backend_folder(tmp_path: Path) -> None:
    # "certs/supabase-ca.crt" funciona rodando de backend/, da raiz do repo ou da IDE.
    from app.config import resolve_backend_path

    (tmp_path / "certs").mkdir()
    # Nome que não existe no repositório: o teste não depende da pasta de onde o pytest roda.
    cert = tmp_path / "certs" / "so-no-teste-ca.crt"
    cert.write_text(PEM, encoding="utf-8")
    assert resolve_backend_path("certs/so-no-teste-ca.crt", base=tmp_path) == cert
    assert resolve_backend_path(str(cert), base=Path("/outro")) == cert  # absoluto fica igual


def test_uvicorn_limits_concurrency_from_the_environment() -> None:
    # Acima do limite o uvicorn responde 503 (inclusive ao /health) em vez de afogar 512 MB. Ele
    # conta CONEXÕES abertas, inclusive keep-alive ociosas, e cada checagem segura uma por até
    # ~90 s esperando o Gemini: 100 deixa folga para o health check do Render não cair.
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    assert "--limit-concurrency ${UVICORN_LIMIT_CONCURRENCY:-100}" in dockerfile


SECRETS = {
    "DATABASE_URL",
    "SUPABASE_SECRET_KEY",
    "BAN_HASH_SALT",
    "GEMINI_API_KEY",
    "GOOGLE_FACTCHECK_API_KEY",
}


def render_service() -> dict[str, object]:
    import yaml

    blueprint = yaml.safe_load((BACKEND.parent / "render.yaml").read_text(encoding="utf-8"))
    services = blueprint["services"]
    assert len(services) == 1  # um segundo serviço Free estouraria as 750 h do workspace
    assert blueprint["previews"]["generation"] == "off"  # previews gastam horas e herdam env
    return services[0]


def test_render_blueprint_is_the_free_docker_service_in_virginia() -> None:
    service = render_service()
    assert service["type"] == "web"
    assert service["runtime"] == "docker"
    assert service["plan"] == "free"  # a troca para Starter é pelo painel, não por commit
    assert service["region"] == "virginia"
    assert service["branch"] == "main"
    # Sem rootDir: com ele, o Render poderia resolver os caminhos abaixo a partir de backend/.
    assert "rootDir" not in service
    assert service["dockerfilePath"] == "./backend/Dockerfile"
    assert service["dockerContext"] == "./backend"
    assert service["buildFilter"]["paths"] == ["backend/**"]  # só o backend redeploya
    env = {item["key"]: item for item in service["envVars"]}
    assert env["UVICORN_LIMIT_CONCURRENCY"]["value"] == "100"
    assert service["healthCheckPath"] == "/health"
    assert service["autoDeployTrigger"] == "checksPass"  # só depois da CI verde


def test_render_blueprint_never_holds_secret_values() -> None:
    env = {item["key"]: item for item in render_service()["envVars"]}  # type: ignore[union-attr]
    for key in SECRETS | {"ADMIN_EMAIL", "SUPABASE_URL"}:
        assert env[key] == {"key": key, "sync": False}, key
    assert "PORT" not in env  # o Render injeta
    assert env["DATABASE_SSL_ROOT_CERT"]["value"] == "/srv/certs/supabase-ca.crt"
    assert env["EXTRACT_CONCURRENCY"]["value"] == "1"
