"""The bootstrap script and the migration runner against a real PostgreSQL server (0007)."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from fivefold.harness.migrations import (
    MigrationError,
    bootstrap,
    discover,
    drop_database,
    migrate,
    recreate_database,
)

pytestmark = pytest.mark.pg

DB_DIR = Path(__file__).resolve().parents[2] / "db"
ROLES = ["ff_agent", "ff_eval", "ff_gw", "ff_harness", "ff_sim", "ff_viewer"]


def test_bootstrap_creates_the_roles_and_can_run_again(admin_dsn: str, empty_database: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        bootstrap(conn, DB_DIR / "bootstrap.sql")
        bootstrap(conn, DB_DIR / "bootstrap.sql")
    # Roles are server-wide; read them through a UTF-8 database so text comes back as str.
    with psycopg.connect(empty_database) as conn:
        rows = conn.execute(
            "SELECT rolname FROM pg_catalog.pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
            (ROLES,),
        ).fetchall()
    assert [row[0] for row in rows] == ROLES


def test_applies_every_migration_and_records_checksums(empty_database: str) -> None:
    expected = discover(DB_DIR / "migrations")
    with psycopg.connect(empty_database, autocommit=True) as conn:
        applied = migrate(conn, DB_DIR / "migrations")
        rows = conn.execute(
            "SELECT filename, sha256 FROM public.schema_migration ORDER BY filename"
        ).fetchall()
    assert applied == [migration.name for migration in expected]
    assert rows == [(migration.name, migration.sha256) for migration in expected]


def test_a_second_run_applies_nothing(empty_database: str) -> None:
    with psycopg.connect(empty_database, autocommit=True) as conn:
        migrate(conn, DB_DIR / "migrations")
        assert migrate(conn, DB_DIR / "migrations") == []


def test_refuses_a_migration_changed_after_it_was_applied(
    empty_database: str, tmp_path: Path
) -> None:
    migrations = tmp_path / "migrations"
    shutil.copytree(DB_DIR / "migrations", migrations)
    with psycopg.connect(empty_database, autocommit=True) as conn:
        migrate(conn, migrations)
        first = sorted(migrations.glob("*.sql"))[0]
        first.write_text(first.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
        with pytest.raises(MigrationError, match="changed after it was applied"):
            migrate(conn, migrations)


def test_refuses_when_an_applied_migration_is_missing(empty_database: str, tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    shutil.copytree(DB_DIR / "migrations", migrations)
    with psycopg.connect(empty_database, autocommit=True) as conn:
        migrate(conn, migrations)
        for path in migrations.glob("*.sql"):
            path.unlink()
        with pytest.raises(MigrationError, match="missing"):
            migrate(conn, migrations)


def test_refuses_a_database_that_is_not_utf8(admin_dsn: str) -> None:
    name = "fivefold_test_ascii"
    drop_database(admin_dsn, name)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "CREATE DATABASE {} ENCODING 'SQL_ASCII' LC_COLLATE 'C' LC_CTYPE 'C'"
                " TEMPLATE template0"
            ).format(sql.Identifier(name))
        )
    target = make_conninfo(admin_dsn, dbname=name)
    try:
        with (
            psycopg.connect(target, autocommit=True) as conn,
            pytest.raises(MigrationError, match="UTF8"),
        ):
            migrate(conn, DB_DIR / "migrations")
    finally:
        drop_database(admin_dsn, name)


def test_runners_started_together_apply_each_migration_once(admin_dsn: str) -> None:
    """Runners started at the same moment on a fresh database must not collide.

    The runner once created its tracking table before taking the advisory lock, and
    PostgreSQL could reject one runner's CREATE TABLE IF NOT EXISTS with a duplicate key.
    A race shows up only some of the time, so the test runs several rounds, each releasing
    all runners at once on a new database.
    """
    rounds, runners = 10, 8
    expected = sorted(migration.name for migration in discover(DB_DIR / "migrations"))
    name = "fivefold_test_race"
    try:
        for _ in range(rounds):
            applied, errors = _migrate_together(recreate_database(admin_dsn, name), runners)
            assert errors == []
            assert sorted(applied) == expected
    finally:
        drop_database(admin_dsn, name)


def _migrate_together(dsn: str, runners: int) -> tuple[list[str], list[Exception]]:
    """Release `runners` migration runners on `dsn` at the same moment.

    Returns every migration name any runner applied, and every error raised.
    """
    start = threading.Barrier(runners)
    guard = threading.Lock()
    applied: list[str] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                start.wait()
                names = migrate(conn, DB_DIR / "migrations")
        except Exception as error:
            with guard:
                errors.append(error)
        else:
            with guard:
                applied.extend(names)

    threads = [threading.Thread(target=run) for _ in range(runners)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return applied, errors


def test_run_databases_use_utf8_and_c_collation(empty_database: str) -> None:
    with psycopg.connect(empty_database) as conn:
        row = conn.execute(
            "SELECT pg_encoding_to_char(encoding), datcollate, datctype"
            " FROM pg_catalog.pg_database WHERE datname = current_database()"
        ).fetchone()
    assert row == ("UTF8", "C", "C")


def test_a_failing_migration_leaves_no_trace(empty_database: str, tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    shutil.copytree(DB_DIR / "migrations", migrations)
    broken = f"{len(discover(migrations)) + 1:04d}_broken.sql"
    (migrations / broken).write_text(
        "CREATE TABLE sim.partial (id integer);\nSELECT * FROM no_such_table;\n",
        encoding="utf-8",
    )
    with psycopg.connect(empty_database, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UndefinedTable):
            migrate(conn, migrations)
        recorded = conn.execute("SELECT filename FROM public.schema_migration").fetchall()
        partial = conn.execute("SELECT to_regclass('sim.partial')").fetchone()
    assert broken not in {row[0] for row in recorded}
    assert partial == (None,)
