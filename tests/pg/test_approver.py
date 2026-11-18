"""The scripted approver in whole runs, B0 and G2 (0002, rule 7; 0008).

The dev scenario's orders are small enough for policy to approve at once, so these runs
lower the auto-approval threshold to zero: every order waits for the scripted approver.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, LiteralString

import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import scenario_from_data
from fivefold.harness.run import Config, InprocRun, RunSpec

pytestmark = pytest.mark.pg


def queued(dev_data: dict[str, Any], approver: list[dict[str, Any]]) -> dict[str, Any]:
    """The dev scenario with every order queued for an approver with these rules."""
    dev_data["authority"]["auto_approve_up_to_minor"] = 0
    dev_data["authority"]["approver"] = approver
    dev_data["faults"] = []
    return dev_data


def run(dsn: str, data: dict[str, Any], config: Config) -> InprocRun:
    spec = RunSpec(scenario_from_data(data), config, Contract.C2, 7)
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    return inproc


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def claims(dsn: str) -> list[Any]:
    (report,) = rows(dsn, "SELECT report FROM harness.final_report WHERE valid")
    return [(c["status"], c["effect_ids"]) for c in report[0]["claims"]]


@pytest.mark.parametrize("config", ["B0", "G2"])
def test_an_approver_that_approves_after_two_seconds_lets_the_order_through(
    run_database: Callable[[str], str], dev_data: dict[str, Any], config: Config
) -> None:
    dsn = run_database(f"approve_{config.lower()}")
    data = queued(dev_data, [{"match": {}, "decision": "approve", "after_ms": 2_000}])
    run(dsn, data, config)
    assert [effect for (effect,) in rows(dsn, "SELECT effect_id FROM sim.effect")] == ["PO-0001"]
    ((actor, action, subject, vt),) = rows(
        dsn, "SELECT actor, action, subject, vt FROM harness.actor_log"
    )
    ((requested_vt,),) = rows(dsn, "SELECT requested_vt FROM sim.approval")
    assert (actor, action, subject) == ("approver", "approve", "APR-0001")
    assert vt == requested_vt + 2_000, "the approver decides after its delay"
    assert claims(dsn) == [("done", ["PO-0001"])]


def test_the_final_poll_can_use_an_approval_decided_after_the_deadline(
    run_database: Callable[[str], str], dev_data: dict[str, Any]
) -> None:
    """Record 0017's reproduction, with the decision 8 virtual ms after the deadline."""
    dsn = run_database("approval_final_poll")
    wait_ms = 1_000
    data = queued(dev_data, [{"match": {}, "decision": "approve", "after_ms": 1_030}])
    data["authority"]["approval_wait_ms"] = wait_ms
    evaluation = run(dsn, data, "G2").result.evaluation

    ((requested_vt,),) = rows(dsn, "SELECT requested_vt FROM gw.grant")
    deadline = requested_vt + wait_ms
    ((approval_id, decided_vt, expires_at),) = rows(
        dsn, "SELECT approval_id, decided_vt, expires_at FROM sim.approval"
    )
    polls = rows(
        dsn,
        "SELECT delivered_vt, body->>'status' FROM harness.message_log"
        " WHERE kind = 'answer' AND hop = 'gw_sim' AND api = 'get_approval_status'"
        " AND fate = 'delivered' ORDER BY delivered_vt, seq",
    )
    final_poll = next(((vt, status) for vt, status in polls if vt >= deadline), None)
    assert final_poll is not None
    observed_vt, status = final_poll
    assert status == "approved"
    assert deadline < decided_vt <= observed_vt
    assert rows(
        dsn, "SELECT reason FROM gw.action_event WHERE reason IN ('approved', 'approval_timeout')"
    ) == [("approved",)]

    ((cited_approval, committed_vt),) = rows(
        dsn, "SELECT approval_id, committed_vt FROM sim.effect"
    )
    assert cited_approval == approval_id
    assert observed_vt < committed_vt < expires_at
    assert evaluation is not None
    verdicts = {prop.property: prop.verdict for prop in evaluation.properties}
    assert (verdicts[2], verdicts[4]) == ("pass", "pass")


@pytest.mark.parametrize("config", ["B0", "G2"])
def test_a_denial_stops_the_intent_with_no_order(
    run_database: Callable[[str], str], dev_data: dict[str, Any], config: Config
) -> None:
    dsn = run_database(f"deny_{config.lower()}")
    data = queued(
        dev_data, [{"match": {"supplier_id": "S-1"}, "decision": "deny", "after_ms": 500}]
    )
    run(dsn, data, config)
    assert rows(dsn, "SELECT effect_id FROM sim.effect") == []
    assert rows(dsn, "SELECT action FROM harness.actor_log") == [("deny",)]
    assert claims(dsn) == [("not_done", [])]
    if config == "G2":
        assert rows(dsn, "SELECT state FROM gw.action") == [("rejected",)]


@pytest.mark.parametrize("config", ["B0", "G2"])
def test_with_no_matching_rule_the_request_stays_pending_and_the_agent_gives_up(
    run_database: Callable[[str], str], dev_data: dict[str, Any], config: Config
) -> None:
    dsn = run_database(f"silent_{config.lower()}")
    run(dsn, queued(dev_data, []), config)
    assert rows(dsn, "SELECT effect_id FROM sim.effect") == []
    assert rows(dsn, "SELECT actor FROM harness.actor_log") == []
    assert claims(dsn) == [("not_done", [])]
    if config == "G2":
        (state,) = rows(dsn, "SELECT state FROM gw.action")
        assert state == ("rejected",), "the report closed the intent, so nothing can send"
