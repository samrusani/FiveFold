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
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import psycopg
import pytest

from fivefold.agents.worker import WorkerEnd
from fivefold.harness.demo import demo_1_spec
from fivefold.harness.run import Config, InprocRun

pytestmark = pytest.mark.pg


def run(dsn: str, config: Config, *, seed: int = 7) -> InprocRun:
    inproc = InprocRun(demo_1_spec(config, seed), dsn)
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


@pytest.mark.parametrize("config", ["B0", "G2"])
def test_a_run_from_a_seed_is_byte_identical(
    run_database: Callable[[str], str], config: Config
) -> None:
    """The determinism contract (0009): the same inputs give the same records."""
    first, second = run_database(f"{config.lower()}_1"), run_database(f"{config.lower()}_2")
    run(first, config)
    run(second, config)
    assert message_log(first) == message_log(second)
    assert effects(first) == effects(second)
    assert reports(first) == reports(second)


def test_another_seed_changes_the_timing_but_not_the_outcome(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("seed_8")
    run(dsn, "G2", seed=8)
    assert effects(dsn) == [("PO-0001", "REQ-1", "APR-0001", "A-0001")]


def after_result_plan() -> list[dict[str, object]]:
    """F11 alone, killing the worker as the order's result reaches it (0006)."""
    return [
        {
            "fault": "F11",
            "at": {
                "point": "agent.after_tool_result",
                "match": {"tool": "submit_purchase_order"},
                "occurrence": 1,
            },
            "params": {"restart_delay_ms": 1_000},
        }
    ]


@pytest.mark.parametrize(("config", "orders"), [("G2", 1), ("B0", 2)])
def test_a_result_voided_by_a_crash_is_never_seen_and_the_call_is_repeated(
    run_database: Callable[[str], str], config: Config, orders: int
) -> None:
    dsn = run_database(f"after_result_{config.lower()}")
    spec = replace(demo_1_spec(config, 7), plan=after_result_plan())
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    assert inproc.result.ends == {"agent-1": (WorkerEnd.FINISHED, "Done.")}
    log = message_log(dsn)
    calls = [row for row in log if row[2] == "request" and row[5] == "submit_purchase_order"]
    calls = [row for row in calls if row[3] == "agent-1"]
    assert len(calls) == 2, "the resumed worker repeats the call whose result it lost"
    with psycopg.connect(dsn) as conn:
        ((voided,),) = conn.execute("SELECT voided FROM harness.fault_log").fetchall()
    first_answer = next(row for row in log if row[2] == "answer" and row[6] == calls[0][0])
    assert voided == [first_answer[0]], "the fault voids the result just delivered"
    assert first_answer[12] == "delivered"
    assert len(effects(dsn)) == orders


def test_the_run_records_when_it_stopped_and_the_evaluator_ends_it_there(
    run_database: Callable[[str], str],
) -> None:
    """The stop comes after the drain window, past the last record (0009)."""
    dsn = run_database("demo_1_stop")
    inproc = run(dsn, "G2")
    with psycopg.connect(dsn) as conn:
        ((stopped_vt,),) = conn.execute("SELECT stopped_vt FROM harness.run_stop").fetchall()
        ((last_vt,),) = conn.execute(
            "SELECT max(greatest(sent_vt, coalesce(delivered_vt, 0))) FROM harness.message_log"
        ).fetchall()
    assert stopped_vt == inproc.result.stopped_vt > last_vt
    assert inproc.result.evaluation is not None
    assert inproc.result.evaluation.run.end_vt == stopped_vt
