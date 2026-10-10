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
    cert = tmp_path / "certs" / "supabase-ca.crt"
    cert.write_text(PEM, encoding="utf-8")
    assert resolve_backend_path("certs/supabase-ca.crt", base=tmp_path) == cert
    assert resolve_backend_path(str(cert), base=Path("/outro")) == cert  # absoluto fica igual
