"""Conexão async com o Postgres do Supabase via Supavisor (transaction mode).

No transaction mode cada transação pode cair numa conexão física diferente, então:
- pool pequeno no processo (DB_POOL_SIZE; 0 = NullPool): reaproveita só a conexão
  cliente → Supavisor, que continua distribuindo as transações;
- caches de prepared statements desligados (asyncpg e SQLAlchemy);
- nomes únicos de prepared statement para evitar "prepared statement already exists".
"""

import ssl
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import Settings, get_settings


class DatabaseNotConfiguredError(RuntimeError):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


# Uma conexão ociosa no pool é refeita depois disso (o Supavisor também derruba as ociosas).
POOL_RECYCLE_SECONDS = 300


def engine_options(settings: Settings) -> dict[str, Any]:
    """Argumentos do create_async_engine (função pura: testável sem banco)."""
    connect_args: dict[str, object] = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
        "timeout": 10,  # conexão
        "command_timeout": 15,  # cada comando SQL
    }
    if settings.database_ssl_root_cert:
        # verify-full: valida a cadeia com o CA do Supabase (Database Settings > SSL).
        connect_args["ssl"] = ssl.create_default_context(cafile=settings.database_ssl_root_cert)
    elif settings.database_ssl:
        # Criptografa sem validar o certificado (só fora de produção: lá o CA é obrigatório).
        connect_args["ssl"] = "require"
    options: dict[str, Any] = {
        "connect_args": connect_args,
        # Erros de SQL não levam os parâmetros (apelidos, ids) para os logs.
        "hide_parameters": True,
    }
    if settings.db_pool_size == 0:
        options["poolclass"] = NullPool
    else:
        options |= {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_pool_max_overflow,
            # Pool cheio: 503 rápido em vez de enfileirar 30 s (o padrão do SQLAlchemy).
            "pool_timeout": settings.db_pool_timeout_seconds,
            # 1 viagem para conferir a conexão, contra ~5 (TCP+TLS+auth) de uma nova.
            "pool_pre_ping": True,
            "pool_recycle": POOL_RECYCLE_SECONDS,
            # Reusa a mais recente: as ociosas ficam no fim da fila e expiram.
            "pool_use_lifo": True,
        }
    return options


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        if not settings.database_url:
            raise DatabaseNotConfiguredError("DATABASE_URL não definida")
        _engine = create_async_engine(settings.database_url, **engine_options(settings))
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependência do FastAPI: uma sessão por requisição.

    Convenção: escritas sempre dentro de `async with session.begin():` (commit/rollback
    explícitos). Sem isso, o que não for commitado é descartado ao fechar a sessão.
    """
    get_engine()
    if _sessionmaker is None:
        raise DatabaseNotConfiguredError("sessionmaker não inicializado")
    async with _sessionmaker() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
