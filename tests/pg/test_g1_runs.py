"""G1, authorization only, in whole runs of dev-001 (0008).

G1 runs the same agent and scripted policy as G2, with the same tools and schemas; only
the gateway's behaviour differs. Each write call is a new action with its own approval
request, and the gateway checks the approval, and no fact, before its one send. A send
with no answer fails the call with `gateway_timeout`, so the framework's retry places
the order again. The final report goes to the harness's report sink unchanged.

Each test checks the records the run leaves, which are ground truth, and the verdict the
run gets; `faults/expected.yaml` holds the verdict of every G1 fault cell.
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
from fivefold.harness.run import InprocRun, RunSpec

pytestmark = pytest.mark.pg

ORDER = {"api": "submit_purchase_order"}


def run(dsn: str, contract: Contract, plan: list[dict[str, Any]]) -> InprocRun:
    spec = RunSpec(load_scenario_file(DEV_001), "G1", contract, 7, plan=plan)
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


def actions(dsn: str) -> list[tuple[Any, ...]]:
    return rows(
        dsn,
        "SELECT action_id, identity, state, effect_id FROM gw.action"
        " ORDER BY substring(action_id FROM 3)::bigint",
    )


def orders_sent(dsn: str) -> list[tuple[Any, ...]]:
    """The order requests on each hop: the agent's calls and the gateway's sends."""
    return rows(
        dsn,
        "SELECT hop, fate, idempotency_key, body->>'idempotency_key' FROM harness.message_log"
        " WHERE kind = 'request' AND api = 'submit_purchase_order' ORDER BY seq",
    )


def verdict(inproc: InprocRun) -> tuple[str, tuple[int, ...]]:
    evaluation = inproc.result.evaluation
    assert evaluation is not None
    failing = tuple(p.property for p in evaluation.properties if str(p.verdict) == "fail")
    return str(evaluation.verdict), failing


def test_with_no_fault_g1_places_one_order_on_one_approval(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("g1_one_order")
    inproc = run(dsn, Contract.C2, [])
    assert effects(dsn) == [("PO-0001", "APR-0001", None)], "G1 sends no idempotency key"
    assert actions(dsn) == [("A-0001", False, "confirmed", "PO-0001")]
    assert rows(
        dsn, "SELECT check_kind, check_approval, check_basis IS NULL, answer_class FROM gw.attempt"
    ) == [("authority_only", "approved", True, "committed")]
    assert rows(dsn, "SELECT reason FROM gw.action_event ORDER BY seq") == [
        ("admitted",),
        ("approval_requested",),
        ("approved",),
        ("dispatched",),
        ("committed",),
    ]
    reads = rows(
        dsn,
        "SELECT hop, count(*) FROM harness.message_log WHERE kind = 'request'"
        " AND api IN ('get_quote', 'get_budget') GROUP BY hop ORDER BY hop",
    )
    assert reads == [("agent_gw", 3), ("gw_sim", 3)], "the gateway read only what the agent did"
    assert rows(dsn, "SELECT run_closed FROM gw.run_state") == [(False,)], "G1 never closes it"
    assert inproc.result.ends["agent-1"][0] == "finished"
    assert verdict(inproc) == ("pass", ())


def test_a_lost_reply_makes_the_agents_retry_place_a_second_order_on_a_new_approval(
    run_database: Callable[[str], str],
) -> None:
    """F2 at C2: the committed answer is dropped. G1's call fails with `gateway_timeout`,
    the framework retries it, and the retry is a new action with a new approval. G2
    would retry with the same key and get the stored result."""
    dsn = run_database("g1_lost_reply")
    plan = [
        {
            "fault": "F2",
            "at": {
                "point": "net.gw_sim.answer",
                "match": {"api": "submit_purchase_order", "answer": "committed"},
                "occurrence": 1,
            },
        }
    ]
    inproc = run(dsn, Contract.C2, plan)
    assert effects(dsn) == [("PO-0001", "APR-0001", None), ("PO-0002", "APR-0002", None)]
    assert actions(dsn) == [
        ("A-0001", False, "outcome_unknown", None),
        ("A-0002", False, "confirmed", "PO-0002"),
    ]
    assert rows(dsn, "SELECT count(*) FROM gw.escalation") == [(0,)]
    assert orders_sent(dsn) == [
        ("agent_gw", "delivered", None, None),
        ("gw_sim", "delivered", None, None),
        ("agent_gw", "delivered", None, None),
        ("gw_sim", "delivered", None, None),
    ], "two calls from the agent, two sends, no keys, no retry by the gateway"
    first_answer = rows(
        dsn,
        "SELECT answer_class, body->'result'->'content'->0->>'text' FROM harness.message_log"
        " WHERE kind = 'answer' AND hop = 'agent_gw' AND api = 'submit_purchase_order'"
        " ORDER BY seq LIMIT 1",
    )
    ((answer_class, text),) = first_answer
    assert answer_class == "error" and '"error": "gateway_timeout"' in text
    assert inproc.result.ends["agent-1"][0] == "finished"
    assert verdict(inproc) == ("fail", (1, 3))


def test_the_report_is_forwarded_unchanged_and_unchecked(
    run_database: Callable[[str], str],
) -> None:
    """F1a at C2: the gateway crashes after its dispatch check and, after the restart,
    sends that order from its outbox, while the agent's retry places another. The
    report names only the second order, which contradicts the gateway's own records;
    G2 would refuse it (`report_inconsistent`). G1 forwards it as the agent sent it."""
    dsn = run_database("g1_report")
    plan = [
        {
            "fault": "F1",
            "variant": "a",
            "at": {"point": "gw.dispatch.after_check", "match": ORDER, "occurrence": 1},
        }
    ]
    inproc = run(dsn, Contract.C2, plan)
    assert actions(dsn) == [
        ("A-0001", False, "confirmed", "PO-0001"),
        ("A-0002", False, "confirmed", "PO-0002"),
    ]
    ((report, valid, accepted_by, sent),) = rows(
        dsn,
        "SELECT r.report, r.valid, r.accepted_by, m.body->'params'->'arguments'"
        " FROM harness.final_report r JOIN harness.message_log m ON m.seq = r.message_seq",
    )
    assert report == sent, "the report as the agent sent it"
    assert [(c["status"], c["effect_ids"]) for c in report["claims"]] == [("done", ["PO-0002"])]
    assert (valid, accepted_by) == (True, "gateway")
    assert rows(dsn, "SELECT count(*) FROM gw.report") == [(0,)]
    assert rows(dsn, "SELECT count(*) FROM gw.tool_call WHERE tool = 'submit_final_report'") == [
        (0,)
    ]
    assert verdict(inproc) == ("fail", (1, 3, 5))


def test_a_g1_run_from_a_seed_is_byte_identical(run_database: Callable[[str], str]) -> None:
    """The determinism contract (0009), on a G1 run with a crash, a restart and a retry."""
    plan = [
        {
            "fault": "F1",
            "variant": "a",
            "at": {"point": "gw.dispatch.after_check", "match": ORDER, "occurrence": 1},
        }
    ]
    first, second = run_database("g1_seed_1"), run_database("g1_seed_2")
    run(first, Contract.C1, plan)
    run(second, Contract.C1, plan)
    queries: list[LiteralString] = [
        "SELECT seq, hop, kind, sender, receiver, api, correlation, intent_id, idempotency_key,"
        " answer_class, body, sent_vt, fate, drop_reason, delivered_seq, delivered_vt"
        " FROM harness.message_log ORDER BY seq",
        "SELECT effect_id, approval_id, idempotency_key, commit_seq, committed_vt FROM sim.effect"
        " ORDER BY commit_seq",
        "SELECT seq, action_id, kind, from_state, to_state, reason, details, vt"
        " FROM gw.action_event ORDER BY seq",
        "SELECT seq, worker, report, valid, accepted_by, message_seq, vt FROM harness.final_report"
        " ORDER BY seq",
    ]
    for query in queries:
        assert rows(first, query) == rows(second, query), query
