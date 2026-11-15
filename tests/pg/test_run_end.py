"""What the ledger shows of a write still in flight when the drain window ends (0005, 0009).

A request not committed within D of its arrival never commits (0005). Every simulator
call first aborts the writes in flight past their deadline; when the drain ends, the
harness has the simulator do the same once more, before it records the run's stop, so the
ledger shows that such a write never committed. A write whose deadline a fault waived (F5
beyond the deadline, a contract violation) is not aborted: it may still commit.

No fault in the catalog leaves a write past its deadline without a waiver, because F5 in
contract commits before D. The first test therefore holds the order with a stand-in for
the simulator's fault point, as a destination whose commit never comes would.
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
from fivefold.harness.sim_points import SimulatorPoints
from fivefold.harness.transport import Envelope
from fivefold.sim.endpoint import WriteDelay

pytestmark = pytest.mark.pg

SEED = 7
FOREVER_MS = 10**9
"""A hold far longer than any run."""


class _HeldPoints(SimulatorPoints):
    """The first order reaches the simulator and is held for good, with its deadline."""

    held = False

    def write_received(self, request: Envelope) -> WriteDelay | None:
        if self.held:
            return super().write_received(request)
        self.held = True
        return WriteDelay(FOREVER_MS, waive_deadline=False)


class _HeldRun(InprocRun):
    """A run whose destination never commits the first order it receives."""

    def _simulator_points(self) -> SimulatorPoints:
        params = self.spec.scenario.params
        return _HeldPoints(
            self.scheduler,
            self.faults,
            self.log,
            dispatch_timeout_ms=params.dispatch_timeout_ms,
            status_lag_ms=params.status_lag_ms,
        )


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


WRITES: LiteralString = (
    "SELECT state, outcome_vt, deadline_vt, deadline_waived FROM sim.write ORDER BY received_seq"
)
ABORTS: LiteralString = "SELECT vt FROM sim.event WHERE kind = 'write_aborted' ORDER BY seq"
STOP: LiteralString = "SELECT stopped_vt FROM harness.run_stop"


def test_a_write_still_in_flight_past_its_deadline_is_aborted_when_the_drain_ends(
    run_database: Callable[[str], str],
) -> None:
    """G2 at C0: after the timeout the action is escalated, so nothing calls the simulator
    again before the run stops, and only the harness's last call aborts the order."""
    dsn = run_database("end_held_order")
    spec = RunSpec(load_scenario_file(DEV_001), "G2", Contract.C0, SEED, plan=[])
    asyncio.run(_HeldRun(spec, dsn).execute())
    ((stopped_vt,),) = rows(dsn, STOP)
    ((state, outcome_vt, deadline_vt, waived),) = rows(dsn, WRITES)
    assert deadline_vt < stopped_vt and not waived
    assert (state, outcome_vt) == ("aborted", stopped_vt)
    assert rows(dsn, ABORTS) == [(stopped_vt,)]
    assert rows(dsn, "SELECT effect_id FROM sim.effect") == []


def test_a_write_whose_deadline_f5_waived_is_not_aborted_when_the_drain_ends(
    run_database: Callable[[str], str],
) -> None:
    """F5 beyond the deadline, with a delay longer than the run: the order is still in
    flight at the stop, past its deadline, and stays so."""
    plan = [
        {
            "fault": "F5",
            "variant": "beyond_deadline",
            "at": {
                "point": "sim.write.received",
                "match": {"api": "submit_purchase_order"},
                "occurrence": 1,
            },
            "params": {"delay_ms": FOREVER_MS},
        }
    ]
    dsn = run_database("end_waived_order")
    spec = RunSpec(load_scenario_file(DEV_001), "G2", Contract.C0, SEED, plan=plan)
    asyncio.run(InprocRun(spec, dsn).execute())
    ((stopped_vt,),) = rows(dsn, STOP)
    ((state, outcome_vt, deadline_vt, waived),) = rows(dsn, WRITES)
    assert deadline_vt < stopped_vt and waived
    assert (state, outcome_vt) == ("received", None)
    assert rows(dsn, ABORTS) == []
