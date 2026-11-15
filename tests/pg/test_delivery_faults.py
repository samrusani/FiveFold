"""F4 and F5 in whole runs of dev-001 (0006; M3).

- F4 delivers the order's request twice, on the gateway's hop to the simulator, the
  agent's hop to the gateway, or B0's hop to the simulator.
- F5 holds the order where it reaches the simulator and commits it later: 5,000 ms in
  contract, 20,000 ms beyond the deadline.

The assertions are ground truth: the simulator's ledger and writes, the harness's
message fates and fault log, the gateway's events, and the report. Verdicts are in
`test_expected.py`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, LiteralString

import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.cells import Cell, cell_plan
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import InprocRun, RunSpec

pytestmark = pytest.mark.pg

SEED = 7


def run(dsn: str, fault: str, variant: str | None, contract: Contract, config: str) -> InprocRun:
    """Run dev-001 with the plan and scenario of the matching expected-outcome cell."""
    cell = Cell(
        fault=fault, variant=variant, contract=contract, config=config, verdict="", failing=()
    )
    scenario = load_scenario_file(DEV_001)
    spec = RunSpec(scenario, config, contract, SEED, plan=cell_plan(cell))  # type: ignore[arg-type]
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    return inproc


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def effects(dsn: str) -> list[tuple[Any, ...]]:
    return rows(dsn, "SELECT effect_id, approval_id FROM sim.effect ORDER BY commit_seq")


def claim(dsn: str) -> tuple[str, list[str]]:
    report = rows(dsn, "SELECT report FROM harness.final_report WHERE valid ORDER BY seq DESC")
    (only,) = report[0][0]["claims"]
    return only["status"], only["effect_ids"]


def faults(dsn: str) -> list[tuple[Any, ...]]:
    return rows(
        dsn,
        "SELECT fault, variant, point, action, target_seq, component, params"
        " FROM harness.fault_log ORDER BY seq",
    )


ORDERS_TO_SIM: LiteralString = (
    "SELECT seq, copy_of, fate, drop_reason FROM harness.message_log"
    " WHERE kind = 'request' AND api = 'submit_purchase_order' AND hop <> 'agent_gw'"
    " ORDER BY seq"
)
ANSWERS_FROM_SIM: LiteralString = (
    "SELECT correlation, fate, drop_reason, body->>'answer', body->>'effect_id'"
    " FROM harness.message_log WHERE kind = 'answer' AND api = 'submit_purchase_order'"
    " AND hop <> 'agent_gw' ORDER BY seq"
)
WRITES: LiteralString = (
    "SELECT received_vt, state, outcome_vt, deadline_vt, deadline_waived, effect_id"
    " FROM sim.write ORDER BY received_seq"
)
EVENTS: LiteralString = (
    "SELECT action_id, to_state, reason FROM gw.action_event"
    " WHERE from_state IS DISTINCT FROM to_state ORDER BY seq"
)


# F4: duplicate delivery ------------------------------------------------------------------


def test_f4_without_keys_the_copy_commits_a_second_order(
    run_database: Callable[[str], str],
) -> None:
    """G2 at C0, on gw_sim: a documented limit (0006). The gateway hears of one order."""
    dsn = run_database("f4_gw_sim_c0")
    run(dsn, "F4", "gw_sim", Contract.C0, "G2")
    assert effects(dsn) == [("PO-0001", "APR-0001"), ("PO-0002", "APR-0001")]
    original, copy = rows(dsn, ORDERS_TO_SIM)
    assert (original[1:], copy[1:]) == ((None, "delivered", None), (original[0], "delivered", None))
    assert rows(dsn, ANSWERS_FROM_SIM) == [
        (original[0], "delivered", None, "committed", "PO-0001"),
        (copy[0], "dropped", "answer_to_copy", "committed", "PO-0002"),
    ]
    ((fault, variant, point, action, target, component, params),) = faults(dsn)
    assert (fault, variant, point, action, target, component) == (
        "F4",
        None,
        "net.gw_sim.request",
        "duplicate",
        original[0],
        None,
    )
    assert params["copy_seq"] == copy[0] and 5 <= params["copy_delay_ms"] <= 50
    assert claim(dsn) == ("done", ["PO-0001"])


def test_f4_with_keys_the_copy_gets_the_stored_result(run_database: Callable[[str], str]) -> None:
    """G2 at C2, on gw_sim: the copy's key already committed, so it is a replay."""
    dsn = run_database("f4_gw_sim_c2")
    run(dsn, "F4", "gw_sim", Contract.C2, "G2")
    assert effects(dsn) == [("PO-0001", "APR-0001")]
    first, copy = rows(dsn, WRITES)
    assert (first[1], first[5], copy[1], copy[5]) == ("committed", "PO-0001", "replayed", "PO-0001")
    copy_answer = rows(dsn, ANSWERS_FROM_SIM)[1]
    assert copy_answer[1:] == ("dropped", "answer_to_copy", "committed", "PO-0001")
    assert claim(dsn) == ("done", ["PO-0001"])


def test_f4_on_the_agents_hop_the_copy_attaches_to_the_same_action(
    run_database: Callable[[str], str],
) -> None:
    """G2 at C0, on agent_gw: the gateway admits the order once; the copy of the tool call
    attaches to it (0001), and its answer never reaches the agent."""
    dsn = run_database("f4_agent_gw_c0")
    run(dsn, "F4", "agent_gw", Contract.C0, "G2")
    assert effects(dsn) == [("PO-0001", "APR-0001")]
    assert rows(dsn, "SELECT admission, action_id FROM gw.proposal ORDER BY proposal_id") == [
        ("admitted", "A-0001"),
        ("attached", "A-0001"),
    ]
    calls = rows(
        dsn,
        "SELECT seq, kind, copy_of, correlation, fate, drop_reason FROM harness.message_log"
        " WHERE hop = 'agent_gw' AND api = 'submit_purchase_order' ORDER BY seq",
    )
    request, copy = (row for row in calls if row[1] == "request")
    assert copy[2] == request[0]
    answers = sorted(row[3:] for row in calls if row[1] == "answer")
    assert answers == [
        (request[0], "delivered", None),
        (copy[0], "dropped", "answer_to_copy"),
    ]
    assert claim(dsn) == ("done", ["PO-0001"])


@pytest.mark.parametrize(
    ("contract", "orders"),
    [(Contract.C2, [("PO-0001", "APR-0001"), ("PO-0002", "APR-0001")]), (Contract.C3, [])],
)
def test_f4_in_b0_without_keys_until_the_conditional_write_refuses_the_copy(
    run_database: Callable[[str], str], contract: Contract, orders: list[tuple[str, str]]
) -> None:
    """B0 sends no idempotency key (0008). At C3 the copy finds its approval used and the
    budget short, so only the first order commits."""
    dsn = run_database(f"f4_b0_{contract.lower()}")
    run(dsn, "F4", "agent_sim", contract, "B0")
    states = [row[1] for row in rows(dsn, WRITES)]
    if contract is Contract.C3:
        assert effects(dsn) == [("PO-0001", "APR-0001")]
        assert states == ["committed", "rejected"]
        ((checks,),) = rows(
            dsn,
            "SELECT rejection->'failed_checks' FROM sim.write WHERE state = 'rejected'",
        )
        assert {check["check"] for check in checks} == {"approval_used", "stale_input"}
    else:
        assert effects(dsn) == orders
        assert states == ["committed", "committed"]
    answers = rows(dsn, ANSWERS_FROM_SIM)
    assert [row[1:3] for row in answers] == [("delivered", None), ("dropped", "answer_to_copy")]
    assert claim(dsn) == ("done", ["PO-0001"])


def test_two_runs_of_an_f4_cell_give_byte_identical_logs(
    run_database: Callable[[str], str],
) -> None:
    """0009: the same scenario, configuration, contract level, seed and model outputs give
    byte-identical logs and ledgers, wall-clock columns aside. The copy delay is drawn
    from the run's seed."""
    texts = []
    for name in ("f4_twice_a", "f4_twice_b"):
        dsn = run_database(name)
        run(dsn, "F4", "gw_sim", Contract.C1, "G2")
        records = {
            "messages": rows(
                dsn,
                "SELECT seq, hop, kind, sender, receiver, api, correlation, intent_id,"
                " idempotency_key, answer_class, body, sent_vt, fate, drop_reason,"
                " delivered_seq, delivered_vt, copy_of FROM harness.message_log ORDER BY seq",
            ),
            "faults": faults(dsn),
            "effects": rows(
                dsn,
                "SELECT effect_id, intent_id, args, approval_id, basis, idempotency_key,"
                " received_seq, commit_seq, committed_vt FROM sim.effect ORDER BY commit_seq",
            ),
        }
        texts.append(json.dumps(records, sort_keys=True, default=str))
    assert texts[0] == texts[1]
    assert '"answer_to_copy"' in texts[0]


# F5: late commit -------------------------------------------------------------------------


def test_f5_in_contract_at_c1_the_order_commits_late_and_a_probe_finds_it(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f5_in_contract_c1")
    run(dsn, "F5", "in_contract", Contract.C1, "G2")
    ((arrived, state, outcome_vt, deadline_vt, waived, effect_id),) = rows(dsn, WRITES)
    assert (state, outcome_vt, waived, effect_id) == (
        "committed",
        arrived + 5_000,
        False,
        "PO-0001",
    )
    assert outcome_vt < deadline_vt
    ((request, _, fate, _),) = rows(dsn, ORDERS_TO_SIM)
    assert fate == "delivered"
    assert rows(dsn, ANSWERS_FROM_SIM) == [
        (request, "dropped", "caller_timed_out", "committed", "PO-0001")
    ]
    assert [event[1:] for event in rows(dsn, EVENTS)][-3:] == [
        ("dispatched", "dispatched"),
        ("outcome_unknown", "timeout"),
        ("confirmed", "status_found"),
    ]
    assert faults(dsn) == [
        (
            "F5",
            "in_contract",
            "sim.write.received",
            "delay_commit",
            request,
            "simulator",
            {"delay_ms": 5_000},
        )
    ]
    assert claim(dsn) == ("done", ["PO-0001"])


def test_f5_beyond_the_deadline_at_c1_failed_was_wrong_and_the_budget_is_overdrawn(
    run_database: Callable[[str], str],
) -> None:
    """A documented G2 limit (0006): the destination broke its deadline, so an empty
    status answer after the settle time made Failed wrong. The agent's second order
    committed, then the late first one."""
    dsn = run_database("f5_beyond_c1")
    run(dsn, "F5", "beyond_deadline", Contract.C1, "G2")
    first, second = rows(dsn, WRITES)
    assert first[1:] == ("committed", first[0] + 20_000, first[0] + 10_000, True, "PO-0002")
    assert second[1] == "committed" and second[2] < first[2]
    assert effects(dsn) == [("PO-0001", "APR-0002"), ("PO-0002", "APR-0001")]
    assert ("A-0001", "failed", "status_settled_empty") in rows(dsn, EVENTS)
    assert ("A-0002", "confirmed", "committed") in rows(dsn, EVENTS)
    ((left,),) = rows(
        dsn,
        "SELECT (value->>'limit_minor')::int - (value->>'committed_minor')::int"
        " FROM sim.fact_version WHERE key = 'budget/CC-OPS' ORDER BY version DESC LIMIT 1",
    )
    assert left < 0, "the late order overdrew the budget"
    assert claim(dsn) == ("done", ["PO-0001"])


def test_f5_at_c2_the_retries_get_in_progress_then_the_stored_result(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f5_in_contract_c2")
    run(dsn, "F5", "in_contract", Contract.C2, "G2")
    states = [row[1] for row in rows(dsn, WRITES)]
    assert states[0] == "committed" and states[-1] == "replayed"
    assert set(states[1:-1]) == {"in_progress"}
    assert effects(dsn) == [("PO-0001", "APR-0001")]
    assert claim(dsn) == ("done", ["PO-0001"])


@pytest.mark.parametrize("variant", ["in_contract", "beyond_deadline"])
def test_f5_at_c0_g2_escalates_and_reports_unknown(
    run_database: Callable[[str], str], variant: str
) -> None:
    dsn = run_database(f"f5_{variant}_c0")
    run(dsn, "F5", variant, Contract.C0, "G2")
    assert effects(dsn) == [("PO-0001", "APR-0001")]
    assert rows(dsn, "SELECT action_id, resolution FROM gw.escalation") == [("A-0001", None)]
    assert claim(dsn) == ("unknown", [])


def test_f5_in_b0_the_framework_retry_orders_again_without_a_key(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f5_in_contract_b0")
    run(dsn, "F5", "in_contract", Contract.C0, "B0")
    first, retry = rows(dsn, WRITES)
    assert (first[1], first[2], first[5]) == ("committed", first[0] + 5_000, "PO-0002")
    assert (retry[1], retry[5]) == ("committed", "PO-0001")
    assert effects(dsn) == [("PO-0001", "APR-0001"), ("PO-0002", "APR-0001")]
    assert claim(dsn) == ("done", ["PO-0001"])
