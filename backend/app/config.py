import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Nome de modelo do Gemini como vai na URL da API (ex.: gemini-3.8-flash).
_MODEL_NAME = re.compile(r"[a-z0-9][a-z0-9.\-]*")


class Settings(BaseSettings):
    """Configuração lida de variáveis de ambiente (ou de backend/.env em desenvolvimento)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Origens do frontend, separadas por vírgula. "*" é recusado.
    cors_origins: str = "http://localhost:5173"
    # String do pooler do Supabase (transaction mode, porta 6543). Vazio = sem banco.
    database_url: str | None = Field(default=None, repr=False)
    database_ssl: bool = True
    # Caminho do certificado CA do Supabase. Se definido, valida o certificado (verify-full).
    database_ssl_root_cert: str | None = None

    # Supabase Auth. URL do projeto: https://<project_ref>.supabase.co
    supabase_url: str | None = None
    # Só para projetos com "Legacy JWT Secret" (HS256). Vazio = só chaves assimétricas (JWKS).
    supabase_jwt_secret: str | None = Field(default=None, repr=False)
    # Chave secreta (sb_secret_...) — usada apenas para excluir contas (LGPD). Nunca no frontend.
    supabase_secret_key: str | None = Field(default=None, repr=False)
    # E-mail Google do administrador.
    admin_email: str | None = None
    # Segredo do HMAC de banimento. Se mudar, os banimentos existentes deixam de valer.
    ban_hash_salt: str | None = Field(default=None, repr=False)

    # ---- Checagem (etapa 2) ----
    # Chave do Google AI Studio. Vazio = checagem indisponível (503).
    gemini_api_key: str | None = Field(default=None, repr=False)
    # Cadeia de modelos, separados por vírgula: o primeiro é o principal; em 429 de cota DIÁRIA
    # de um modelo, a checagem tenta o próximo. Só nomes fixos (sem "latest"/"preview"): o 3.8
    # foi escolhido em 09/10/2026 pela lista de modelos da chave (models.list).
    gemini_model_fallbacks: str = "gemini-3.8-flash,gemini-3.7-flash"
    # 0 = sem "thinking": com o padrão o Flash 3.x passou de 60 s; com 0, de 7 a 13 s.
    # Vale para a checagem e (etapa 4) para a moderação.
    gemini_thinking_budget: int = Field(default=0, ge=0)
    # Teto de cada tentativa, novas tentativas (só 429/500/503/504/rede) e orçamento total.
    gemini_attempt_timeout_seconds: float = Field(default=25, gt=0, le=60)
    gemini_max_retries: int = Field(default=2, ge=0, le=2)
    gemini_budget_seconds: float = Field(default=60, gt=0, le=60)
    # Cotas diárias (dia do Pacífico, quando a cota gratuita zera). A de moderação é da etapa 4.
    gemini_daily_checks: int = Field(default=200, ge=0)
    gemini_daily_moderation: int = Field(default=200, ge=0)
    # Chave restrita à Fact Check Tools API. Vazio = segue sem checagens de agências.
    google_factcheck_api_key: str | None = Field(default=None, repr=False)
    check_rate_limit_per_hour: int = Field(default=10, ge=1)
    # Teto de checagens novas por usuário em 24 h (consultas ao cache não contam).
    check_daily_limit_per_user: int = Field(default=20, ge=1)
    cache_lookup_limit_per_hour: int = Field(default=60, ge=1)
    check_cache_hours: int = Field(default=24, ge=1)
    fetch_timeout_seconds: float = Field(default=10, gt=0, le=30)
    fetch_max_bytes: int = Field(default=2_000_000, ge=100_000, le=10_000_000)

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
        "gemini_api_key",
        "google_factcheck_api_key",
    )
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("gemini_model_fallbacks")
    @classmethod
    def _normalize_model_chain(cls, value: str) -> str:
        models = list(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))
        if not models:
            raise ValueError("GEMINI_MODEL_FALLBACKS precisa de pelo menos um modelo")
        invalid = [name for name in models if not _MODEL_NAME.fullmatch(name)]
        if invalid:
            raise ValueError(f"nome de modelo inválido em GEMINI_MODEL_FALLBACKS: {invalid}")
        return ",".join(models)

    @property
    def gemini_models(self) -> tuple[str, ...]:
        """A cadeia de GEMINI_MODEL_FALLBACKS, em ordem e sem repetição."""
        return tuple(self.gemini_model_fallbacks.split(","))

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
