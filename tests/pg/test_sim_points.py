"""F5 at the simulator's endpoint, against the real simulator (0005, 0006).

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

from fivefold.domain.contracts import Contract
from fivefold.faults.plan import FaultInjector, parse_plan
from fivefold.harness.log import MemoryHarnessLog, MessageRecord
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.sim_points import SimulatorPoints
from fivefold.harness.transport import Reply, Transport, TransportTimeout
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


class Caller:
    """The gateway's side: requests at chosen virtual times, each waiting at most T."""

    def __init__(self, sim: Simulator, plan: list[dict[str, Any]]) -> None:
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
        points = SimulatorPoints(self.scheduler, faults, self.log)
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
