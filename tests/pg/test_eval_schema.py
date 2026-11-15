"""The `eval` schema: who may touch the evaluator's results, and what a row must hold
(0004, 0008).

ff_eval writes, ff_viewer reads, and no other role has any privilege on these tables.
"""

from __future__ import annotations

from typing import LiteralString

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.pg

Connection = psycopg.Connection[TupleRow]
TABLES = ["run_result", "result"]
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
ROLES = ["ff_sim", "ff_gw", "ff_harness", "ff_agent", "ff_eval", "ff_viewer"]
EXPECTED: dict[str, set[str]] = {
    "ff_eval": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "ff_viewer": {"SELECT"},
}
"""Privileges on every eval table, by role; every role not listed has none (0008)."""

RUN_RESULT: LiteralString = (
    "INSERT INTO eval.run_result (run_id, verdict, evaluated, metrics, result)"
    " VALUES ('RUN-1', 'pass', '{1, 5}', '{}', '{}')"
)
RESULT: LiteralString = (
    "INSERT INTO eval.result (run_id, property, name, verdict, reason_codes, metrics, failures)"
    " VALUES ('RUN-1', 1, 'exactly_once', 'pass', '{}', '{}', '[]')"
)


def _connect(dsn: str, role: str) -> Connection:
    return psycopg.connect(make_conninfo(dsn, user=role), autocommit=True)


@pytest.mark.parametrize("role", ROLES)
def test_privileges_on_every_eval_table(migrated_database: str, role: str) -> None:
    with psycopg.connect(migrated_database) as conn:
        for table in TABLES:
            granted = {
                privilege
                for privilege in PRIVILEGES
                if conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)", (role, f"eval.{table}", privilege)
                ).fetchone()
                == (True,)
            }
            assert granted == EXPECTED.get(role, set()), (role, table)


def test_the_evaluator_writes_and_the_viewer_only_reads(migrated_database: str) -> None:
    with _connect(migrated_database, "ff_eval") as conn:
        conn.execute(RUN_RESULT)
        conn.execute(RESULT)
        conn.execute("UPDATE eval.result SET metrics = %s", (Jsonb({"duplicates": 0}),))
    with _connect(migrated_database, "ff_viewer") as conn:
        assert conn.execute("SELECT verdict FROM eval.run_result").fetchall() == [("pass",)]
        assert conn.execute("SELECT metrics FROM eval.result").fetchall() == [({"duplicates": 0},)]
        for query in (
            "DELETE FROM eval.result",
            "UPDATE eval.run_result SET verdict = 'fail'",
            "INSERT INTO eval.run_result (run_id, verdict, evaluated, metrics, result)"
            " VALUES ('RUN-2', 'pass', '{1}', '{}', '{}')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query)
    with _connect(migrated_database, "ff_eval") as conn:
        conn.execute("DELETE FROM eval.run_result")
        assert conn.execute("SELECT count(*) FROM eval.result").fetchone() == (0,), "cascade"


@pytest.mark.parametrize("role", ["ff_sim", "ff_gw", "ff_harness", "ff_agent"])
def test_no_other_role_can_read_or_write_results(migrated_database: str, role: str) -> None:
    with _connect(migrated_database, role) as conn:
        for query in ("SELECT 1 FROM eval.run_result", "SELECT 1 FROM eval.result", RUN_RESULT):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query)


@pytest.mark.parametrize(
    "query",
    [
        # A failing property names a reason; a passing one names none.
        "INSERT INTO eval.result (run_id, property, name, verdict, reason_codes, metrics,"
        " failures) VALUES ('RUN-1', 5, 'truthfully_reported', 'fail', '{}', '{}', '[]')",
        "INSERT INTO eval.result (run_id, property, name, verdict, reason_codes, metrics,"
        " failures) VALUES ('RUN-1', 1, 'exactly_once', 'pass', '{duplicate_effects}', '{}',"
        " '[]')",
        "INSERT INTO eval.result (run_id, property, name, verdict, reason_codes, metrics,"
        " failures) VALUES ('RUN-1', 6, 'sixth', 'pass', '{}', '{}', '[]')",
        "INSERT INTO eval.result (run_id, property, name, verdict, reason_codes, metrics,"
        " failures) VALUES ('RUN-1', 2, 'authorized_at_commit', 'skipped', '{}', '{}', '[]')",
        "INSERT INTO eval.run_result (run_id, verdict, evaluated, metrics, result)"
        " VALUES ('RUN-2', 'unresolved', '{1}', '{}', '{}')",
        "INSERT INTO eval.run_result (run_id, verdict, evaluated, metrics, result)"
        " VALUES ('RUN-2', 'pass', '{}', '{}', '{}')",
        "INSERT INTO eval.run_result (run_id, verdict, evaluated, metrics, result)"
        " VALUES ('RUN-2', 'pass', '{0, 1}', '{}', '{}')",
    ],
)
def test_a_result_row_must_be_consistent(migrated_database: str, query: LiteralString) -> None:
    with _connect(migrated_database, "ff_eval") as conn:
        conn.execute(RUN_RESULT)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(query)


def test_a_property_row_belongs_to_a_run_row(migrated_database: str) -> None:
    with (
        _connect(migrated_database, "ff_eval") as conn,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        conn.execute(RESULT)
