"""Migration file naming and order (0007)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from fivefold.harness.migrations import (
    Connection,
    MigrationError,
    check_database_name,
    discover,
    drop_database,
    migrate,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"


def _write(directory: Path, *names: str) -> None:
    for name in names:
        (directory / name).write_text("SELECT 1;\n", encoding="utf-8")


def test_repository_migrations_are_well_formed() -> None:
    migrations = discover(MIGRATIONS)
    assert migrations, "expected at least one migration"
    assert migrations[0].name == "0001_schemas.sql"


def test_the_run_registry_s_migrations_are_well_formed() -> None:
    migrations = discover(MIGRATIONS.parent / "registry")
    assert [migration.name for migration in migrations][:1] == ["0001_registry.sql"]


def test_migrations_refuse_postgres_15_before_any_query() -> None:
    connection = cast(
        Connection,
        SimpleNamespace(autocommit=True, info=SimpleNamespace(server_version=150012)),
    )
    with pytest.raises(MigrationError, match="PostgreSQL 16 or newer"):
        migrate(connection, MIGRATIONS)


@pytest.mark.parametrize(
    "names",
    [
        ("0001_first.sql", "0003_gap.sql"),
        ("0001_first.sql", "0001_duplicate.sql"),
        ("0002_starts_late.sql",),
    ],
)
def test_numbers_must_run_from_one_without_gaps(tmp_path: Path, names: tuple[str, ...]) -> None:
    _write(tmp_path, *names)
    with pytest.raises(MigrationError, match="no gaps"):
        discover(tmp_path)


@pytest.mark.parametrize(
    "name",
    ["1_short.sql", "0001-dash.sql", "0001_Upper.sql", "0001_.sql", "0001_trailing_.sql"],
)
def test_bad_names_are_rejected(tmp_path: Path, name: str) -> None:
    _write(tmp_path, name)
    with pytest.raises(MigrationError, match="bad migration file name"):
        discover(tmp_path)


def test_a_missing_directory_is_an_error(tmp_path: Path) -> None:
    # A mistyped path must not look like a database with nothing to apply.
    with pytest.raises(MigrationError, match="not found"):
        discover(tmp_path / "no-such-directory")


def test_a_file_is_not_a_migration_directory(tmp_path: Path) -> None:
    path = tmp_path / "0001_schemas.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="not found"):
        discover(path)


def test_other_files_are_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "0001_only.sql")
    (tmp_path / "README.md").write_text("notes\n", encoding="utf-8")
    assert [migration.name for migration in discover(tmp_path)] == ["0001_only.sql"]


def test_checksum_follows_content(tmp_path: Path) -> None:
    path = tmp_path / "0001_only.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    before = discover(tmp_path)[0].sha256
    path.write_text("SELECT 2;\n", encoding="utf-8")
    assert discover(tmp_path)[0].sha256 != before


@pytest.mark.parametrize("name", ["fivefold_dev", "_x", "run_0001", "a" * 63])
def test_database_names_the_runner_accepts(name: str) -> None:
    check_database_name(name)


@pytest.mark.parametrize(
    "name", ["a" * 64, "Fivefold", "fivefold-dev", "1abc", "", "fivefold_dev\n", "f\u00efvefold"]
)
def test_database_names_the_runner_refuses(name: str) -> None:
    with pytest.raises(MigrationError):
        check_database_name(name)


def test_an_overlong_name_is_refused_before_any_connection() -> None:
    with pytest.raises(MigrationError, match="at most 63 bytes"):
        drop_database("postgresql://nobody@invalid.invalid/postgres", "a" * 64)
