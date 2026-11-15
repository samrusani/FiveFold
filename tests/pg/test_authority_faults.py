"""F12, F6 and F7 in whole runs at C2, B0 and G2 (0006; M2).

- F12 loses a write request before it reaches the destination.
- F6 revokes the approval: early, at the gateway's dispatch check or as B0's approval
  answer reaches it; late, as the write leaves for the simulator.
- F7 changes the quote the order relies on, at the same points: `price_up` breaks the
  exact rule, `harmless` changes only the lead time, which no rule reads (0004).

The outcomes asserted here are ground truth: the ledger and the approvals. Verdicts come
with the evaluator.
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

ORDER = {"api": "submit_purchase_order"}


def fault(name: str, point: str, match: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {"fault": name, "at": {"point": point, "match": match, "occurrence": 1}, **more}


def run(dsn: str, config: Config, plan: list[dict[str, Any]]) -> InprocRun:
    spec = RunSpec(load_scenario_file(DEV_001), config, Contract.C2, 7, plan=plan)
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    return inproc


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def effects(dsn: str) -> list[tuple[Any, ...]]:
    return rows(
        dsn,
        "SELECT effect_id, approval_id, args->>'unit_price_minor' FROM sim.effect"
        " ORDER BY commit_seq",
    )


def claim(dsn: str) -> tuple[str, list[str]]:
    ((report,),) = rows(dsn, "SELECT report FROM harness.final_report WHERE valid")
    (only,) = report["claims"]
    return only["status"], only["effect_ids"]


# F12 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("config", "hop"), [("G2", "gw_sim"), ("B0", "agent_sim")])
def test_a_lost_write_request_is_sent_again_and_commits_once(
    run_database: Callable[[str], str], config: Config, hop: str
) -> None:
    dsn = run_database(f"f12_{config.lower()}")
    run(dsn, config, [fault("F12", f"net.{hop}.request", ORDER)])
    assert effects(dsn) == [("PO-0001", "APR-0001", "540")]
    writes = rows(
        dsn,
        "SELECT fate, drop_reason FROM harness.message_log"
        " WHERE kind = 'request' AND api = 'submit_purchase_order' AND hop <> 'agent_gw'"
        " ORDER BY seq",
    )
    assert writes == [("dropped", "fault"), ("delivered", None)]
    assert claim(dsn) == ("done", ["PO-0001"])


# F6 --------------------------------------------------------------------------------------


def test_f6_early_in_g2_the_gateway_refuses_to_send(run_database: Callable[[str], str]) -> None:
    dsn = run_database("f6_early_g2")
    run(dsn, "G2", [fault("F6", "gw.dispatch.before_check", {})])
    assert effects(dsn) == []
    assert rows(dsn, "SELECT state, grant_id FROM gw.action") == [("rejected", "G-0001")]
    ((reason,),) = rows(dsn, "SELECT reason FROM gw.action_event WHERE to_state = 'rejected'")
    assert reason == "approval_revoked"
    assert rows(dsn, "SELECT action, subject FROM harness.actor_log") == [("revoke", "APR-0001")]
    assert claim(dsn) == ("not_done", [])


def test_f6_early_in_b0_the_agent_orders_on_a_revoked_approval(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("f6_early_b0")
    run(dsn, "B0", [fault("F6", "net.agent_sim.answer", {"api": "request_approval"})])
    assert effects(dsn) == [("PO-0001", "APR-0001", "540")]
    assert rows(dsn, "SELECT status FROM sim.approval") == [("revoked",)]


def test_f6_late_at_c2_the_order_commits_after_the_revocation(
    run_database: Callable[[str], str],
) -> None:
    """A documented G2 limit (0006): authority changed after the check, before the commit."""
    dsn = run_database("f6_late_g2")
    run(dsn, "G2", [fault("F6", "net.gw_sim.request", ORDER)])
    assert effects(dsn) == [("PO-0001", "APR-0001", "540")]
    assert rows(dsn, "SELECT status FROM sim.approval") == [("revoked",)]


# F7 --------------------------------------------------------------------------------------


def test_f7_early_price_up_in_g2_names_the_stale_fact_and_reorders_at_the_new_price(
    run_database: Callable[[str], str],
) -> None:
    """Demo 2a's G2 side (0006)."""
    dsn = run_database("f7_early_g2")
    run(dsn, "G2", [fault("F7", "gw.dispatch.before_check", {}, variant="price_up")])
    assert effects(dsn) == [("PO-0001", "APR-0002", "567")]
    rejected = rows(
        dsn,
        "SELECT details FROM gw.action_event WHERE to_state = 'rejected' ORDER BY seq",
    )
    ((details,),) = rejected
    assert details["stale"] == [
        {
            "key": "quote/S-1/W-100",
            "cited_version": 1,
            "current_version": 2,
            "changed_fields": ["unit_price_minor"],
        }
    ]
    assert claim(dsn) == ("done", ["PO-0001"])


def test_f7_early_price_up_in_b0_orders_at_the_old_price(
    run_database: Callable[[str], str],
) -> None:
    """Demo 2a's B0 side (0006)."""
    dsn = run_database("f7_early_b0")
    run(
        dsn,
        "B0",
        [fault("F7", "net.agent_sim.answer", {"api": "request_approval"}, variant="price_up")],
    )
    assert effects(dsn) == [("PO-0001", "APR-0001", "540")]
    ((price,),) = rows(
        dsn,
        "SELECT value->>'unit_price_minor' FROM sim.fact_version"
        " WHERE key = 'quote/S-1/W-100' ORDER BY version DESC LIMIT 1",
    )
    assert price == "567", "the quote moved while the order cited the old one"


def test_f7_harmless_changes_nothing_a_rule_reads(run_database: Callable[[str], str]) -> None:
    dsn = run_database("f7_harmless_g2")
    run(dsn, "G2", [fault("F7", "gw.dispatch.before_check", {}, variant="harmless")])
    assert effects(dsn) == [("PO-0001", "APR-0001", "540")]
    assert rows(dsn, "SELECT count(*) FROM gw.action") == [(1,)]
    ((lead,),) = rows(
        dsn,
        "SELECT value->>'lead_time_days' FROM sim.fact_version"
        " WHERE key = 'quote/S-1/W-100' ORDER BY version DESC LIMIT 1",
    )
    assert lead == "6"


def test_f12_on_the_gateway_hop_the_agent_sends_the_write_again(
    run_database: Callable[[str], str],
) -> None:
    """An MCP request lost on its way to the gateway: the tool times out and LangGraph
    retries it over the same connection."""
    dsn = run_database("f12_agent_gw")
    inproc = run(dsn, "G2", [fault("F12", "net.agent_gw.request", ORDER)])
    assert effects(dsn) == [("PO-0001", "APR-0001", "540")]
    calls = rows(
        dsn,
        "SELECT fate, drop_reason FROM harness.message_log WHERE hop = 'agent_gw'"
        " AND kind = 'request' AND api = 'submit_purchase_order' ORDER BY seq",
    )
    assert calls == [("dropped", "fault"), ("delivered", None)]
    assert claim(dsn) == ("done", ["PO-0001"])
    assert inproc.result.ends["agent-1"][0] == "finished"


def test_f12_on_the_mcp_handshake_ends_the_worker_with_an_error(
    run_database: Callable[[str], str],
) -> None:
    """A lost `initialize` request: the worker cannot connect, ends with an error and
    reports nothing; the run still ends and is scored."""
    dsn = run_database("f12_initialize")
    inproc = run(dsn, "G2", [fault("F12", "net.agent_gw.request", {"api": "initialize"})])
    ((end, why),) = inproc.result.ends.values()
    assert end == "error" and "initialize" in why
    assert effects(dsn) == []
    assert rows(dsn, "SELECT count(*) FROM harness.final_report") == [(0,)]
