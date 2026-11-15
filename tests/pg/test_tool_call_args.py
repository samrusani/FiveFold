"""`gw.tool_call.args`: each logged response keeps the arguments of the call it answers
(record 0003, data model; db/migrations/0014_tool_call_args.sql).

The harness's message log holds every tool call as the agent sent it, so a run's logged
responses can be checked against it: each one names a call the gateway received, with
the same tool and the same arguments.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from fivefold.harness.demo import demo_1_spec
from fivefold.harness.migrations import discover, migrate
from fivefold.harness.run import Config, InprocRun

pytestmark = pytest.mark.pg

DB_DIR = Path(__file__).resolve().parents[2] / "db"
READS = frozenset({"get_quote", "list_suppliers", "get_budget", "get_inventory"})
"""The read tools: their calls are recorded as reads, not as responses."""


def _canonical(tool: str, args: Any) -> str:
    return json.dumps([tool, args], sort_keys=True)


@pytest.mark.parametrize("config", ["G1", "G2"])
def test_each_logged_response_holds_the_arguments_its_call_arrived_with(
    run_database: Callable[[str], str], config: Config
) -> None:
    """Demo 1: the order, the order sent again once the killed agent resumes, a status
    call and the report. Each call the gateway answered with a tool result has its
    response logged, with the call's arguments: every call but the reads, and in G1 but
    the report, which G1 forwards and keeps no record of (0008). The order the agent sent
    before it was killed was answered with an error as its connection closed, so it has
    no logged response."""
    dsn = run_database(f"args_demo_1_{config.lower()}")
    asyncio.run(InprocRun(demo_1_spec(config, 7), dsn).execute())
    with psycopg.connect(dsn) as conn:
        logged = conn.execute("SELECT tool, args FROM gw.tool_call ORDER BY call_no").fetchall()
        answered = conn.execute(
            "SELECT q.body->'params'->>'name', q.body->'params'->'arguments'"
            " FROM harness.message_log q JOIN harness.message_log a ON a.correlation = q.seq"
            " WHERE q.hop = 'agent_gw' AND q.kind = 'request'"
            " AND q.body->>'method' = 'tools/call' AND a.body ? 'result'"
            " AND NOT (a.body->'result'->>'isError')::boolean ORDER BY q.seq"
        ).fetchall()
    unlogged = READS | ({"submit_final_report"} if config == "G1" else set())
    calls = Counter(_canonical(tool, args) for tool, args in answered if tool not in unlogged)
    assert Counter(_canonical(tool, args) for tool, args in logged) == calls
    tools = [tool for tool, _ in logged]
    assert "submit_purchase_order" in tools
    assert ("submit_final_report" in tools) == (config == "G2")
    order = next(args for tool, args in logged if tool == "submit_purchase_order")
    assert (order["intent_id"], order["supplier_id"], len(order["based_on"])) == ("REQ-1", "S-1", 2)


def test_a_logged_response_needs_its_arguments_as_an_object(migrated_database: str) -> None:
    insert = (
        "INSERT INTO gw.tool_call (call_no, tool, args, result, as_of_seq, vt)"
        " VALUES (%s, 'get_action_status', %s, '{}', 0, 0)"
    )
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(insert, (1, None))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (1, '["A-0001"]'))
        conn.execute(insert, (1, '{"action_id": "A-0001", "wait_ms": 0}'))
        assert conn.execute("SELECT args FROM gw.tool_call").fetchall() == [
            ({"action_id": "A-0001", "wait_ms": 0},)
        ]


def test_a_database_with_logged_calls_cannot_take_the_column(
    empty_database: str, tmp_path: Path
) -> None:
    """The arguments of calls logged before 0014 were never recorded, and an empty object
    would say there were none. So 0014 refuses a database whose `gw.tool_call` has rows:
    the file rolls back and the runner stops, before the column exists."""
    every = discover(DB_DIR / "migrations")
    (added,) = [m for m in every if m.name == "0014_tool_call_args.sql"]
    before = tmp_path / "before"
    before.mkdir()
    for migration in every:
        if migration.number < added.number:
            shutil.copy(migration.path, before / migration.name)
    with psycopg.connect(empty_database, autocommit=True) as conn:
        migrate(conn, before)
        conn.execute(
            "INSERT INTO gw.tool_call (call_no, tool, result, as_of_seq, vt)"
            " VALUES (1, 'get_action_status', '{}', 0, 0)"
        )
        with pytest.raises(psycopg.errors.NotNullViolation, match='column "args"'):
            migrate(conn, DB_DIR / "migrations")
        applied = conn.execute("SELECT filename FROM public.schema_migration").fetchall()
        assert added.name not in {row[0] for row in applied}
        column = conn.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_schema = 'gw' AND table_name = 'tool_call' AND column_name = 'args'"
        ).fetchone()
        assert column is None
