"""Configuração: o arquivo de variáveis local só é lido de propósito, e erro de validação nunca
mostra valores (incidente de 14/10/2026: um erro de configuração exibiu o fim de um valor).

Tudo aqui usa um arquivo FICTÍCIO numa pasta temporária, nunca o arquivo real do projeto.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import DOTENV_FLAG, Settings, get_settings

# Certificado público da CA do Supabase (no repositório; não é segredo).
CA = str(Path(__file__).parents[2] / "certs" / "supabase-ca.crt")
FAKE_SECRET = "valor-ficticio-que-nunca-pode-aparecer-0123456789"  # pragma: allowlist secret


@pytest.fixture
def folder_with_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".env").write_text(
        f"GEMINI_API_KEY={FAKE_SECRET}\nAPP_ENV=production\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DOTENV_FLAG, raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    get_settings.cache_clear()
    return tmp_path


def test_the_local_file_is_not_read_by_default(folder_with_dotenv: Path) -> None:
    settings = get_settings()

    assert settings.gemini_api_key != FAKE_SECRET
    assert settings.app_env != "production"
    get_settings.cache_clear()


def test_the_flag_turns_the_local_file_on(
    folder_with_dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DOTENV_FLAG, "1")
    monkeypatch.setenv("DATABASE_SSL_ROOT_CERT", CA)  # produção exige o certificado

    assert get_settings().gemini_api_key == FAKE_SECRET
    get_settings.cache_clear()


def test_a_path_passed_on_purpose_is_read(folder_with_dotenv: Path) -> None:
    settings = Settings(_env_file=folder_with_dotenv / ".env", database_ssl_root_cert=CA)  # type: ignore[call-arg]

    assert settings.gemini_api_key == FAKE_SECRET


def test_validation_errors_never_show_the_values(
    folder_with_dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O mesmo erro do incidente: produção sem o certificado do banco.
    monkeypatch.setenv(DOTENV_FLAG, "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:senha-ficticia@h:6543/postgres")
    monkeypatch.delenv("DATABASE_SSL_ROOT_CERT", raising=False)

    with pytest.raises(ValidationError) as caught:
        get_settings()
    get_settings.cache_clear()

    text = str(caught.value)
    assert "DATABASE_SSL_ROOT_CERT" in text  # o motivo continua claro
    assert FAKE_SECRET not in text and FAKE_SECRET[-20:] not in text
    assert "senha-ficticia" not in text
    assert "input_value" not in text
