"""Database privileges match decision record 0008.

Each component connects with its own role. The gateway and the simulator cannot touch each
other's tables, so they can never share a transaction.
"""

from __future__ import annotations

from typing import LiteralString

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import TupleRow

pytestmark = pytest.mark.pg

SCHEMAS = ("sim", "gw", "harness", "agent", "eval")
WRITES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
READS = frozenset({"SELECT"})
NONE: frozenset[str] = frozenset()

EXPECTED: dict[str, dict[str, frozenset[str]]] = {
    "ff_sim": {"sim": WRITES, "gw": NONE, "harness": NONE, "agent": NONE, "eval": NONE},
    "ff_gw": {"sim": NONE, "gw": WRITES, "harness": NONE, "agent": NONE, "eval": NONE},
    "ff_harness": {"sim": READS, "gw": NONE, "harness": WRITES, "agent": NONE, "eval": NONE},
    "ff_agent": {"sim": NONE, "gw": NONE, "harness": NONE, "agent": WRITES, "eval": NONE},
    "ff_eval": {"sim": READS, "gw": READS, "harness": READS, "agent": NONE, "eval": WRITES},
    "ff_viewer": {"sim": READS, "gw": READS, "harness": READS, "agent": NONE, "eval": READS},
    "ff_registry": {"sim": NONE, "gw": NONE, "harness": NONE, "agent": NONE, "eval": NONE},
}
"""The privilege table in 0008, by role and schema, and `ff_registry`, which writes the run
registry and has no privilege in a run database (0012)."""


def _privilege(conn: psycopg.Connection[TupleRow], query: LiteralString, *args: str) -> bool:
    row = conn.execute(query, args).fetchone()
    assert row is not None
    return bool(row[0])


@pytest.mark.parametrize("schema", SCHEMAS)
def test_new_tables_get_the_privileges_in_0008(migrated_database: str, schema: str) -> None:
    probe = f"{schema}.privilege_probe"
    with psycopg.connect(migrated_database) as conn:
        conn.execute(
            sql.SQL("CREATE TABLE {}.privilege_probe (id integer)").format(sql.Identifier(schema))
        )
        for role, row in EXPECTED.items():
            granted = {
                privilege
                for privilege in sorted(WRITES)
                if _privilege(
                    conn, "SELECT has_table_privilege(%s, %s, %s)", role, probe, privilege
                )
            }
            assert granted == row[schema], f"{role} on {schema}"
        conn.rollback()


def test_schema_usage_and_create_match_0008(migrated_database: str) -> None:
    with psycopg.connect(migrated_database) as conn:
        for role, row in EXPECTED.items():
            for schema in SCHEMAS:
                usage = _privilege(
                    conn, "SELECT has_schema_privilege(%s, %s, 'USAGE')", role, schema
                )
                create = _privilege(
                    conn, "SELECT has_schema_privilege(%s, %s, 'CREATE')", role, schema
                )
                assert usage == bool(row[schema]), f"USAGE for {role} on {schema}"
                only_agent_creates = (role, schema) == ("ff_agent", "agent")
                assert create == only_agent_creates, f"CREATE for {role} on {schema}"


def test_agent_role_searches_its_own_schema(empty_database: str) -> None:
    # Role settings are server-wide; read them through a UTF-8 database.
    with psycopg.connect(empty_database) as conn:
        row = conn.execute(
            "SELECT rolconfig FROM pg_catalog.pg_roles WHERE rolname = 'ff_agent'"
        ).fetchone()
    assert row is not None
    assert "search_path=agent" in (row[0] or [])
