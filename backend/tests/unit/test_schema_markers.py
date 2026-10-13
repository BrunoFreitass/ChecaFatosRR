"""Toda migration precisa de uma marca no /health/ready: sem ela, um banco sem a migration
aplicada passaria como pronto (incidente de 13/10/2026)."""

from pathlib import Path

from app.routers.health import SCHEMA_MARKERS

MIGRATIONS = Path(__file__).resolve().parents[3] / "supabase" / "migrations"


def test_every_migration_has_a_schema_marker() -> None:
    names = {path.stem for path in MIGRATIONS.glob("*.sql")}
    assert names, "nenhuma migration encontrada"
    assert names == set(SCHEMA_MARKERS)


def test_markers_are_well_formed() -> None:
    for name, (kind, obj, column) in SCHEMA_MARKERS.items():
        assert kind in {"table", "index", "column", "function"}, name
        assert obj.startswith("public."), name
        assert (column is not None) == (kind == "column"), name
