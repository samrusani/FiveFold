"""F5 and F10 at the simulator's fault points: what fires, and how it shapes the answer
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
from fivefold.sim.endpoint import StatusShape, WriteDelay

T_MS = 2_000
LAG_MS = 3_000


class Rig:
    def __init__(self, plan: list[dict[str, Any]]) -> None:
        self.scheduler = Scheduler()
        self.log = MemoryHarnessLog()
        self.points = SimulatorPoints(
            self.scheduler,
            FaultInjector(parse_plan(plan)),
            self.log,
            dispatch_timeout_ms=T_MS,
            status_lag_ms=LAG_MS,
        )
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

    def status(self, *, vt: int) -> StatusShape | None:
        return self.points.status_answer(self.request("get_order_status", vt=vt))


def f5(variant: str | None = None, **params: int) -> dict[str, Any]:
    return {
        "fault": "F5",
        "variant": variant,
        "at": {"point": "sim.write.received", "match": {"api": "submit_purchase_order"}},
        "params": params,
    }


def f10(variant: str, occurrence: int = 1, **params: int) -> dict[str, Any]:
    return {
        "fault": "F10",
        "variant": variant,
        "at": {"point": "sim.status.answer", "occurrence": occurrence},
        "params": params,
    }


def test_nothing_fires_without_a_plan() -> None:
    rig = Rig([])
    assert (rig.write(vt=10), rig.status(vt=20)) == (None, None)
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


def test_two_f5_entries_at_one_write_are_refused() -> None:
    """A write is held once. Without this, the second entry would be recorded with a delay
    the write never had."""
    rig = Rig([f5(), f5("beyond_deadline", delay_ms=20_000)])
    with pytest.raises(SimulatorPointError, match=r"2 F5 entries fired at one write"):
        rig.write(vt=10)
    assert rig.log.faults == [], "neither is recorded"


def test_f10_delayed_holds_the_next_count_answers_for_t() -> None:
    rig = Rig([f10("delayed", count=3)])
    shapes = [rig.status(vt=vt) for vt in (1_000, 4_000, 7_000, 10_000)]
    assert shapes == [StatusShape(hold_ms=T_MS)] * 3 + [None]
    (fault,) = rig.log.faults
    assert (fault.variant, fault.vt, fault.target_seq, fault.params) == (
        "delayed",
        1_000,
        10,
        {"count": 3},
    )


def test_f10_stale_within_the_lag_answers_as_of_l_before_and_says_so() -> None:
    rig = Rig([f10("stale_within_lag", count=2)])
    assert rig.status(vt=2_000) == StatusShape(as_of_vt=0), "never before the run started"
    assert rig.status(vt=9_000) == StatusShape(as_of_vt=9_000 - LAG_MS)
    assert rig.status(vt=12_000) is None


def test_f10_stale_beyond_the_lag_reflects_the_state_when_it_fired() -> None:
    """The answers list the effects as of the moment F10 fired, but claim to be accurate
    to the time each is produced: a contract violation (0006)."""
    rig = Rig([f10("stale_beyond_lag", occurrence=2, count=2)])
    assert rig.status(vt=1_000) is None, "it fires at the second status answer"
    expected = StatusShape(reflect_vt=4_999, contract_violation=True)
    assert [rig.status(vt=vt) for vt in (5_000, 8_000, 11_000)] == [expected, expected, None]
    assert rig.log.faults[0].vt == 5_000


def test_overlapping_f10s_each_count_the_answer_and_the_first_shapes_it() -> None:
    rig = Rig([f10("delayed", count=2), f10("stale_beyond_lag", occurrence=2, count=2)])
    shapes = [rig.status(vt=vt) for vt in (1_000, 2_000, 3_000, 4_000)]
    assert shapes == [
        StatusShape(hold_ms=T_MS),
        StatusShape(hold_ms=T_MS),
        StatusShape(reflect_vt=1_999, contract_violation=True),
        None,
    ]
    assert [(f.variant, f.vt) for f in rig.log.faults] == [
        ("delayed", 1_000),
        ("stale_beyond_lag", 2_000),
    ]


def test_a_fault_with_nothing_to_do_at_a_simulator_point_is_refused() -> None:
    """Only F5 acts where writes arrive; a broken plan fails loudly instead of firing
    nothing (the catalog already refuses such plans)."""
    rig = Rig([])
    planned = parse_plan([{"fault": "F2", "at": {"point": "net.gw_sim.answer"}}])
    wrong = planned[0].model_copy(
        update={"at": planned[0].at.model_copy(update={"point": "sim.write.received"})}
    )
    rig.points = SimulatorPoints(
        rig.scheduler,
        FaultInjector([wrong]),
        rig.log,
        dispatch_timeout_ms=T_MS,
        status_lag_ms=LAG_MS,
    )
    with pytest.raises(SimulatorPointError, match=r"F2 cannot act at sim\.write\.received"):
        rig.write(vt=10)
