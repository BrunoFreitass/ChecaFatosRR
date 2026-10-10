"""Pool de conexões (deploy no Render: processo persistente, Virginia → Supabase SP).

Com NullPool cada requisição abriria TCP + TLS (verify-full) + autenticação no Supavisor:
~5 viagens de ~120 ms. Um pool pequeno reaproveita a conexão cliente → Supavisor.
"""

import pytest
from pydantic import ValidationError
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db import engine_options

URL = "postgresql://u:p@db.example.com:6543/postgres"


def test_default_is_a_small_pool_that_fails_fast() -> None:
    options = engine_options(Settings(database_url=URL, database_ssl=False))
    assert options["pool_size"] == 3
    assert options["max_overflow"] == 2  # teto: 5 conexões
    assert options["pool_timeout"] == 8  # 503 rápido em vez de enfileirar 30 s
    assert options["pool_pre_ping"] is True  # 1 viagem, contra ~5 de conexão nova
    assert options["pool_recycle"] == 300
    assert options["pool_use_lifo"] is True  # ociosas sobram no fim e expiram
    assert "poolclass" not in options


def test_pool_size_zero_keeps_null_pool() -> None:
    # Testes e plano B (Cloud Run): sem pool no processo, quem agrupa é o Supavisor.
    options = engine_options(Settings(database_url=URL, database_ssl=False, db_pool_size=0))
    assert options["poolclass"] is NullPool
    assert "pool_size" not in options


def test_pool_stays_compatible_with_transaction_mode() -> None:
    connect_args = engine_options(Settings(database_url=URL, database_ssl=False))["connect_args"]
    assert connect_args["statement_cache_size"] == 0
    assert connect_args["prepared_statement_cache_size"] == 0
    first = connect_args["prepared_statement_name_func"]()
    assert first != connect_args["prepared_statement_name_func"]()  # nomes únicos


def test_pool_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "4")
    monkeypatch.setenv("DB_POOL_MAX_OVERFLOW", "1")
    options = engine_options(Settings(database_url=URL, database_ssl=False))
    assert (options["pool_size"], options["max_overflow"]) == (4, 1)


@pytest.mark.parametrize("field", ["db_pool_size", "db_pool_max_overflow"])
def test_pool_has_a_ceiling(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: 50})


def test_extract_concurrency_sizes_the_parser_semaphore(monkeypatch: pytest.MonkeyPatch) -> None:
    # Render Free (0,1 CPU): um parser de HTML por vez.
    from app.config import get_settings
    from app.services import extractor

    monkeypatch.setenv("EXTRACT_CONCURRENCY", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(extractor, "_parsers", None)
    try:
        slots = extractor._parser_slots()
        assert slots._value == 1
        assert extractor._parser_slots() is slots  # um semáforo por processo
    finally:
        get_settings.cache_clear()
