"""F1 and F3, the gateway crashes, in whole runs (0003, startup recovery; 0006).

- F1a crashes the gateway after the dispatch check, before the attempt is recorded. The
  restarted gateway checks again and sends.
- F1b crashes it after the attempt is recorded, before the send. Recovery finds the open
  attempt: C2 and C3 retry with the same key, C1 probes and settles it as failed, C0
  escalates (0005).
- B0's equivalent kills the agent just before it sends the order; it resumes from its
  checkpoint and sends once.
- F3 crashes the gateway after the order's answer arrived, before it was recorded. The
  crash voids that answer. Recovery finds the open attempt, as after F1b, but this time
  the order committed. B0's equivalent kills the agent as the order's result reaches it.

The agent's call waits when the gateway dies: its connection ends, the framework retries,
and the agent opens a new connection to the restarted gateway (`agents/connection.py`).
The outcomes asserted here are ground truth; verdicts are in `faults/expected.yaml`.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any, LiteralString

import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import Config, InprocRun, RunSpec

pytestmark = pytest.mark.pg

ORDER = {"api": "submit_purchase_order"}
POINTS = {"a": "gw.dispatch.after_check", "b": "gw.dispatch.after_record"}


def f1(variant: str, config: Config = "G2", **params: int) -> list[dict[str, Any]]:
    point = "agent.before_tool_call" if config == "B0" else POINTS[variant]
    entry: dict[str, Any] = {
        "fault": "F1",
        "variant": variant,
        "at": {"point": point, "match": ORDER, "occurrence": 1},
    }
    if params:
        entry["params"] = params
    return [entry]


def run(
    dsn: str, contract: Contract, plan: list[dict[str, Any]], config: Config = "G2"
) -> InprocRun:
    spec = RunSpec(load_scenario_file(DEV_001), config, contract, 7, plan=plan)
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    return inproc


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def effects(dsn: str) -> list[tuple[Any, ...]]:
    return rows(
        dsn, "SELECT effect_id, approval_id, idempotency_key FROM sim.effect ORDER BY commit_seq"
    )


def claim(dsn: str) -> tuple[str, list[str]]:
    ((report,),) = rows(dsn, "SELECT report FROM harness.final_report WHERE valid")
    (only,) = report["claims"]
    return only["status"], only["effect_ids"]


def sent_orders(dsn: str) -> list[tuple[Any, ...]]:
    return rows(
        dsn,
        "SELECT sent_vt, fate FROM harness.message_log WHERE kind = 'request'"
        " AND api = 'submit_purchase_order' AND hop IN ('gw_sim', 'agent_sim') ORDER BY seq",
    )


def test_f1a_the_restarted_gateway_checks_again_and_sends_once(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f1a_c2")
    inproc = run(dsn, Contract.C2, f1("a"))
    assert effects(dsn) == [("PO-0001", "APR-0001", "A-0001")]
    ((fault, variant, point, action, component),) = rows(
        dsn, "SELECT fault, variant, point, action, component FROM harness.fault_log"
    )
    assert (fault, variant, point, action, component) == (
        "F1",
        "a",
        "gw.dispatch.after_check",
        "kill",
        "gateway",
    )
    assert rows(dsn, "SELECT state FROM gw.action") == [("confirmed",)]
    assert rows(dsn, "SELECT attempt_no FROM gw.attempt") == [(1,)], "nothing was recorded before"
    assert claim(dsn) == ("done", ["PO-0001"])
    assert inproc.result.ends["agent-1"][0] == "finished"


def test_f1b_a_possibly_sent_request_is_recorded_and_retried_with_its_key_at_c2(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f1b_c2")
    run(dsn, Contract.C2, f1("b"))
    ((params, vt),) = rows(dsn, "SELECT params, vt FROM harness.fault_log")
    assert params["request"] == {
        "intent_id": "REQ-1",
        "api": "submit_purchase_order",
        "idempotency_key": "A-0001",
    }, "the evaluator counts it as a request that may have left (0004, 0006)"
    assert effects(dsn) == [("PO-0001", "APR-0001", "A-0001")]
    reasons = [r for (r,) in rows(dsn, "SELECT reason FROM gw.action_event ORDER BY seq")]
    assert "recovered_open_attempt" in reasons
    assert rows(dsn, "SELECT attempt_no, idempotency_key FROM gw.attempt ORDER BY attempt_no") == [
        (1, "A-0001"),
        (2, "A-0001"),
    ]
    ((sent_vt, fate),) = sent_orders(dsn)
    assert fate == "delivered" and sent_vt > vt + 1_000, "only the retry left, after the restart"
    assert claim(dsn) == ("done", ["PO-0001"])


def test_f1b_at_c0_the_gateway_escalates_and_the_agent_reports_unknown(
    run_database: Callable[[str], str],
) -> None:
    """Safety kept, liveness lost: C0 cannot tell whether the request left (0006)."""
    dsn = run_database("f1b_c0")
    run(dsn, Contract.C0, f1("b"))
    assert effects(dsn) == []
    assert sent_orders(dsn) == []
    assert rows(dsn, "SELECT state FROM gw.action") == [("outcome_unknown",)]
    assert rows(dsn, "SELECT count(*) FROM gw.escalation") == [(1,)]
    assert claim(dsn) == ("unknown", [])


def test_f1b_at_c1_the_open_attempt_settles_as_failed_and_the_agent_orders_again(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f1b_c1")
    run(dsn, Contract.C1, f1("b"))
    assert rows(dsn, "SELECT action_id, state FROM gw.action ORDER BY action_id") == [
        ("A-0001", "failed"),
        ("A-0002", "confirmed"),
    ]
    ((effect_id, approval_id, _),) = effects(dsn)
    assert (effect_id, approval_id) == ("PO-0001", "APR-0002")
    assert claim(dsn) == ("done", ["PO-0001"])


def test_f1b_at_c3_recovery_retries_and_the_destination_commits_once(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f1b_c3")
    run(dsn, Contract.C3, f1("b"))
    assert effects(dsn) == [("PO-0001", "APR-0001", "A-0001")]
    assert claim(dsn) == ("done", ["PO-0001"])


def test_b0_killed_before_the_order_call_sends_it_once_after_resuming(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f1_b0")
    inproc = run(dsn, Contract.C2, f1("a", "B0"), config="B0")
    ((point, component, vt),) = rows(dsn, "SELECT point, component, vt FROM harness.fault_log")
    assert (point, component) == ("agent.before_tool_call", "agent-1")
    ((sent_vt, fate),) = sent_orders(dsn)
    assert fate == "delivered" and sent_vt >= vt + 1_000, "the call left only after the resume"
    ((effect_id, approval_id, _),) = effects(dsn)
    assert (effect_id, approval_id) == ("PO-0001", "APR-0001")
    assert claim(dsn) == ("done", ["PO-0001"])
    assert inproc.result.ends["agent-1"][0] == "finished"


def test_a_gateway_down_longer_than_the_agents_retries_still_recovers_its_action(
    run_database: Callable[[str], str],
) -> None:
    """The agent gives up while the gateway is down and ends with no report. The restarted
    gateway closes the run, and recovery still commits the order once, in the drain window."""
    dsn = run_database("f1b_long")
    inproc = run(dsn, Contract.C2, f1("b", restart_delay_ms=20_000))
    ((end, why),) = inproc.result.ends.values()
    assert end == "error" and "initialize" in why
    assert rows(dsn, "SELECT run_closed FROM gw.run_state") == [(True,)]
    assert effects(dsn) == [("PO-0001", "APR-0001", "A-0001")]
    assert rows(dsn, "SELECT count(*) FROM harness.final_report") == [(0,)]


def test_a_crash_run_is_deterministic(run_database: Callable[[str], str]) -> None:
    logs = []
    for name in ("f1_det_1", "f1_det_2"):
        dsn = run_database(name)
        run(dsn, Contract.C2, f1("b"))
        log = rows(
            dsn,
            "SELECT seq, hop, kind, sender, receiver, api, body, sent_vt, fate, drop_reason,"
            " delivered_seq, delivered_vt FROM harness.message_log ORDER BY seq",
        )
        logs.append(json.dumps(log, sort_keys=True, default=str))
    assert logs[0] == logs[1]


def test_a_g2_run_refuses_f1_at_b0s_equivalent_point() -> None:
    """In G2, F1 is a gateway crash; at `agent.before_tool_call` it would kill the agent and
    test nothing about recovery (0006)."""
    spec = RunSpec(load_scenario_file(DEV_001), "G2", Contract.C2, 7, plan=f1("a", "B0"))
    with pytest.raises(ValueError, match=re.escape("F1 at agent.before_tool_call")):
        InprocRun(spec, "postgresql://unused")


# F3 -------------------------------------------------------------------------------------


def f3(config: Config = "G2") -> list[dict[str, Any]]:
    if config == "B0":
        at = {"point": "agent.after_tool_result", "match": {"tool": "submit_purchase_order"}}
    else:
        at = {"point": "gw.dispatch.after_answer", "match": ORDER}
    return [{"fault": "F3", "at": {**at, "occurrence": 1}}]


def test_f3_voids_the_answer_and_recovery_retries_into_the_stored_result_at_c2(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f3_c2")
    run(dsn, Contract.C2, f3())
    ((voided, component),) = rows(dsn, "SELECT voided, component FROM harness.fault_log")
    assert component == "gateway"
    ((answer_seq, fate, answer_class),) = rows(
        dsn,
        "SELECT seq, fate, answer_class FROM harness.message_log WHERE hop = 'gw_sim'"
        " AND kind = 'answer' AND api = 'submit_purchase_order' ORDER BY seq LIMIT 1",
    )
    assert (fate, answer_class) == ("delivered", "committed")
    assert voided == [answer_seq], "the answer reached the gateway; the crash voided it"
    assert effects(dsn) == [("PO-0001", "APR-0001", "A-0001")]
    reasons = [r for (r,) in rows(dsn, "SELECT reason FROM gw.action_event ORDER BY seq")]
    assert "recovered_open_attempt" in reasons
    assert rows(dsn, "SELECT attempt_no FROM gw.attempt ORDER BY attempt_no") == [(1,), (2,)]
    assert claim(dsn) == ("done", ["PO-0001"])


def test_f3_after_a_lost_answer_waits_for_an_answer_that_arrives(
    run_database: Callable[[str], str],
) -> None:
    """F2 drops the order's first answer, so the first send times out and no answer reaches
    the gateway. F3 waits for the retry's answer, the first that arrives, and voids it."""
    dsn = run_database("f3_after_f2")
    f2 = {
        "fault": "F2",
        "at": {
            "point": "net.gw_sim.answer",
            "match": {**ORDER, "answer": "committed"},
            "occurrence": 1,
        },
    }
    run(dsn, Contract.C2, [f2, *f3()])
    answers = rows(
        dsn,
        "SELECT seq, fate FROM harness.message_log WHERE hop = 'gw_sim' AND kind = 'answer'"
        " AND api = 'submit_purchase_order' ORDER BY seq",
    )
    assert [fate for _, fate in answers] == ["dropped", "delivered", "delivered"]
    fired = rows(dsn, "SELECT fault, voided FROM harness.fault_log ORDER BY seq")
    assert fired == [("F2", []), ("F3", [answers[1][0]])]
    assert effects(dsn) == [("PO-0001", "APR-0001", "A-0001")]
    assert claim(dsn) == ("done", ["PO-0001"])


def test_f3_at_c1_a_probe_finds_the_order_the_crash_forgot(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f3_c1")
    run(dsn, Contract.C1, f3())
    assert effects(dsn) == [("PO-0001", "APR-0001", None)]
    assert rows(dsn, "SELECT state FROM gw.action") == [("confirmed",)]
    assert rows(dsn, "SELECT count(*) FROM gw.attempt") == [(1,)], "C1 never retries"
    assert claim(dsn) == ("done", ["PO-0001"])


def test_f3_at_c0_the_gateway_escalates_and_the_agent_reports_unknown(
    run_database: Callable[[str], str],
) -> None:
    """The order committed, but the gateway forgot its answer and C0 cannot ask (0006)."""
    dsn = run_database("f3_c0")
    run(dsn, Contract.C0, f3())
    assert effects(dsn) == [("PO-0001", "APR-0001", None)]
    assert rows(dsn, "SELECT state FROM gw.action") == [("outcome_unknown",)]
    assert claim(dsn) == ("unknown", [])


def test_b0_killed_as_the_orders_result_arrives_orders_again(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f3_b0")
    run(dsn, Contract.C2, f3("B0"), config="B0")
    ((point, component, voided),) = rows(
        dsn, "SELECT point, component, voided FROM harness.fault_log"
    )
    assert (point, component, len(voided)) == ("agent.after_tool_result", "agent-1", 1)
    assert [effect for effect, _, _ in effects(dsn)] == ["PO-0001", "PO-0002"]


def test_a_g2_run_refuses_f3_at_b0s_equivalent_point() -> None:
    plan = [{"fault": "F3", "at": {"point": "agent.after_tool_result", "occurrence": 1}}]
    spec = RunSpec(load_scenario_file(DEV_001), "G2", Contract.C2, 7, plan=plan)
    with pytest.raises(ValueError, match=re.escape("F3 at agent.after_tool_result")):
        InprocRun(spec, "postgresql://unused")
