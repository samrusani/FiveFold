"""Two crash faults that fire at one event leave one live copy of the component (0006).

A crash stops a component and restarts it after `restart_delay_ms` (0006, Crashes). When
two planned kills match the same event, the component crashes twice at that moment. The
records do not say how two such crashes combine; the harness never runs two copies of one
component: each restart belongs to the crash that queued it, and only the latest crash's
restart brings the component back (`fivefold.harness.scheduler`). A plan whose two kills
have the very same trigger is refused before the run (`fivefold.faults.plan`).

The plans here are demo 1's faults, F2 on the order's answer and F11 after the order
call, restarted after 1,200 ms, plus a second F11 that matches the same call through
another field and restarts after 1,000 ms, so the plan check lets it through. Before the
fix the worker resumed twice, 200 ms apart, and both copies ran on one checkpoint thread:
in G2 they sent the order call three times and two final reports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any, LiteralString

import psycopg
import pytest

from fivefold.agents.worker import WorkerEnd
from fivefold.harness.demo import demo_1_plan, demo_1_spec
from fivefold.harness.run import Config, InprocRun

pytestmark = pytest.mark.pg


SECOND_KILL: dict[str, Any] = {
    "fault": "F11",
    "at": {
        "point": "agent.after_tool_call",
        "match": {"api": "submit_purchase_order"},
        "occurrence": 1,
    },
    "params": {"restart_delay_ms": 1_000},
}
"""A kill at the order call, as demo 1's F11 is, matched on `api` instead of `tool`."""


def two_kills(config: Config) -> list[dict[str, Any]]:
    """Demo 1's plan, its F11 restarting after 1,200 ms, and `SECOND_KILL`."""
    lost_reply, kill = demo_1_plan(config)
    return [lost_reply, {**kill, "params": {"restart_delay_ms": 1_200}}, SECOND_KILL]


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


@pytest.mark.parametrize("config", ["B0", "G2"])
def test_two_kills_at_one_tool_call_resume_the_worker_once(
    run_database: Callable[[str], str], monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    resumed: list[tuple[str, int]] = []
    start_worker = InprocRun._start_worker

    def counted(self: InprocRun, worker: str, *, resume: bool) -> None:
        if resume:
            resumed.append((worker, self.scheduler.now))
        start_worker(self, worker, resume=resume)

    monkeypatch.setattr(InprocRun, "_start_worker", counted)
    dsn = run_database(f"two_kills_{config.lower()}")
    spec = replace(demo_1_spec(config, 7), plan=two_kills(config))
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    kills = rows(dsn, "SELECT fault, vt FROM harness.fault_log WHERE action = 'kill' ORDER BY seq")
    assert [fault for fault, _ in kills] == ["F11", "F11"], "both kills fired"
    ((_, killed_at), _) = kills
    assert resumed == [("agent-1", killed_at + 1_000)], "the latest kill's restart, once"
    assert inproc.result.ends["agent-1"][0] is WorkerEnd.FINISHED
    orders = rows(
        dsn,
        "SELECT count(*) FROM harness.message_log WHERE kind = 'request'"
        " AND sender = 'agent-1' AND api = 'submit_purchase_order'",
    )
    assert orders == [(2,)], "the order call, and the one the resumed worker sends again"
    reports = rows(dsn, "SELECT count(*) FROM harness.final_report WHERE worker = 'agent-1'")
    assert reports == [(1,)]


def test_two_kills_with_the_same_trigger_are_refused_before_the_run(
    run_database: Callable[[str], str],
) -> None:
    """The second kill names demo 1's very trigger, so it can only fire with the first."""
    lost_reply, kill = demo_1_plan("G2")
    again = {**kill, "params": {"restart_delay_ms": 1_000}}
    spec = replace(demo_1_spec("G2", 7), plan=[lost_reply, kill, again])
    with pytest.raises(ValueError, match="F11 and F11 crash the same component"):
        InprocRun(spec, run_database("two_kills_refused"))
