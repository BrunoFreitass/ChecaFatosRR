"""Conexão async com o Postgres do Supabase via Supavisor (transaction mode).

No transaction mode cada transação pode cair numa conexão física diferente, então:
- pool pequeno no processo (DB_POOL_SIZE; 0 = NullPool): reaproveita só a conexão
  cliente → Supavisor, que continua distribuindo as transações;
- caches de prepared statements desligados (asyncpg e SQLAlchemy);
- nomes únicos de prepared statement para evitar "prepared statement already exists".
"""

import logging
import ssl
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.util.concurrency import await_

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


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


def ping_one_round_trip(dbapi_connection: Any) -> bool:
    """pre_ping de 1 viagem. O do SQLAlchemy+asyncpg abre transação, prepara, executa e faz
    rollback: ~4,5 viagens (~450 ms entre Render/Virginia e Supabase/São Paulo, medido em
    13/10/2026). Uma consulta no protocolo simples é uma mensagem só, então também é segura no
    transaction mode do Supavisor (o motivo da transação no ping original, SQLAlchemy #10226).
    Usa os mesmos internos do adaptador que o ping original (SQLAlchemy travado em requirements)."""
    try:
        await_(dbapi_connection._connection.execute("select 1"))
    except Exception as error:
        # Traduz para o erro do DBAPI: o pool reconhece a desconexão e abre outra conexão.
        dbapi_connection._handle_exception(error)
    return True


# Conexão física nova (TCP + TLS + autenticação no Supavisor): deve ser rara com o pool. O log
# mostra em produção se as conexões estão sendo reaproveitadas e quanto custa abrir uma.
_connect_started_at: ContextVar[float | None] = ContextVar("db_connect_started_at", default=None)


def _connect_started(*_: Any) -> None:
    _connect_started_at.set(time.monotonic())


def _connect_finished(*_: Any) -> None:
    started = _connect_started_at.get()
    elapsed = int((time.monotonic() - started) * 1000) if started is not None else None
    logger.info("db_new_connection", extra={"connect_ms": elapsed})


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        if not settings.database_url:
            raise DatabaseNotConfiguredError("DATABASE_URL não definida")
        _engine = create_async_engine(settings.database_url, **engine_options(settings))
        _engine.sync_engine.dialect.do_ping = ping_one_round_trip  # type: ignore[method-assign]
        event.listen(_engine.sync_engine, "do_connect", _connect_started)
        event.listen(_engine.sync_engine, "connect", _connect_finished)
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
