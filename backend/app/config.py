import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.model_limits import ModelLimits, parse_model_limits

# Domínios onde qualquer pessoa publica (blogs, newsletters, sites gratuitos): um link deles
# não é "origem confirmada" (OPEN_PLATFORM_DOMAINS; redes sociais entram sempre).
DEFAULT_OPEN_PLATFORMS: tuple[str, ...] = (
    "blogspot.com",
    "wordpress.com",
    "medium.com",
    "substack.com",
    "sites.google.com",
    "github.io",
    "wixsite.com",
    "tumblr.com",
    "weebly.com",
    "webnode.page",
    "jimdosite.com",
    "notion.site",
    "telegra.ph",
    "beehiiv.com",
    "linktr.ee",
)

# Arquivo de resultado do TSE em 2026 (conferido em 11/10/2026 com o 1º turno, eleição 6257).
TSE_URL_TEMPLATE = (
    "https://resultados.tse.jus.br/oficial/ele2026/{eleicao}/dados/{escopo}/"
    "{escopo}-c{cargo:04d}-e{eleicao:06d}-u.json"
)
TSE_HOST = "resultados.tse.jus.br"
TSE_ALLOWED_SCOPES = ("br", "rr")
_BOA_VISTA = timezone(timedelta(hours=-4))

# Pasta backend/: caminhos relativos (ex.: certs/supabase-ca.crt) valem a partir dela.
BACKEND_DIR = Path(__file__).resolve().parents[1]


def resolve_backend_path(value: str, base: Path = BACKEND_DIR) -> Path:
    """Absoluto fica igual; relativo vale a partir da pasta atual ou, se não existir lá, de
    backend/ (rodar da raiz do repositório ou da IDE dá no mesmo arquivo)."""
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    return base / path


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
    # Pool da CHECAGEM, em ordem (o primeiro com cota e vaga no minuto responde); depois dele,
    # o modo "só agências". Só nomes fixos (sem "latest"/"preview"), pela models.list de
    # 12/10/2026: 2.5-flash deu 404 (indisponível para contas novas) e 3-flash só existe como
    # preview, então ficam de fora. Pools separados: nenhum modelo da moderação aqui.
    gemini_model_fallbacks: str = (
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemma-4-31b-it"
    )
    # Limites por modelo por cima da tabela do nível gratuito (model_limits.py):
    # "modelo=rpm/rpd[/tpm],...". Vazio = a tabela.
    gemini_model_limits: str = ""
    # 0 = sem "thinking": com o padrão o Flash 3.x passou de 60 s; com 0, de 7 a 13 s.
    # Vale para a checagem e (etapa 4) para a moderação.
    gemini_thinking_budget: int = Field(default=0, ge=0)
    # Nível nativo dos 3.x; se definido, substitui o budget (o 3.8-flash recusa "minimal").
    # Saída de emergência sem deploy, se o log gemini_usage mostrar thoughts_tokens > 0.
    gemini_thinking_level: Literal["", "minimal", "low", "medium", "high"] = ""
    # Teto de cada tentativa, novas tentativas (só 429/500/503/504/rede) e orçamento total.
    # 48 s: um modelo lento gasta 1 tentativa (25 s) e o próximo da cadeia ainda tem ~23 s
    # (saudável responde em 7 a 13 s). A checagem inteira cabe em 90 s: o Render responde
    # atrás do Cloudflare, que corta em ~100 s (524). Teto = padrão (ver check_service).
    gemini_attempt_timeout_seconds: float = Field(default=25, gt=0, le=25)
    gemini_max_retries: int = Field(default=2, ge=0, le=2)
    gemini_budget_seconds: float = Field(default=48, gt=0, le=48)
    # Tetos de SEGURANÇA globais por dia (dia do Pacífico): quem limita de verdade é o RPD de
    # cada modelo (contador por modelo no banco). Esgotado: checagem no modo "só agências";
    # moderação publica e manda para a fila do admin (fail-open). Decisão de 12/10/2026.
    gemini_daily_checks: int = Field(default=1000, ge=0)
    gemini_daily_moderation: int = Field(default=2000, ge=0)
    # Pool da MODERAÇÃO (o Gemma é a reserva do Flash-Lite; decisão de 12/10/2026).
    gemini_moderation_models: str = "gemini-3.1-flash-lite,gemma-4-26b-a4b-it"
    # Moderações pela IA por usuário por dia: uma conta não gasta a cota de todos (etapa 4).
    moderation_ai_daily_per_user: int = Field(default=20, ge=0)
    # Comentários (etapa 3): 1 a cada 30 s e no máximo 50 por dia por usuário (12/10/2026).
    comment_min_interval_seconds: int = Field(default=30, ge=0)
    comments_daily_limit: int = Field(default=50, ge=1)
    # Chave restrita à Fact Check Tools API. Vazio = segue sem checagens de agências.
    google_factcheck_api_key: str | None = Field(default=None, repr=False)
    check_rate_limit_per_hour: int = Field(default=10, ge=1)
    # Teto de checagens novas por usuário em 24 h (consultas ao cache não contam).
    check_daily_limit_per_user: int = Field(default=20, ge=1)
    cache_lookup_limit_per_hour: int = Field(default=60, ge=1)
    check_cache_hours: int = Field(default=24, ge=1)
    fetch_timeout_seconds: float = Field(default=10, gt=0, le=30)
    fetch_max_bytes: int = Field(default=2_000_000, ge=100_000, le=10_000_000)
    # Parsers de HTML (lxml) ao mesmo tempo. No Render Free (0,1 CPU), 1.
    extract_concurrency: int = Field(default=2, ge=1, le=8)
    # Plataformas abertas, separadas por vírgula (ver DEFAULT_OPEN_PLATFORMS).
    open_platform_domains: str = ",".join(DEFAULT_OPEN_PLATFORMS)

    # Apuração do TSE (etapa 5). 6258 = 2º turno federal de 2026 (6257 = 1º turno, ensaio).
    tse_enabled: bool = True
    tse_url_template: str = TSE_URL_TEMPLATE
    tse_election_code: int = Field(default=6258, ge=1, le=999_999)
    tse_scopes: str = "br,rr"
    tse_refresh_seconds: int = Field(default=30, ge=10, le=600)
    # Banner da home (horário de Boa Vista): 16h de 25/10 (17h de Brasília) até o fim de 26/10.
    tse_banner_inicio: datetime = datetime(2026, 10, 25, 16, 0, tzinfo=_BOA_VISTA)
    tse_banner_fim: datetime = datetime(2026, 10, 27, 0, 0, tzinfo=_BOA_VISTA)

    # Pool de conexões ao banco (processo persistente no Render). 0 = NullPool (o Supavisor
    # agrupa sozinho; testes e plano B Cloud Run). Compatível com transaction mode: sem
    # prepared statements em cache e sem estado de sessão.
    db_pool_size: int = Field(default=3, ge=0, le=10)
    db_pool_max_overflow: int = Field(default=2, ge=0, le=10)
    db_pool_timeout_seconds: float = Field(default=8, gt=0, le=30)

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

    @field_validator("gemini_model_fallbacks", "gemini_moderation_models")
    @classmethod
    def _normalize_model_chain(cls, value: str, info: ValidationInfo) -> str:
        env = (info.field_name or "").upper()
        models = list(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))
        if not models:
            raise ValueError(f"{env} precisa de pelo menos um modelo")
        invalid = [name for name in models if not _MODEL_NAME.fullmatch(name)]
        if invalid:
            raise ValueError(f"nome de modelo inválido em {env}: {invalid}")
        return ",".join(models)

    @field_validator("tse_url_template")
    @classmethod
    def _tse_template_is_the_official_site(cls, value: str) -> str:
        parts = urlsplit(value)
        try:
            fields = {name for _, name, _, _ in Formatter().parse(value) if name is not None}
        except ValueError:
            fields = set()
        if (
            parts.scheme != "https"
            or parts.hostname != TSE_HOST
            or parts.port is not None
            or parts.username is not None
            or chr(92) in value
            or "escopo" not in fields
            or not fields <= {"escopo", "eleicao", "cargo"}
        ):
            raise ValueError(
                f"TSE_URL_TEMPLATE precisa ser https://{TSE_HOST}/... com {{escopo}} "
                "(e só {eleicao} e {cargo} além dele)"
            )
        return value

    @field_validator("tse_scopes")
    @classmethod
    def _tse_scopes_are_ufs(cls, value: str) -> str:
        items = [item.strip().lower() for item in value.split(",")]
        # A tabela tse_snapshots só aceita esses (check da migração inicial).
        if not items or any(item not in TSE_ALLOWED_SCOPES for item in items):
            raise ValueError("TSE_SCOPES: br e/ou rr, separados por vírgula")
        return ",".join(dict.fromkeys(items))

    @field_validator("tse_banner_inicio", "tse_banner_fim")
    @classmethod
    def _banner_has_timezone(cls, value: datetime) -> datetime:
        # Sem fuso no painel: horário de Boa Vista (o público do site).
        return value if value.tzinfo else value.replace(tzinfo=_BOA_VISTA)

    @field_validator("gemini_model_limits")
    @classmethod
    def _model_limits_are_valid(cls, value: str) -> str:
        parse_model_limits(value)  # ValueError com a mensagem certa
        return value

    @model_validator(mode="after")
    def _pools_are_separate(self) -> "Settings":
        limits = self.model_limits
        missing = [
            m for m in (*self.gemini_models, *self.gemini_moderation_models_list) if m not in limits
        ]
        if missing:
            raise ValueError(
                f"modelos sem limites conhecidos: {missing}. Informe em GEMINI_MODEL_LIMITS "
                "(modelo=rpm/rpd[/tpm]) os limites do AI Studio."
            )
        shared = set(self.gemini_models) & set(self.gemini_moderation_models_list)
        if shared:
            raise ValueError(
                "GEMINI_MODEL_FALLBACKS e GEMINI_MODERATION_MODELS não podem dividir modelos "
                f"(as cotas são por modelo): {sorted(shared)}"
            )
        return self

    @model_validator(mode="after")
    def _banner_window_is_valid(self) -> "Settings":
        if self.tse_banner_fim <= self.tse_banner_inicio:
            raise ValueError("TSE_BANNER_FIM precisa ser depois de TSE_BANNER_INICIO")
        return self

    @property
    def tse_scope_list(self) -> tuple[str, ...]:
        return tuple(self.tse_scopes.split(","))

    @property
    def open_platforms(self) -> tuple[str, ...]:
        """OPEN_PLATFORM_DOMAINS em minúsculas, sem espaços nem itens vazios."""
        items = (d.strip().lower().strip(".") for d in self.open_platform_domains.split(","))
        return tuple(dict.fromkeys(d for d in items if d))

    @property
    def gemini_models(self) -> tuple[str, ...]:
        """A cadeia de GEMINI_MODEL_FALLBACKS, em ordem e sem repetição."""
        return tuple(self.gemini_model_fallbacks.split(","))

    @property
    def model_limits(self) -> dict[str, ModelLimits]:
        """Tabela do nível gratuito com GEMINI_MODEL_LIMITS por cima."""
        return parse_model_limits(self.gemini_model_limits)

    @property
    def gemini_moderation_models_list(self) -> tuple[str, ...]:
        """A cadeia de GEMINI_MODERATION_MODELS, em ordem e sem repetição."""
        return tuple(self.gemini_moderation_models.split(","))

    @field_validator("database_ssl_root_cert")
    @classmethod
    def _root_cert_must_exist(cls, value: str | None) -> str | None:
        # Falha na INICIALIZAÇÃO (revisão do Cloud Run não sobe, erro claro no log) em vez de
        # na primeira conexão ao banco com um erro de SSL difícil de entender.
        if value is None:
            return None
        path = resolve_backend_path(value)
        if not path.is_file():
            raise ValueError(
                f"DATABASE_SSL_ROOT_CERT aponta para um arquivo que não existe: {value}"
            )
        if "BEGIN CERTIFICATE" not in path.read_text(encoding="utf-8", errors="replace"):
            raise ValueError(f"DATABASE_SSL_ROOT_CERT não é um certificado PEM: {value}")
        return str(path)

    @model_validator(mode="after")
    def _production_verifies_the_database_certificate(self) -> "Settings":
        # Sem o CA, o asyncpg usa ssl="require": criptografa, mas não valida o certificado
        # (um intermediário leria a senha do banco). Em produção, o certificado é obrigatório.
        if self.app_env == "production" and self.database_url and not self.database_ssl_root_cert:
            raise ValueError(
                "Em produção, defina DATABASE_SSL_ROOT_CERT (certificado do Supabase em "
                "backend/certs/supabase-ca.crt; ver docs/DEPLOY.md)"
            )
        return self

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
