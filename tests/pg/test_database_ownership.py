"""Only owned FiveFold databases can be adopted, migrated or reset."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from fivefold.harness.migrations import (
    REGISTRY_ENV,
    MigrationError,
    create_database,
    create_run_database,
    discover,
    drop_database,
    main,
    migrate,
    prepare_registry,
    prepare_template,
)
from fivefold.paths import DB_DIR

pytestmark = pytest.mark.pg


@pytest.fixture
def named_database(admin_dsn: str, db_prefix: str) -> Iterator[str]:
    name = f"{db_prefix}_legacy_owned"
    yield name
    drop_database(admin_dsn, name)


def catalog(admin_dsn: str, name: str) -> tuple[Any, ...] | None:
    with psycopg.connect(admin_dsn) as conn:
        return conn.execute(
            "SELECT oid, pg_catalog.shobj_description(oid, 'pg_database'), datdba"
            " FROM pg_catalog.pg_database WHERE datname = %s",
            (name,),
        ).fetchone()


def prefix(source: Path, target: Path, count: int) -> Path:
    target.mkdir()
    for file in discover(source)[:count]:
        shutil.copyfile(file.path, target / file.name)
    return target


def prepare(kind: str, admin: str, name: str) -> None:
    if kind == "registry":
        prepare_registry(admin, DB_DIR / "registry", name)
    else:
        prepare_template(admin, DB_DIR / "migrations", name)


@pytest.mark.parametrize(
    ("kind", "source", "count", "marker"),
    [
        ("template", "migrations", 17, "FiveFold template database"),
        ("registry", "registry", 2, "FiveFold registry database"),
        ("registry", "registry", 3, "FiveFold registry database"),
    ],
)
def test_legacy_prefix_is_adopted_and_migrated_in_place(
    admin_dsn: str,
    named_database: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    source: str,
    count: int,
    marker: str,
) -> None:
    name = named_database
    old = create_database(admin_dsn, name)
    files = discover(DB_DIR / source)
    with psycopg.connect(old.dsn, autocommit=True) as database:
        migrate(database, prefix(DB_DIR / source, tmp_path / "old", count))
    prepare(kind, admin_dsn, name)
    assert capsys.readouterr().out.splitlines() == [
        f"Adopted database {name} through {files[count - 1].name}."
    ]
    identity = catalog(admin_dsn, name)
    assert identity is not None and identity[:2] == (old.oid, marker)
    with psycopg.connect(old.dsn) as database:
        applied = database.execute(
            "SELECT filename, sha256 FROM public.schema_migration ORDER BY filename"
        ).fetchall()
    assert applied == [(file.name, file.sha256) for file in files]


@pytest.mark.parametrize("kind", ["template", "registry"])
@pytest.mark.parametrize("problem", ["stray_table", "checksum", "owner"])
def test_preparation_leaves_a_database_it_cannot_adopt_untouched(
    named_database: str,
    admin_dsn: str,
    kind: str,
    problem: str,
) -> None:
    name = named_database
    created = create_database(admin_dsn, name)
    directory = DB_DIR / ("registry" if kind == "registry" else "migrations")
    history: list[tuple[Any, ...]] = []
    with psycopg.connect(created.dsn, autocommit=True) as database:
        database.execute("CREATE TABLE public.sentinel (value integer)")
        database.execute("INSERT INTO public.sentinel VALUES (17)")
        if problem != "stray_table":
            migrate(database, directory)
            if problem == "checksum":
                database.execute("UPDATE public.schema_migration SET sha256 = 'changed'")
            if problem == "owner":
                database.execute(
                    sql.SQL("ALTER DATABASE {} OWNER TO ff_agent").format(sql.Identifier(name))
                )
            history = database.execute(
                "SELECT * FROM public.schema_migration ORDER BY filename"
            ).fetchall()
    before = catalog(admin_dsn, name)
    remedy = "rename the database" if kind == "registry" else "Use another database name"
    with pytest.raises(MigrationError, match=remedy):
        prepare(kind, admin_dsn, name)
    assert catalog(admin_dsn, name) == before
    with psycopg.connect(created.dsn) as database:
        assert database.execute("SELECT value FROM public.sentinel").fetchall() == [(17,)]
        if history:
            assert (
                database.execute(
                    "SELECT * FROM public.schema_migration ORDER BY filename"
                ).fetchall()
                == history
            )
        else:
            assert database.execute("SELECT to_regclass('public.schema_migration')").fetchone() == (
                None,
            )


@pytest.mark.parametrize("command", ["migrate", "reset"])
def test_cli_requires_ownership_and_accepts_an_old_development_prefix(
    named_database: str,
    admin_dsn: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    name = named_database
    database = create_database(admin_dsn, name)
    argv = [command, "--dsn", admin_dsn, "--database", name]
    assert main(argv) == 1
    assert "Use another database name" in capsys.readouterr().err
    assert catalog(admin_dsn, name) is not None
    with psycopg.connect(database.dsn, autocommit=True) as conn:
        migrate(conn, prefix(DB_DIR / "migrations", tmp_path / "old", 17))
    assert main(argv) == 0
    applied = discover(DB_DIR / "migrations")
    stdout = capsys.readouterr().out.splitlines()
    assert stdout[0] == f"Adopted database {name} through {applied[-2].name}."
    recorded = catalog(admin_dsn, name)
    assert recorded is not None and recorded[1] == "FiveFold development database"
    assert (recorded[0] == database.oid) == (command == "migrate")
    with psycopg.connect(database.dsn) as conn:
        assert conn.execute(
            "SELECT filename FROM public.schema_migration ORDER BY filename DESC LIMIT 1"
        ).fetchone() == (applied[-1].name,)


def test_a_marked_template_is_never_a_run_replacement(
    named_database: str,
    admin_dsn: str,
    db_prefix: str,
) -> None:
    name = named_database
    prepare_template(admin_dsn, DB_DIR / "migrations", name)
    before = catalog(admin_dsn, name)
    with pytest.raises(MigrationError, match="is not a run database"):
        create_run_database(
            admin_dsn,
            name,
            DB_DIR / "migrations",
            template=f"{db_prefix}_other_tpl",
            replace=True,
        )
    assert catalog(admin_dsn, name) == before


@pytest.mark.parametrize("recognition", ["name", "marker"])
def test_migrate_uses_the_registry_directory(
    recognition: str,
    named_database: str,
    admin_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = named_database
    registry = DB_DIR / "registry"
    legacy = prefix(registry, tmp_path / "registry", 2)
    if recognition == "name":
        database = create_database(admin_dsn, name)
        with psycopg.connect(database.dsn, autocommit=True) as conn:
            migrate(conn, legacy)
        monkeypatch.setenv(REGISTRY_ENV, name)
    else:
        prepare_registry(admin_dsn, legacy, name)
    assert main(["migrate", "--dsn", admin_dsn, "--database", name]) == 0
    identity = catalog(admin_dsn, name)
    assert identity is not None and identity[1] == "FiveFold registry database"
    with psycopg.connect(admin_dsn, dbname=name) as conn:
        applied = conn.execute(
            "SELECT filename FROM public.schema_migration ORDER BY filename"
        ).fetchall()
    assert applied == [(file.name,) for file in discover(registry)]
