"""Conexão async com o Postgres do Supabase via Supavisor (transaction mode).

No transaction mode cada transação pode cair numa conexão física diferente, então:
- NullPool: quem faz o pooling é o Supavisor;
- caches de prepared statements desligados (asyncpg e SQLAlchemy);
- nomes únicos de prepared statement para evitar "prepared statement already exists".
"""

import ssl
from collections.abc import AsyncIterator
from uuid import uuid4

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings


class DatabaseNotConfiguredError(RuntimeError):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        if not settings.database_url:
            raise DatabaseNotConfiguredError("DATABASE_URL não definida")
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
            # Criptografa sem validar o certificado. Aceitável até a etapa 9 (ver DEPLOY.md).
            connect_args["ssl"] = "require"
        _engine = create_async_engine(
            settings.database_url, poolclass=NullPool, connect_args=connect_args
        )
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
