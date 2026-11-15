"""The run registry (0009, 0012): one row per run database, written by the harness.

The registry is a database of its own. Role `ff_registry` writes it, `ff_viewer` reads it,
and no other component role can touch it. Every run writes its database's row when it
records itself, whoever made the database. A database made again under the same name
gets its row replaced, and `forget` removes the row of a dropped one.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.migrations import (
    discover,
    drop_database,
    prepare_registry,
    registry_database,
)
from fivefold.harness.registry import RegistryEntry, RegistryError, forget, register
from fivefold.harness.run import InprocRun, RunSpec

pytestmark = pytest.mark.pg

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_DIR = ROOT / "db" / "registry"
COMPONENT_ROLES = ("ff_sim", "ff_gw", "ff_harness", "ff_agent", "ff_eval")
WRITES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})


def entry(database: str, run_id: str = "dev-001-single-po.G2.C2.7") -> RegistryEntry:
    return RegistryEntry(
        database=database,
        run_id=run_id,
        scenario_id="dev-001-single-po",
        config="G2",
        contract="C2",
        seed=7,
    )


def rows(admin_dsn: str, registry: str | None = None) -> list[tuple[Any, ...]]:
    name = registry_database() if registry is None else registry
    with psycopg.connect(make_conninfo(admin_dsn, dbname=name)) as conn:
        return conn.execute(
            "SELECT database_name, run_id, scenario_id, config, contract, seed"
            " FROM registry.run ORDER BY database_name"
        ).fetchall()


def test_the_tests_register_in_a_registry_of_their_own(db_prefix: str, admin_dsn: str) -> None:
    assert registry_database() == f"{db_prefix}_registry"


def test_preparing_creates_the_registry_once_and_keeps_its_rows(
    admin_dsn: str, db_prefix: str
) -> None:
    name = f"{db_prefix}_registry_prepared"
    drop_database(admin_dsn, name)
    try:
        dsn = prepare_registry(admin_dsn, REGISTRY_DIR, name)
        register(admin_dsn, entry("run_one"), registry=name)
        assert prepare_registry(admin_dsn, REGISTRY_DIR, name) == dsn
        assert rows(admin_dsn, name) == [
            ("run_one", "dev-001-single-po.G2.C2.7", "dev-001-single-po", "G2", "C2", 7)
        ]
        with psycopg.connect(dsn) as conn:
            applied = conn.execute(
                "SELECT filename FROM public.schema_migration ORDER BY filename"
            ).fetchall()
        assert [row[0] for row in applied] == [m.name for m in discover(REGISTRY_DIR)]
    finally:
        drop_database(admin_dsn, name)


def test_ff_registry_writes_the_registry_ff_viewer_reads_it_and_no_one_else_can(
    admin_dsn: str,
) -> None:
    with psycopg.connect(make_conninfo(admin_dsn, dbname=registry_database())) as conn:
        conn.execute("CREATE TABLE registry.privilege_probe (id integer)")
        for table in ("registry.run", "registry.privilege_probe"):
            for role in (*COMPONENT_ROLES, "ff_viewer", "ff_registry"):
                granted = {
                    privilege
                    for privilege in sorted(WRITES)
                    if conn.execute(
                        "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
                    ).fetchone()
                    == (True,)
                }
                expected = {"ff_registry": WRITES, "ff_viewer": {"SELECT"}}.get(role, set())
                assert granted == expected, f"{role} on {table}"
        for role in (*COMPONENT_ROLES, "ff_viewer", "ff_registry"):
            usage, create = conn.execute(
                "SELECT has_schema_privilege(%s, 'registry', 'USAGE'),"
                " has_schema_privilege(%s, 'registry', 'CREATE')",
                (role, role),
            ).fetchone() or (None, None)
            assert usage == (role in ("ff_viewer", "ff_registry")), f"USAGE for {role}"
            assert create is False, f"CREATE for {role}"
        conn.rollback()


def test_the_viewer_s_role_cannot_write_the_registry(admin_dsn: str) -> None:
    viewer = make_conninfo(admin_dsn, dbname=registry_database(), user="ff_viewer")
    with psycopg.connect(viewer) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM registry.run")


def test_a_database_made_again_gets_its_row_replaced_and_a_dropped_one_leaves(
    admin_dsn: str, db_prefix: str
) -> None:
    database = f"{db_prefix}_registry_probe"
    register(admin_dsn, entry(database))
    register(admin_dsn, entry(database, run_id="dev-001-single-po.B0.C2.7"))
    assert [row[:2] for row in rows(admin_dsn) if row[0] == database] == [
        (database, "dev-001-single-po.B0.C2.7")
    ]
    forget(admin_dsn, database)
    assert [row for row in rows(admin_dsn) if row[0] == database] == []


def test_a_registry_that_was_never_created_is_named_with_the_command_that_creates_it(
    admin_dsn: str, db_prefix: str
) -> None:
    missing = f"{db_prefix}_registry_missing"
    drop_database(admin_dsn, missing)
    with pytest.raises(RegistryError, match=re.escape("fivefold.harness.migrations bootstrap")):
        register(admin_dsn, entry("run_one"), registry=missing)


def test_a_run_registers_its_database(admin_dsn: str, run_database: Callable[[str], str]) -> None:
    dsn = run_database("registered")
    database = conninfo_to_dict(dsn)["dbname"]
    scenario = load_scenario_file(ROOT / "scenarios" / "dev" / "dev-001-single-po.yaml")
    asyncio.run(InprocRun(RunSpec(scenario, "B0", Contract.C1, 11, plan=[]), dsn).execute())
    assert [row for row in rows(admin_dsn) if row[0] == database] == [
        (database, "dev-001-single-po.B0.C1.11", "dev-001-single-po", "B0", "C1", 11)
    ]
