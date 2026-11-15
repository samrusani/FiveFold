"""Approval expiry in whole runs (0002, rules 8 and 17; 0010, change 4).

An approval expires `approval_ttl_ms` after its decision. Before every send, the gateway
counts an approval as expired if it expires at or before the send's time plus the settle
time S (10,050 virtual ms in dev-001), because an effect must commit before the approval
expires. B0 checks nothing; a C3 destination checks at commit.

The outcomes asserted here are ground truth; verdicts are in `faults/expected.yaml`.
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

LOST_ORDER_ANSWER = {
    "fault": "F2",
    "at": {
        "point": "net.gw_sim.answer",
        "match": {"api": "submit_purchase_order", "answer": "committed"},
        "occurrence": 1,
    },
}


def run(
    dsn: str,
    dev_data: dict[str, Any],
    config: Config,
    contract: Contract,
    ttl_ms: int,
    plan: list[dict[str, Any]] | None = None,
) -> InprocRun:
    dev_data["authority"]["approval_ttl_ms"] = ttl_ms
    dev_data["faults"] = []
    spec = RunSpec(scenario_from_data(dev_data), config, contract, 7, plan=plan or [])
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    return inproc


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def claim(dsn: str) -> tuple[str, list[str]]:
    ((report,),) = rows(dsn, "SELECT report FROM harness.final_report WHERE valid")
    (only,) = report["claims"]
    return only["status"], only["effect_ids"]


def test_g2_refuses_to_send_on_an_approval_that_expires_before_a_send_could_settle(
    run_database: Callable[[str], str], dev_data: dict[str, Any]
) -> None:
    dsn = run_database("expiry_g2_short")
    run(dsn, dev_data, "G2", Contract.C2, ttl_ms=5_000)
    assert rows(dsn, "SELECT effect_id FROM sim.effect") == []
    assert rows(dsn, "SELECT state FROM gw.action") == [("rejected",)]
    ((reason,),) = rows(dsn, "SELECT reason FROM gw.action_event WHERE to_state = 'rejected'")
    assert reason == "approval_expired"
    ((status, decided_vt, expires_at),) = rows(
        dsn, "SELECT status, decided_vt, expires_at FROM sim.approval"
    )
    assert (status, expires_at - decided_vt) == ("approved", 5_000), "still approved at the check"
    assert claim(dsn) == ("not_done", [])


def test_g2_sends_when_the_approval_outlives_the_send_and_its_settle_time(
    run_database: Callable[[str], str], dev_data: dict[str, Any]
) -> None:
    dsn = run_database("expiry_g2_long")
    run(dsn, dev_data, "G2", Contract.C2, ttl_ms=12_000)
    ((committed_vt, expires_at),) = rows(
        dsn,
        "SELECT e.committed_vt, a.expires_at FROM sim.effect e"
        " JOIN sim.approval a USING (approval_id)",
    )
    assert committed_vt < expires_at
    assert claim(dsn) == ("done", ["PO-0001"])


def test_g2_does_not_retry_once_the_approval_would_expire_first(
    run_database: Callable[[str], str], dev_data: dict[str, Any]
) -> None:
    """The order's answer is lost; by the retry check, the approval expires before a
    retry could settle, so the gateway escalates instead of retrying (0002, rule 17)."""
    dsn = run_database("expiry_g2_retry")
    run(dsn, dev_data, "G2", Contract.C2, ttl_ms=12_000, plan=[LOST_ORDER_ANSWER])
    assert rows(dsn, "SELECT effect_id FROM sim.effect") == [("PO-0001",)]
    assert rows(dsn, "SELECT count(*) FROM gw.attempt") == [(1,)], "no retry was sent"
    assert rows(dsn, "SELECT state FROM gw.action") == [("outcome_unknown",)]
    assert rows(dsn, "SELECT count(*) FROM gw.escalation") == [(1,)]
    assert claim(dsn) == ("unknown", [])


def test_b0_orders_on_an_approval_that_has_already_expired(
    run_database: Callable[[str], str], dev_data: dict[str, Any]
) -> None:
    dsn = run_database("expiry_b0_c2")
    run(dsn, dev_data, "B0", Contract.C2, ttl_ms=500)
    ((committed_vt, expires_at),) = rows(
        dsn,
        "SELECT e.committed_vt, a.expires_at FROM sim.effect e"
        " JOIN sim.approval a USING (approval_id)",
    )
    assert committed_vt >= expires_at, "property 2 fails: expired at commit (0004)"
    assert claim(dsn) == ("done", ["PO-0001"])


def test_a_c3_destination_refuses_an_expired_approval_at_commit(
    run_database: Callable[[str], str], dev_data: dict[str, Any]
) -> None:
    dsn = run_database("expiry_b0_c3")
    run(dsn, dev_data, "B0", Contract.C3, ttl_ms=500)
    assert rows(dsn, "SELECT effect_id FROM sim.effect") == []
    ((reason, checks),) = rows(
        dsn,
        "SELECT body->>'reason', body->'failed_checks' FROM harness.message_log"
        " WHERE kind = 'answer' AND api = 'submit_purchase_order'",
    )
    assert reason == "precondition_failed"
    assert [check["check"] for check in checks] == ["approval_expired"]
    assert claim(dsn) == ("not_done", [])
