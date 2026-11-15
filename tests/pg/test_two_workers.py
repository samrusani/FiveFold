"""F9, two workers on the same task, in whole runs (0006, 0010).

`agent.step` fires each time a worker's model finishes a step; F9 starts `agent-2`
there, with its own checkpoint thread and step budget, running the same policy from the
start. In `different_args` it chooses the second-cheapest acceptable option, S-2's quote.
The gateway expects one report from each worker the plan can start (0010).

The outcomes asserted here are ground truth; verdicts are in `faults/expected.yaml`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, LiteralString

import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import Config, InprocRun, RunSpec

pytestmark = pytest.mark.pg


def f9(variant: str, occurrence: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "fault": "F9",
            "variant": variant,
            "at": {"point": "agent.step", "match": {"worker": "agent-1"}, "occurrence": occurrence},
        }
    ]


def run(dsn: str, config: Config, plan: list[dict[str, Any]]) -> InprocRun:
    spec = RunSpec(load_scenario_file(DEV_001), config, Contract.C2, 7, plan=plan)
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    return inproc


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def orders(dsn: str) -> list[tuple[Any, ...]]:
    return rows(
        dsn,
        "SELECT effect_id, approval_id, args->>'supplier_id' FROM sim.effect ORDER BY commit_seq",
    )


def reports(dsn: str) -> list[tuple[str, list[tuple[str, list[str]]]]]:
    found = rows(dsn, "SELECT worker, report FROM harness.final_report WHERE valid ORDER BY seq")
    return [
        (worker, [(c["status"], c["effect_ids"]) for c in report["claims"]])
        for worker, report in found
    ]


def test_f9_starts_a_second_worker_at_the_first_workers_step(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f9_start")
    inproc = run(dsn, "G2", f9("same_args", occurrence=3))
    ((fault, variant, point, action, component, params),) = rows(
        dsn, "SELECT fault, variant, point, action, component, params FROM harness.fault_log"
    )
    assert (fault, variant, point, action, component) == (
        "F9",
        "same_args",
        "agent.step",
        "start_worker",
        "agent-2",
    )
    assert params == {"worker": "agent-1", "step": 3}
    assert sorted(inproc.result.ends) == ["agent-1", "agent-2"]
    assert all(end == "finished" for end, _ in inproc.result.ends.values())


@pytest.mark.parametrize("variant", ["same_args", "different_args"])
def test_in_b0_each_worker_places_its_own_order(
    run_database: Callable[[str], str], variant: str
) -> None:
    dsn = run_database(f"f9_b0_{variant}")
    run(dsn, "B0", f9(variant))
    placed = orders(dsn)
    assert [(effect, approval) for effect, approval, _ in placed] == [
        ("PO-0001", "APR-0001"),
        ("PO-0002", "APR-0002"),
    ], "two orders, each on its own approval"
    second = "S-2" if variant == "different_args" else "S-1"
    assert sorted(supplier for _, _, supplier in placed) == sorted(["S-1", second])
    assert [worker for worker, _ in reports(dsn)] == ["agent-1", "agent-2"]


def test_in_g2_the_second_worker_adopts_the_first_workers_order(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f9_g2_same")
    run(dsn, "G2", f9("same_args"))
    assert orders(dsn) == [("PO-0001", "APR-0001", "S-1")]
    assert rows(dsn, "SELECT action_id, state FROM gw.action") == [("A-0001", "confirmed")]
    assert reports(dsn) == [
        ("agent-1", [("done", ["PO-0001"])]),
        ("agent-2", [("done", ["PO-0001"])]),
    ]
    assert rows(dsn, "SELECT run_closed FROM gw.run_state") == [(True,)]


def test_in_g2_a_different_order_for_the_same_intent_is_refused(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f9_g2_different")
    run(dsn, "G2", f9("different_args"))
    assert orders(dsn) == [("PO-0001", "APR-0001", "S-1")]
    refusals = rows(
        dsn,
        "SELECT result->>'reason' FROM gw.tool_call"
        " WHERE tool = 'submit_purchase_order' AND result->>'result' = 'refused'",
    )
    assert refusals and {reason for (reason,) in refusals} <= {"intent_busy", "intent_done"}
    assert [claims for _, claims in reports(dsn)] == [[("done", ["PO-0001"])]] * 2


def test_the_gateway_waits_for_both_workers_and_the_harness_closes_a_run_f9_never_joined(
    run_database: Callable[[str], str],
) -> None:
    """F9 planned for a step that never comes: the gateway expected two reports, got one,
    and the harness closed the run when the agents' phase ended (0003)."""
    dsn = run_database("f9_never")
    inproc = run(dsn, "G2", f9("same_args", occurrence=1_000))
    assert list(inproc.result.ends) == ["agent-1"]
    assert rows(dsn, "SELECT count(*) FROM harness.fault_log") == [(0,)]
    assert reports(dsn) == [("agent-1", [("done", ["PO-0001"])])]
    assert rows(dsn, "SELECT run_closed FROM gw.run_state") == [(True,)]
