"""F5 at the simulator's fault points: what fires, and how it shapes the answer
(0005, 0006). The endpoint that applies these decisions to the real simulator is tested
in `tests/pg/test_sim_points.py`."""

from __future__ import annotations

from typing import Any

import pytest

from fivefold.faults.plan import FaultInjector, parse_plan
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.sim_points import SimulatorPointError, SimulatorPoints
from fivefold.harness.transport import Envelope
from fivefold.sim.endpoint import WriteDelay


class Rig:
    def __init__(self, plan: list[dict[str, Any]]) -> None:
        self.scheduler = Scheduler()
        self.log = MemoryHarnessLog()
        self.points = SimulatorPoints(self.scheduler, FaultInjector(parse_plan(plan)), self.log)
        self.seq = 0

    def request(self, api: str, *, vt: int) -> Envelope:
        self.scheduler.clock.advance_to(vt)
        self.seq += 10
        return Envelope(
            seq=self.seq,
            hop="gw_sim",
            kind="request",
            sender="gateway",
            receiver="simulator",
            api=api,
            correlation=None,
            intent_id="REQ-1",
            idempotency_key=None,
            answer_class=None,
            body={"intent_id": "REQ-1"},
            sent_vt=vt,
        )

    def write(self, *, vt: int, api: str = "submit_purchase_order") -> WriteDelay | None:
        return self.points.write_received(self.request(api, vt=vt))


def f5(variant: str | None = None, **params: int) -> dict[str, Any]:
    return {
        "fault": "F5",
        "variant": variant,
        "at": {"point": "sim.write.received", "match": {"api": "submit_purchase_order"}},
        "params": params,
    }


def test_nothing_fires_without_a_plan() -> None:
    rig = Rig([])
    assert rig.write(vt=10) is None
    assert rig.log.faults == []


def test_f5_in_contract_holds_the_write_for_its_delay() -> None:
    rig = Rig([f5()])
    assert rig.write(vt=10, api="reserve_inventory") is None, "the match names the order"
    assert rig.write(vt=40) == WriteDelay(5_000, waive_deadline=False)
    assert rig.write(vt=50) is None, "the fault fires once, at its occurrence"
    (fault,) = rig.log.faults
    assert (fault.fault, fault.variant, fault.point, fault.action, fault.vt) == (
        "F5",
        None,
        "sim.write.received",
        "delay_commit",
        40,
    )
    assert (fault.target_seq, fault.component, fault.params) == (
        20,
        "simulator",
        {"delay_ms": 5_000},
    )


def test_f5_beyond_the_deadline_waives_it() -> None:
    rig = Rig([f5("beyond_deadline", delay_ms=20_000)])
    assert rig.write(vt=10) == WriteDelay(20_000, waive_deadline=True)
    assert rig.log.faults[0].params == {"delay_ms": 20_000}


def test_a_fault_with_nothing_to_do_at_a_simulator_point_is_refused() -> None:
    """Only F5 acts where writes arrive; a broken plan fails loudly instead of firing
    nothing (the catalog already refuses such plans)."""
    rig = Rig([])
    planned = parse_plan([{"fault": "F2", "at": {"point": "net.gw_sim.answer"}}])
    wrong = planned[0].model_copy(
        update={"at": planned[0].at.model_copy(update={"point": "sim.write.received"})}
    )
    rig.points = SimulatorPoints(rig.scheduler, FaultInjector([wrong]), rig.log)
    with pytest.raises(SimulatorPointError, match=r"F2 cannot act at sim\.write\.received"):
        rig.write(vt=10)
