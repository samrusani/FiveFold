"""Migration file naming and order (0007)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fivefold.harness.migrations import MigrationError, discover

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"


def _write(directory: Path, *names: str) -> None:
    for name in names:
        (directory / name).write_text("SELECT 1;\n", encoding="utf-8")


def test_repository_migrations_are_well_formed() -> None:
    migrations = discover(MIGRATIONS)
    assert migrations, "expected at least one migration"
    assert migrations[0].name == "0001_schemas.sql"


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
