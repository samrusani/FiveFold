"""Demos 2a, 2b and 3 of record 0006, end to end from a seed (M3).

Demo 2 runs on dev-002, dev-001 with a human approval. A human approves the order, and
before it is sent the quote's price goes up (2a, F7) or the approval is revoked (2b, F6):
G2 refuses to send and names what went stale, and B0 orders anyway. Demo 3 runs dev-001 at
C0 with the order's reply lost and no resolver: G2 reports the outcome as unknown, and B0
orders twice. Demo 1 has its own tests (`test_demo_1.py`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from fivefold.harness.demo import DEMO_BY_KEY, Summary, main, summarize
from fivefold.harness.run import Config, InprocRun

pytestmark = pytest.mark.pg


def run(dsn: str, key: str, config: Config) -> tuple[Summary, InprocRun]:
    inproc = InprocRun(DEMO_BY_KEY[key].spec(config, 7), dsn)
    asyncio.run(inproc.execute())
    return summarize(dsn, config, ""), inproc


def verdict(inproc: InprocRun) -> tuple[str, tuple[int, ...]]:
    evaluation = inproc.result.evaluation
    assert evaluation is not None
    failing = tuple(r.property for r in evaluation.properties if str(r.verdict) == "fail")
    return str(evaluation.verdict), failing


def test_demo_2a_g2_names_the_stale_fact_and_orders_at_the_new_price(
    run_database: Callable[[str], str],
) -> None:
    summary, inproc = run(run_database("demo_2a_g2"), "2a", "G2")
    first, second = summary.gateway
    assert first.startswith("A-0001 rejected (stale_input), approval APR-0001")
    assert "stale: quote/S-1/W-100 version 1 to 2 (unit_price_minor)" in first
    assert second == "A-0002 confirmed (committed), approval APR-0002"
    assert summary.effects == [("PO-0001", "APR-0002")]
    assert summary.budget["committed_minor"] == 20_000 + 100 * 567, "the new price"
    assert verdict(inproc) == ("pass", ())


def test_demo_2a_b0_orders_at_the_old_price(run_database: Callable[[str], str]) -> None:
    summary, inproc = run(run_database("demo_2a_b0"), "2a", "B0")
    assert summary.effects == [("PO-0001", "APR-0001")]
    assert summary.budget["committed_minor"] == 20_000 + 100 * 540, "the old price"
    assert verdict(inproc) == ("fail", (3,))


def test_demo_2b_g2_refuses_to_send_on_the_revoked_approval(
    run_database: Callable[[str], str],
) -> None:
    summary, inproc = run(run_database("demo_2b_g2"), "2b", "G2")
    assert summary.gateway == ("A-0001 rejected (approval_revoked), approval APR-0001",)
    assert summary.effects == []
    assert [c["status"] for c in summary.claims] == ["not_done"]
    assert verdict(inproc) == ("pass", ()), "property 4 excuses it: authority withdrawn"


def test_demo_2b_b0_orders_anyway(run_database: Callable[[str], str]) -> None:
    summary, inproc = run(run_database("demo_2b_b0"), "2b", "B0")
    assert summary.effects == [("PO-0001", "APR-0001")]
    assert verdict(inproc) == ("fail", (2,))


def test_demo_3_g2_reports_unknown_and_what_to_reconcile(
    run_database: Callable[[str], str],
) -> None:
    summary, inproc = run(run_database("demo_3_g2"), "3", "G2")
    action, told = summary.gateway
    assert action == "A-0001 outcome_unknown (escalated), approval APR-0001"
    assert told.startswith("told the agent to reconcile: escalated_to_human (ESC-0001)")
    assert [c["status"] for c in summary.claims] == ["unknown"]
    assert summary.effects == [("PO-0001", "APR-0001")], "it did commit; only a human can say"
    assert verdict(inproc) == ("pass_unresolved", ())


def test_demo_3_b0_orders_twice(run_database: Callable[[str], str]) -> None:
    summary, inproc = run(run_database("demo_3_b0"), "3", "B0")
    assert summary.effects == [("PO-0001", "APR-0001"), ("PO-0002", "APR-0001")]
    assert verdict(inproc) == ("fail", (1, 2, 3))


def test_an_unknown_demo_is_refused_before_anything_runs() -> None:
    with pytest.raises(SystemExit):
        main(["4"])
