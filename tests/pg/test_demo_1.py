"""Demo 1, "Order placed, reply lost", end to end from a seed (0006, Demos; M1 gate).

F2 drops the purchase order's committed answer, and F11 kills the agent right after it
sends that tool call, resuming it from its checkpoint 10,000 virtual ms later. In G2 the
gateway retries with the same key while the agent is down and gets the stored result;
the resumed agent re-sends the same proposal, which attaches to the confirmed action:
one order. In B0 the resumed agent re-sends the order itself and places a second one,
reusing the first approval and overdrawing the budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from fivefold.agents.worker import WorkerEnd
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario_file
from fivefold.harness.migrations import drop_database
from fivefold.harness.run import InprocRun, RunSpec

pytestmark = pytest.mark.pg

DEV_001 = "scenarios/dev/dev-001-single-po.yaml"


def demo_1_plan(hop: str) -> list[dict[str, Any]]:
    return [
        {
            "fault": "F2",
            "at": {
                "point": f"net.{hop}.answer",
                "match": {"api": "submit_purchase_order", "answer": "committed"},
                "occurrence": 1,
            },
        },
        {
            "fault": "F11",
            "at": {
                "point": "agent.after_tool_call",
                "match": {"tool": "submit_purchase_order"},
                "occurrence": 1,
            },
            "params": {"restart_delay_ms": 10_000},
        },
    ]


@pytest.fixture
def run_database(admin_dsn: str, sim_template: str) -> Iterator[Callable[[str], str]]:
    """Fresh migrated run databases, cloned from the template; dropped afterwards."""
    made: list[str] = []

    def make(suffix: str) -> str:
        name = f"{sim_template}_{suffix}"
        drop_database(admin_dsn, name)
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(name), sql.Identifier(sim_template)
                )
            )
        made.append(name)
        return make_conninfo(admin_dsn, dbname=name)

    yield make
    for name in made:
        drop_database(admin_dsn, name)


def scenario() -> Scenario:
    return load_scenario_file(DEV_001)


def run(dsn: str, config: str, *, seed: int = 7) -> InprocRun:
    hop = "gw_sim" if config == "G2" else "agent_sim"
    spec = RunSpec(scenario(), config, Contract.C2, seed, plan=demo_1_plan(hop))  # type: ignore[arg-type]
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    return inproc


def effects(dsn: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT effect_id, intent_id, approval_id, idempotency_key FROM sim.effect"
            " ORDER BY commit_seq"
        ).fetchall()


def reports(dsn: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT worker, report, valid, accepted_by FROM harness.final_report ORDER BY seq"
        ).fetchall()


def message_log(dsn: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT seq, hop, kind, sender, receiver, api, correlation, intent_id,"
            " idempotency_key, answer_class, body, sent_vt, fate, drop_reason, delivered_seq,"
            " delivered_vt FROM harness.message_log ORDER BY seq"
        ).fetchall()


def test_g2_one_order_whatever_the_lost_reply_and_the_crash(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("g2")
    inproc = run(dsn, "G2")
    assert inproc.result.ends == {"agent-1": (WorkerEnd.FINISHED, "Done.")}
    assert effects(dsn) == [("PO-0001", "REQ-1", "APR-0001", "A-0001")]
    ((worker, report, valid, accepted_by),) = reports(dsn)
    assert (worker, valid, accepted_by) == ("agent-1", True, "gateway")
    assert report["claims"][0]["status"] == "done"
    assert report["claims"][0]["effect_ids"] == ["PO-0001"]
    with psycopg.connect(dsn) as conn:
        faults = conn.execute("SELECT fault, point FROM harness.fault_log ORDER BY seq").fetchall()
        closed = conn.execute("SELECT run_closed FROM gw.run_state").fetchone()
    assert faults == [("F11", "agent.after_tool_call"), ("F2", "net.gw_sim.answer")]
    assert closed == (True,)


def test_b0_places_a_second_order_after_the_resume(run_database: Callable[[str], str]) -> None:
    dsn = run_database("b0")
    inproc = run(dsn, "B0")
    assert inproc.result.ends == {"agent-1": (WorkerEnd.FINISHED, "Done.")}
    assert effects(dsn) == [
        ("PO-0001", "REQ-1", "APR-0001", None),
        ("PO-0002", "REQ-1", "APR-0001", None),
    ], "a second order, on the first approval"
    with psycopg.connect(dsn) as conn:
        budget = conn.execute(
            "SELECT value FROM sim.fact_version WHERE key = 'budget/CC-OPS'"
            " ORDER BY version DESC LIMIT 1"
        ).fetchone()
    assert budget is not None
    assert budget[0]["committed_minor"] > budget[0]["limit_minor"], "the budget is overdrawn"


def test_a_run_from_a_seed_is_byte_identical(run_database: Callable[[str], str]) -> None:
    """The determinism contract (0009): the same inputs give the same message log."""
    first, second = run_database("first"), run_database("second")
    run(first, "G2")
    run(second, "G2")
    assert message_log(first) == message_log(second)
    assert effects(first) == effects(second)
