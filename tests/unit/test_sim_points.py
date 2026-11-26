"""F5 and F10 at the simulator's fault points: what fires, and how it shapes the answer
(0005, 0006). The endpoint that applies these decisions to the real simulator is tested
in `tests/pg/test_sim_points.py`."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import pytest

from fivefold.faults.plan import FaultInjector, PlannedFault, parse_plan
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.sim_points import SimulatorPointError, SimulatorPoints
from fivefold.harness.transport import Envelope
from fivefold.sim.endpoint import StatusShape, WriteDelay

T_MS = 2_000
LAG_MS = 3_000


class Rig:
    def __init__(self, plan: list[dict[str, Any]], *, planned: Sequence[PlannedFault] = ()) -> None:
        """`planned` adds entries parsed already, such as one the catalog would refuse."""
        self.scheduler = Scheduler()
        self.log = MemoryHarnessLog()
        self.points = SimulatorPoints(
            self.scheduler,
            FaultInjector([*parse_plan(plan), *planned]),
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

    def answered(self, request: Envelope, *, intent_id: str = "REQ-1") -> Envelope:
        """The status answer to `request`, as the transport sends it: a message of its own,
        which the points see through their observer."""
        answer = Envelope(
            seq=request.seq + 1,
            hop="gw_sim",
            kind="answer",
            sender="simulator",
            receiver="gateway",
            api=request.api,
            correlation=request.seq,
            intent_id=intent_id,
            idempotency_key=None,
            answer_class=None,
            body={"intent_id": intent_id},
            sent_vt=self.scheduler.now,
        )
        self.points.answer_sent(answer, "sent")
        return answer


def f5(variant: str | None = None, **params: int) -> dict[str, Any]:
    return {
        "fault": "F5",
        "variant": variant,
        "at": {"point": "sim.write.received", "match": {"api": "submit_purchase_order"}},
        "params": params,
    }


def f10(variant: str, occurrence: int = 1, **params: int) -> dict[str, Any]:
    """F10 `delayed` or `stale_within_lag`, at a status answer."""
    return {
        "fault": "F10",
        "variant": variant,
        "at": {"point": "sim.status.answer", "occurrence": occurrence},
        "params": params,
    }


def f10_beyond(occurrence: int = 1, **params: int) -> dict[str, Any]:
    """F10 `stale_beyond_lag`, where the order reaches the simulator (0013)."""
    return {
        "fault": "F10",
        "variant": "stale_beyond_lag",
        "at": {
            "point": "sim.write.received",
            "match": {"api": "submit_purchase_order"},
            "occurrence": occurrence,
        },
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
    rig = Rig(
        [],
        planned=[
            PlannedFault.model_validate(entry)
            for entry in (f5(), f5("beyond_deadline", delay_ms=20_000))
        ],
    )
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


def test_f10_stale_beyond_the_lag_freezes_the_state_before_the_write_it_fired_at() -> None:
    """It fires as the order reaches the destination (0013). The next `count` answers list
    the effects committed before the millisecond the order arrived in, but claim to be
    accurate to the time each is produced: a contract violation (0006)."""
    rig = Rig([f10_beyond(count=2)])
    assert rig.status(vt=1_000) is None, "nothing fired yet"
    assert rig.write(vt=4_000, api="reserve_inventory") is None, "the match names the order"
    assert rig.write(vt=5_000) is None, "F10 holds no write"
    expected = StatusShape(reflect_vt=4_999, contract_violation=True)
    assert [rig.status(vt=vt) for vt in (8_000, 11_000, 14_000)] == [expected, expected, None]
    (fault,) = rig.log.faults
    assert (fault.fault, fault.variant, fault.point, fault.action, fault.vt) == (
        "F10",
        "stale_beyond_lag",
        "sim.write.received",
        "degrade_status",
        5_000,
    )
    assert (fault.target_seq, fault.component, fault.params) == (30, "simulator", {"count": 2})


def test_f10_stale_beyond_the_lag_fires_at_the_nth_write_it_matches() -> None:
    rig = Rig([f10_beyond(occurrence=2, count=1)])
    assert rig.write(vt=2_000) is None
    assert rig.status(vt=3_000) is None, "the first order is not the one"
    assert rig.write(vt=6_000) is None
    assert rig.status(vt=7_000) == StatusShape(reflect_vt=5_999, contract_violation=True)
    assert rig.status(vt=8_000) is None


def test_overlapping_f10s_each_count_the_answer_and_the_first_shapes_it() -> None:
    """An F10 that fired where a write arrived counts the same answers as one that fired
    at a status answer."""
    rig = Rig([f10("delayed", count=2), f10_beyond(count=2)])
    first = rig.status(vt=1_000)
    assert rig.write(vt=1_500) is None
    shapes = [first, *(rig.status(vt=vt) for vt in (2_000, 3_000, 4_000))]
    assert shapes == [
        StatusShape(hold_ms=T_MS),
        StatusShape(hold_ms=T_MS),
        StatusShape(reflect_vt=1_499, contract_violation=True),
        None,
    ]
    assert [(f.variant, f.point, f.vt) for f in rig.log.faults] == [
        ("delayed", "sim.status.answer", 1_000),
        ("stale_beyond_lag", "sim.write.received", 1_500),
    ]


def test_f5_and_f10_both_act_at_one_write_in_plan_order() -> None:
    """F5 holds the order, and F10 freezes the state from before it arrived."""
    rig = Rig([f10_beyond(count=1), f5()])
    assert rig.write(vt=40) == WriteDelay(5_000, waive_deadline=False)
    assert [(f.fault, f.point, f.vt, f.params) for f in rig.log.faults] == [
        ("F10", "sim.write.received", 40, {"count": 1}),
        ("F5", "sim.write.received", 40, {"delay_ms": 5_000}),
    ]
    assert rig.status(vt=3_000) == StatusShape(reflect_vt=39, contract_violation=True)
    assert rig.status(vt=6_000) is None


@pytest.mark.parametrize(
    ("variant", "point"),
    [
        ("stale_beyond_lag", "sim.status.answer"),
        ("delayed", "sim.write.received"),
        ("stale_within_lag", "sim.write.received"),
    ],
)
def test_an_f10_variant_away_from_its_point_is_refused(variant: str, point: str) -> None:
    """The catalog refuses such a plan (0013). One that got past it stops the run: stale
    beyond L at a status answer would freeze the state there, as it did before 0013."""
    home = "sim.write.received" if variant == "stale_beyond_lag" else "sim.status.answer"
    (planned,) = parse_plan([{"fault": "F10", "variant": variant, "at": {"point": home}}])
    wrong = planned.model_copy(update={"at": planned.at.model_copy(update={"point": point})})
    rig = Rig([], planned=[wrong])
    with pytest.raises(
        SimulatorPointError, match=re.escape(f"F10 {variant} cannot act at {point}")
    ):
        if point == "sim.write.received":
            rig.write(vt=10)
        else:
            rig.status(vt=10)
    assert rig.log.faults == [], "nothing is recorded"


def test_a_fault_with_nothing_to_do_at_a_simulator_point_is_refused() -> None:
    """Only F5 and F10 stale beyond L act where writes arrive; a broken plan fails loudly
    instead of firing nothing (the catalog already refuses such plans)."""
    planned = parse_plan([{"fault": "F2", "at": {"point": "net.gw_sim.answer"}}])
    wrong = planned[0].model_copy(
        update={"at": planned[0].at.model_copy(update={"point": "sim.write.received"})}
    )
    rig = Rig([], planned=[wrong])
    with pytest.raises(SimulatorPointError, match=r"F2 cannot act at sim\.write\.received"):
        rig.write(vt=10)


# The intents faults touch (0014) -----------------------------------------------------------


def test_f5_and_f10_name_the_intent_of_the_request_they_acted_on() -> None:
    rig = Rig([f10_beyond(count=1), f5(), f10("delayed", count=1)])
    rig.write(vt=10)
    rig.status(vt=20)
    assert [(f.fault, f.variant, f.point, f.intent_id) for f in rig.log.faults] == [
        ("F10", "stale_beyond_lag", "sim.write.received", "REQ-1"),
        ("F5", None, "sim.write.received", "REQ-1"),
        ("F10", "delayed", "sim.status.answer", "REQ-1"),
    ]


@pytest.mark.parametrize("variant", ["delayed", "stale_within_lag", "stale_beyond_lag"])
def test_f10_logs_each_status_answer_it_changes_once_the_answer_is_sent(variant: str) -> None:
    """Whichever variant, and wherever it fired, F10 acts on the next `count` status
    answers, which can concern several intents. Each answer it changed gets one more row,
    after the row written when F10 fired: at the status answer point, naming the answer and
    its intent (0014). Stale beyond L fires where the order arrives (0013)."""
    beyond = variant == "stale_beyond_lag"
    rig = Rig([f10_beyond(count=2) if beyond else f10(variant, count=2)])
    if beyond:
        order = rig.request("submit_purchase_order", vt=500)
        assert rig.points.write_received(order) is None
    first = rig.request("get_order_status", vt=1_000)
    assert rig.points.status_answer(first) is not None
    fired = (
        (500, order.seq, "sim.write.received")
        if beyond
        else (1_000, first.seq, "sim.status.answer")
    )
    rig.scheduler.clock.advance_to(3_000)
    answer = rig.answered(first)
    rig.points.answer_sent(answer, "delivered")
    second = rig.request("get_order_status", vt=4_000)
    assert rig.points.status_answer(second) is not None
    other = rig.answered(second, intent_id="REQ-2")
    third = rig.request("get_order_status", vt=7_000)
    assert rig.points.status_answer(third) is None, "F10 changed two answers"
    rig.answered(third)
    assert [(f.vt, f.target_seq, f.point, f.intent_id, f.params) for f in rig.log.faults] == [
        (*fired, "REQ-1", {"count": 2}),
        (3_000, answer.seq, "sim.status.answer", "REQ-1", {"count": 2, "answer_no": 1}),
        (4_000, other.seq, "sim.status.answer", "REQ-2", {"count": 2, "answer_no": 2}),
    ]
    assert {(f.fault, f.variant, f.action) for f in rig.log.faults} == {
        ("F10", variant, "degrade_status")
    }


def test_an_answer_f10_shapes_is_logged_for_the_f10_that_shapes_it() -> None:
    """Two F10 entries overlap: both count the second answer, and only the first, which
    shapes it, logs it. The later one never shapes an answer, so it has its own row only."""
    rig = Rig([f10("delayed", count=2), f10_beyond(count=1)])
    request = rig.request("get_order_status", vt=1_000)
    rig.points.status_answer(request)
    rig.answered(request)
    assert rig.write(vt=1_500) is None
    request = rig.request("get_order_status", vt=2_000)
    rig.points.status_answer(request)
    rig.answered(request)
    assert rig.status(vt=3_000) is None
    assert [(f.variant, f.point, f.params) for f in rig.log.faults] == [
        ("delayed", "sim.status.answer", {"count": 2}),
        ("delayed", "sim.status.answer", {"count": 2, "answer_no": 1}),
        ("stale_beyond_lag", "sim.write.received", {"count": 1}),
        ("delayed", "sim.status.answer", {"count": 2, "answer_no": 2}),
    ]
