"""Whole runs of dev-001 with the scripted resolver (0003, 0009; M3).

The scenario is dev-001 with `authority.resolver` set to record 0009's example,
`{after_ms: 30000, attests: truth}`. The committed scenario file has no resolver, so the
expected outcomes in `faults/expected.yaml` stay as they are. Each run uses the plan of
an expected-outcome cell (`fivefold.harness.cells`) and the cells' seed.

- F2 at C0 in G2: the order commits and its answer is lost, so G2 escalates at once.
  30,000 ms later the resolver finds the order in the ledger and attests it; the action
  is Confirmed with `human_attested`, the agent's status wait ends at once, and the agent
  reports the order done. Without a resolver this cell is pass_unresolved.
- F12 on `gw_sim` at C0 in G2: the order's request never arrives, so G2 escalates at
  once. 30,000 ms later, well past the settle time, the resolver finds nothing and
  attests that nothing committed; the action fails with `human_attested`, and the agent
  tries once more, as 0008's policy says for a failure. Without a resolver this cell
  fails property 4.
- B0 has no gateway, so the resolver never starts.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, LiteralString

import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario_file, scenario_from_data
from fivefold.evaluator import RunResult, result_json
from fivefold.harness.cells import Cell, cell_plan
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import Config, InprocRun, RunSpec

pytestmark = pytest.mark.pg

SEED = 7
AFTER_MS = 30_000
T_MS = 2_000
"""dev-001's dispatch timeout T."""
SETTLE_MS = 10_050
"""dev-001's settle time S: `max_hop_latency` 50 plus `commit_deadline` 10,000."""


def with_resolver() -> Scenario:
    """dev-001, with record 0009's example resolver."""
    data = load_scenario_file(DEV_001).model_dump(mode="json")
    data["authority"]["resolver"] = {"after_ms": AFTER_MS, "attests": "truth"}
    return scenario_from_data(data)


def run(dsn: str, fault: str, variant: str | None, contract: Contract, config: Config) -> InprocRun:
    cell = Cell(
        fault=fault, variant=variant, contract=contract, config=config, verdict="", failing=()
    )
    spec = RunSpec(with_resolver(), config, contract, SEED, plan=cell_plan(cell))
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    return inproc


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def effects(dsn: str) -> list[tuple[Any, ...]]:
    return rows(dsn, "SELECT effect_id, approval_id FROM sim.effect ORDER BY commit_seq")


def attestations(dsn: str) -> list[tuple[Any, ...]]:
    return rows(
        dsn, "SELECT actor, action, subject, details, vt FROM harness.actor_log ORDER BY seq"
    )


def escalation(dsn: str) -> tuple[Any, ...]:
    (row,) = rows(
        dsn,
        "SELECT escalation_id, action_id, opened_vt, resolved_vt, resolution, effect_id, actor"
        " FROM gw.escalation",
    )
    return row


def transitions(dsn: str) -> list[tuple[Any, ...]]:
    return rows(
        dsn,
        "SELECT action_id, to_state, reason, vt FROM gw.action_event WHERE kind = 'transition'"
        " ORDER BY seq",
    )


def responses(dsn: str) -> list[tuple[Any, ...]]:
    """What the agent was told: each logged response, with its state, reason and time."""
    return rows(
        dsn,
        "SELECT tool, action_id, result->>'state', result->>'reason', vt FROM gw.tool_call"
        " ORDER BY call_no",
    )


def claim(dsn: str) -> tuple[str, list[str]]:
    report = rows(dsn, "SELECT report FROM harness.final_report WHERE valid ORDER BY seq DESC")
    (only,) = report[0][0]["claims"]
    return only["status"], only["effect_ids"]


def verdicts(evaluation: RunResult | None) -> tuple[str, list[tuple[int, str]]]:
    assert evaluation is not None
    properties = [(result.property, str(result.verdict)) for result in evaluation.properties]
    return str(evaluation.verdict), properties


ALL_PASS = [(number, "pass") for number in range(1, 6)]


def test_f2_at_c0_the_resolver_attests_the_order_and_the_agent_reports_it_done(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("res_f2_c0")
    inproc = run(dsn, "F2", None, Contract.C0, "G2")
    assert effects(dsn) == [("PO-0001", "APR-0001")]
    ((sent,),) = rows(dsn, "SELECT sent_vt FROM gw.attempt")
    attested = sent + T_MS + AFTER_MS
    assert escalation(dsn) == (
        "ESC-0001",
        "A-0001",
        sent + T_MS,
        attested,
        "committed",
        "PO-0001",
        "resolver",
    )
    assert transitions(dsn)[-3:] == [
        ("A-0001", "dispatched", "dispatched", sent),
        ("A-0001", "outcome_unknown", "timeout", sent + T_MS),
        ("A-0001", "confirmed", "human_attested", attested),
    ]
    assert attestations(dsn) == [
        (
            "resolver",
            "attest",
            "ESC-0001",
            {
                "resolution": "committed",
                "effect_id": "PO-0001",
                "intent_id": "REQ-1",
                "action_id": "A-0001",
            },
            attested,
        )
    ]
    told = responses(dsn)
    assert [row[:4] for row in told] == [
        ("submit_purchase_order", "A-0001", "outcome_unknown", "timeout"),
        ("get_action_status", "A-0001", "confirmed", "human_attested"),
        ("get_action_status", "A-0001", "confirmed", "human_attested"),
        ("submit_final_report", None, None, None),
    ]
    assert told[1][4] == attested, "the agent's 60,000 ms wait ended at the attestation"
    assert claim(dsn) == ("done", ["PO-0001"])
    evaluation = inproc.result.evaluation
    assert verdicts(evaluation) == ("pass", ALL_PASS)
    assert evaluation is not None and evaluation.metrics["human_interventions"] == 1
    assert evaluation.report is not None
    (judged,) = evaluation.report.claims
    assert (str(judged.knowledge), judged.known_effects) == ("committed", ("PO-0001",))


def test_f12_at_c0_the_resolver_attests_nothing_committed_and_the_agent_orders_again(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("res_f12_c0")
    inproc = run(dsn, "F12", "gw_sim", Contract.C0, "G2")
    assert effects(dsn) == [("PO-0001", "APR-0002")], "only the second order exists"
    sent = rows(dsn, "SELECT sent_vt FROM gw.attempt WHERE action_id = 'A-0001'")[0][0]
    attested = sent + T_MS + AFTER_MS
    assert attested >= sent + SETTLE_MS
    assert escalation(dsn) == (
        "ESC-0001",
        "A-0001",
        sent + T_MS,
        attested,
        "not_committed",
        None,
        "resolver",
    )
    assert attestations(dsn) == [
        (
            "resolver",
            "attest",
            "ESC-0001",
            {"resolution": "not_committed", "intent_id": "REQ-1", "action_id": "A-0001"},
            attested,
        )
    ]
    assert [row[:3] for row in transitions(dsn)] == [
        ("A-0001", "proposed", "admitted"),
        ("A-0001", "pending_approval", "approval_requested"),
        ("A-0001", "authorized", "approved"),
        ("A-0001", "dispatched", "dispatched"),
        ("A-0001", "outcome_unknown", "timeout"),
        ("A-0001", "failed", "human_attested"),
        ("A-0002", "proposed", "admitted"),
        ("A-0002", "pending_approval", "approval_requested"),
        ("A-0002", "authorized", "approved"),
        ("A-0002", "dispatched", "dispatched"),
        ("A-0002", "confirmed", "committed"),
    ]
    told = responses(dsn)
    assert [row[:4] for row in told] == [
        ("submit_purchase_order", "A-0001", "outcome_unknown", "timeout"),
        ("get_action_status", "A-0001", "failed", "human_attested"),
        ("submit_purchase_order", "A-0002", "confirmed", "committed"),
        ("get_action_status", "A-0001", "failed", "human_attested"),
        ("get_action_status", "A-0002", "confirmed", "committed"),
        ("submit_final_report", None, None, None),
    ]
    assert told[1][4] == attested, "the agent's wait ended at the attestation"
    assert claim(dsn) == ("done", ["PO-0001"])
    evaluation = inproc.result.evaluation
    assert verdicts(evaluation) == ("pass", ALL_PASS)
    assert evaluation is not None and evaluation.metrics["human_interventions"] == 1
    assert evaluation.report is not None
    (judged,) = evaluation.report.claims
    first, second = judged.requests
    assert first.sent_vt == sent and str(first.settled_by) == "attested_not_committed"
    assert second.settled_by is None and str(judged.knowledge) == "committed"


def test_in_b0_there_is_no_gateway_so_the_resolver_never_acts(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("res_f2_c0_b0")
    inproc = run(dsn, "F2", None, Contract.C0, "B0")
    assert inproc.resolver is None
    assert attestations(dsn) == []
    evaluation = inproc.result.evaluation
    assert evaluation is not None and evaluation.metrics["human_interventions"] == 0
    failing = [number for number, verdict in verdicts(evaluation)[1] if verdict == "fail"]
    assert (str(evaluation.verdict), failing) == ("fail", [1, 2, 3]), "as the cell without one"


def test_two_runs_with_the_resolver_give_byte_identical_logs(
    run_database: Callable[[str], str],
) -> None:
    """0009: the same inputs give byte-identical logs, ledgers and results, wall-clock
    columns aside. The resolver adds actor-log rows and gateway events of its own."""
    texts = []
    for name in ("res_twice_a", "res_twice_b"):
        dsn = run_database(name)
        inproc = run(dsn, "F2", None, Contract.C0, "G2")
        evaluation = inproc.result.evaluation
        assert evaluation is not None
        records = {
            "messages": rows(
                dsn,
                "SELECT seq, hop, kind, sender, receiver, api, correlation, body, sent_vt, fate,"
                " drop_reason, delivered_seq, delivered_vt FROM harness.message_log ORDER BY seq",
            ),
            "actors": rows(
                dsn, "SELECT seq, actor, action, subject, details, vt FROM harness.actor_log"
            ),
            "events": rows(
                dsn,
                "SELECT seq, action_id, kind, from_state, to_state, reason, details, vt"
                " FROM gw.action_event ORDER BY seq",
            ),
            "escalations": rows(dsn, "SELECT * FROM gw.escalation"),
            "effects": effects(dsn),
            "result": result_json(evaluation),
        }
        texts.append(json.dumps(records, sort_keys=True, default=str))
    assert texts[0] == texts[1]
    assert '"human_attested"' in texts[0]
