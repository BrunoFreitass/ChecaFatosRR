from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração lida de variáveis de ambiente (ou de backend/.env em desenvolvimento)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Origens do frontend, separadas por vírgula. "*" é recusado.
    cors_origins: str = "http://localhost:5173"
    # String do pooler do Supabase (transaction mode, porta 6543). Vazio = sem banco.
    database_url: str | None = None
    database_ssl: bool = True
    # Caminho do certificado CA do Supabase. Se definido, valida o certificado (verify-full).
    database_ssl_root_cert: str | None = None

    # Supabase Auth. URL do projeto: https://<project_ref>.supabase.co
    supabase_url: str | None = None
    # Só para projetos com "Legacy JWT Secret" (HS256). Vazio = só chaves assimétricas (JWKS).
    supabase_jwt_secret: str | None = None
    # Chave secreta (sb_secret_...) — usada apenas para excluir contas (LGPD). Nunca no frontend.
    supabase_secret_key: str | None = None
    # E-mail Google do administrador.
    admin_email: str | None = None
    # Segredo do HMAC de banimento. Se mudar, os banimentos existentes deixam de valer.
    ban_hash_salt: str | None = None

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("cors_origins")
    @classmethod
    def _reject_wildcard(cls, value: str) -> str:
        if "*" in value:
            raise ValueError("CORS_ORIGINS não pode conter '*'; liste as origens do frontend")
        return value

    @field_validator(
        "database_url",
        "database_ssl_root_cert",
        "supabase_url",
        "supabase_jwt_secret",
        "supabase_secret_key",
        "admin_email",
        "ban_hash_salt",
    )
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("supabase_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else None

    @field_validator("admin_email")
    @classmethod
    def _lower_email(cls, value: str | None) -> str | None:
        return value.lower() if value else None

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Aceita a string copiada do painel do Supabase ("postgresql://...").
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value.removeprefix(prefix)
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
