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
