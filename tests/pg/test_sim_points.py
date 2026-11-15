"""F5 and F10 at the simulator's endpoint, against the real simulator (0005, 0006).

A caller sends requests through the transport to the simulator's endpoint, which asks
`SimulatorPoints` what fires. The simulator's own records show what happened: when a
held write was received and committed, against its deadline, and what each status
answer listed and claimed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, LiteralString

import psycopg
import pytest

from fivefold.domain.api import GetOrderStatus, StatusAnswer, SubmitPurchaseOrder, WriteCommitted
from fivefold.domain.contracts import Contract
from fivefold.faults.plan import FaultInjector, parse_plan
from fivefold.harness.log import MemoryHarnessLog, MessageRecord
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.sim_points import SimulatorPoints
from fivefold.harness.transport import Envelope, Reply, Transport, TransportTimeout
from fivefold.sim import Simulator
from fivefold.sim.endpoint import endpoint

pytestmark = pytest.mark.pg

SimFactory = Callable[..., Simulator]
T_MS = 2_000
ORDER: dict[str, Any] = {
    "intent_id": "REQ-1",
    "approval_id": "APR-0001",
    "basis": {"quote/S-1/W-100": 1, "budget/CC-OPS": 1},
    "idempotency_key": "A-0001",
    "supplier_id": "S-1",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 540,
    "currency": "USD",
    "cost_center": "CC-OPS",
}
STATUS = {"intent_id": "REQ-1"}


class Caller:
    """The gateway's side: requests at chosen virtual times, each waiting at most T."""

    def __init__(self, sim: Simulator, plan: list[dict[str, Any]], *, lag_ms: int = 0) -> None:
        self.scheduler = Scheduler()
        self.log = MemoryHarnessLog()
        faults = FaultInjector(parse_plan(plan))
        self.transport = Transport(
            self.scheduler,
            self.log,
            Generators(3).get("latency"),
            max_hop_latency_ms=50,
            faults=faults,
        )
        points = SimulatorPoints(
            self.scheduler, faults, self.log, dispatch_timeout_ms=T_MS, status_lag_ms=lag_ms
        )
        self.transport.serve("simulator", endpoint(sim, self.scheduler, points=points))
        self.outcomes: dict[str, Reply | None] = {}
        self._calls: list[tuple[int, str, str, dict[str, Any]]] = []

    def at(self, vt: int, label: str, api: str, body: dict[str, Any]) -> None:
        self._calls.append((vt, label, api, body))

    async def _call(self, label: str, api: str, body: dict[str, Any]) -> None:
        try:
            self.outcomes[label] = await self.transport.call(
                hop="gw_sim",
                sender="gateway",
                receiver="simulator",
                api=api,
                body=body,
                timeout_ms=T_MS,
                intent_id="REQ-1",
                idempotency_key=body.get("idempotency_key"),
            )
        except TransportTimeout:
            self.outcomes[label] = None

    def _start(self, label: str, api: str, body: dict[str, Any]) -> Callable[[], None]:
        def start() -> None:
            self.scheduler.spawn("gateway", self._call(label, api, body), name=label)

        return start

    def run(self) -> None:
        async def main() -> None:
            for vt, label, api, body in self._calls:
                self.scheduler.call_at(vt, self._start(label, api, body))
            await self.scheduler.run()

        asyncio.run(main())

    def answers(self, api: str) -> list[MessageRecord]:
        return [m for m in self.log.messages if m.kind == "answer" and m.api == api]


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def f5(variant: str, delay_ms: int) -> dict[str, Any]:
    return {
        "fault": "F5",
        "variant": variant,
        "at": {"point": "sim.write.received", "match": {"api": "submit_purchase_order"}},
        "params": {"delay_ms": delay_ms},
    }


def f10(variant: str, count: int) -> dict[str, Any]:
    return {
        "fault": "F10",
        "variant": variant,
        "at": {"point": "sim.status.answer"},
        "params": {"count": count},
    }


WRITES: LiteralString = (
    "SELECT received_vt, state, outcome_vt, deadline_vt, deadline_waived, effect_id"
    " FROM sim.write ORDER BY received_seq"
)


def test_f5_in_contract_commits_after_the_callers_timeout_and_before_the_deadline(
    make_sim: SimFactory, sim_database: str
) -> None:
    """The write is in flight for 5,000 ms: a retry with its key meanwhile gets
    `in_progress` (C2), and the commit's answer comes too late for the caller."""
    caller = Caller(make_sim(Contract.C2), [f5("in_contract", 5_000)])
    caller.at(0, "order", "submit_purchase_order", ORDER)
    caller.at(3_000, "retry", "submit_purchase_order", ORDER)
    caller.run()
    first, retry = rows(sim_database, WRITES)
    arrived = first[0]
    assert first == (arrived, "committed", arrived + 5_000, arrived + 10_000, False, "PO-0001")
    assert retry[1] == "in_progress"
    assert caller.outcomes["order"] is None, "the caller timed out after T"
    retried = caller.outcomes["retry"]
    assert retried is not None and retried.answer_class == "in_progress"
    late = caller.answers("submit_purchase_order")[1]
    assert (late.answer_class, late.sent_vt, late.drop_reason) == (
        "committed",
        arrived + 5_000,
        "caller_timed_out",
    )
    (fault,) = caller.log.faults
    request = next(m for m in caller.log.messages if m.kind == "request")
    assert (fault.fault, fault.variant, fault.target_seq, fault.component, fault.vt) == (
        "F5",
        "in_contract",
        request.seq,
        "simulator",
        arrived,
    )


def test_f5_beyond_the_deadline_commits_after_d_and_is_never_aborted(
    make_sim: SimFactory, sim_database: str
) -> None:
    """A contract violation: every call aborts overdue writes first, but not this one."""
    caller = Caller(make_sim(Contract.C0), [f5("beyond_deadline", 20_000)])
    caller.at(0, "order", "submit_purchase_order", ORDER)
    caller.at(15_000, "read", "get_quote", {"supplier_id": "S-1", "sku": "W-100"})
    caller.run()
    ((arrived, state, outcome_vt, deadline_vt, waived, effect_id),) = rows(sim_database, WRITES)
    assert (state, outcome_vt, waived, effect_id) == (
        "committed",
        arrived + 20_000,
        True,
        "PO-0001",
    )
    assert outcome_vt > deadline_vt == arrived + 10_000
    assert rows(sim_database, "SELECT kind FROM sim.event WHERE kind = 'write_aborted'") == []


def test_f10_delayed_answers_arrive_after_the_callers_timeout(make_sim: SimFactory) -> None:
    caller = Caller(make_sim(Contract.C1), [f10("delayed", 2)])
    for n, vt in enumerate((0, 3_000, 6_000)):
        caller.at(vt, f"probe {n}", "get_order_status", STATUS)
    caller.run()
    assert [caller.outcomes[f"probe {n}"] is None for n in range(3)] == [True, True, False]
    answers = caller.answers("get_order_status")
    assert [(m.fate, m.drop_reason) for m in answers] == [
        ("dropped", "caller_timed_out"),
        ("dropped", "caller_timed_out"),
        ("delivered", None),
    ]
    requests = [m for m in caller.log.messages if m.kind == "request"]
    for request, answer in zip(requests[:2], answers[:2], strict=True):
        assert request.delivered_vt is not None
        assert answer.sent_vt == request.delivered_vt + T_MS, "held for T after it arrived"


def test_f10_stale_within_the_lag_is_accurate_to_l_before_it_is_produced(
    make_sim: SimFactory, dev_data: dict[str, Any]
) -> None:
    dev_data["params"]["status_lag_ms"] = 3_000
    caller = Caller(make_sim(Contract.C1), [f10("stale_within_lag", 2)], lag_ms=3_000)
    caller.at(5_000, "order", "submit_purchase_order", ORDER)
    caller.at(7_000, "early", "get_order_status", STATUS)
    caller.at(10_000, "late", "get_order_status", STATUS)
    caller.run()
    answers = {
        label: reply.body
        for label, reply in caller.outcomes.items()
        if reply is not None and label != "order"
    }
    early, late = answers["early"], answers["late"]
    assert isinstance(early, dict) and isinstance(late, dict)
    assert early["effects"] == [], "the order committed less than L before the answer"
    assert [e["effect_id"] for e in late["effects"]] == ["PO-0001"]
    requests = {m.seq: m for m in caller.log.messages if m.kind == "request"}
    for answer in caller.answers("get_order_status"):
        assert answer.correlation is not None and isinstance(answer.body, dict)
        produced = requests[answer.correlation].delivered_vt
        assert produced is not None
        assert answer.body["as_of_vt"] == produced - 3_000


def test_f10_stale_beyond_the_lag_lists_the_state_when_it_fired_but_claims_now(
    make_sim: SimFactory,
) -> None:
    """A contract violation (0006): F10 fires before the order commits, so its answers
    keep listing nothing while claiming to be accurate to the moment each is produced."""
    caller = Caller(make_sim(Contract.C1), [f10("stale_beyond_lag", 2)])
    caller.at(0, "first", "get_order_status", STATUS)
    caller.at(1_000, "order", "submit_purchase_order", ORDER)
    caller.at(3_000, "stale", "get_order_status", STATUS)
    caller.at(6_000, "fresh", "get_order_status", STATUS)
    caller.run()
    bodies: dict[str, Any] = {}
    for label in ("first", "stale", "fresh"):
        reply = caller.outcomes[label]
        assert reply is not None
        bodies[label] = reply.body
    requests = {m.seq: m for m in caller.log.messages if m.kind == "request"}
    stale_request = next(
        requests[m.correlation]
        for m in caller.answers("get_order_status")
        if m.correlation is not None and m.body == bodies["stale"]
    )
    assert bodies["stale"]["effects"] == []
    assert bodies["stale"]["as_of_vt"] == stale_request.delivered_vt
    assert [e["effect_id"] for e in bodies["fresh"]["effects"]] == ["PO-0001"]


def test_f10_stale_beyond_the_lag_answers_alike_when_an_order_commits_in_its_millisecond(
    make_sim: SimFactory,
) -> None:
    """Every answer F10 shapes lists the same effects, those committed before the virtual
    millisecond in which it fired. An order that commits later in that millisecond is in
    none of them, though each answer claims to be current (0006)."""
    sim = make_sim(Contract.C1)
    scheduler = Scheduler()
    faults = FaultInjector(parse_plan([f10("stale_beyond_lag", 2)]))
    points = SimulatorPoints(
        scheduler, faults, MemoryHarnessLog(), dispatch_timeout_ms=T_MS, status_lag_ms=0
    )

    def status(vt: int, seq: int) -> list[str]:
        scheduler.clock.advance_to(vt)
        request = Envelope(
            seq=seq,
            hop="gw_sim",
            kind="request",
            sender="gateway",
            receiver="simulator",
            api="get_order_status",
            correlation=None,
            intent_id="REQ-1",
            idempotency_key=None,
            answer_class=None,
            body=STATUS,
            sent_vt=vt,
        )
        shape = points.status_answer(request)
        assert shape is not None and shape.contract_violation
        answer = sim.get_order_status(
            GetOrderStatus(**STATUS),
            caller="gateway",
            vt=vt,
            as_of_vt=shape.as_of_vt,
            reflect_vt=shape.reflect_vt,
            contract_violation=True,
        )
        assert isinstance(answer, StatusAnswer)
        return [effect.effect_id for effect in answer.effects]

    first = status(5_000, 1)
    order = sim.submit_purchase_order(SubmitPurchaseOrder(**ORDER), caller="gateway", vt=5_000)
    assert isinstance(order, WriteCommitted) and order.effect_id == "PO-0001"
    assert status(6_000, 2) == first == [], "the order committed in F10's millisecond"
