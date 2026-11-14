"""Messages between components: latency, order, fates, drop rules and faults (0006, 0009)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from fivefold.faults.plan import FaultInjector, parse_plan
from fivefold.harness.log import MemoryHarnessLog, MessageRecord
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Envelope, Reply, Transport, TransportTimeout


class Rig:
    """A scheduler, a transport and a simulator stand-in that echoes requests."""

    def __init__(self, seed: int = 1, plan: list[dict[str, object]] | None = None) -> None:
        self.scheduler = Scheduler()
        self.log = MemoryHarnessLog()
        self.transport = Transport(
            self.scheduler,
            self.log,
            Generators(seed).get("latency"),
            max_hop_latency_ms=50,
            faults=FaultInjector(parse_plan(plan or [])),
        )
        self.delay_ms = 0
        self.transport.serve("sim", self.handle)

    async def handle(self, request: Envelope) -> Reply:
        if self.delay_ms:
            await self.scheduler.sleep(self.delay_ms)
        body = request.body
        assert isinstance(body, dict)
        return Reply({"echo": body["n"]}, answer_class=str(body.get("class", "committed")))

    def call(
        self, n: int, *, timeout_ms: int = 2_000, sender: str = "gateway"
    ) -> asyncio.Future[Reply]:
        return asyncio.ensure_future(
            self.transport.call(
                hop="gw_sim",
                sender=sender,
                receiver="sim",
                api="submit_purchase_order",
                body={"n": n},
                timeout_ms=timeout_ms,
                intent_id="REQ-1",
                idempotency_key=f"A-000{n}",
            )
        )

    def go(self, body: Callable[[], object]) -> None:
        async def main() -> None:
            body()
            await self.scheduler.run()

        asyncio.run(main())


def fates(log: MemoryHarnessLog) -> list[tuple[str, str | None, str | None]]:
    return [(m.kind, m.fate, m.drop_reason) for m in log.messages]


def test_a_call_carries_a_request_and_its_answer_with_seeded_latency() -> None:
    rig = Rig()
    results: list[Reply] = []

    async def caller() -> None:
        results.append(await rig.call(1))

    rig.go(lambda: rig.scheduler.spawn("gateway", caller(), name="call"))
    assert results == [Reply({"echo": 1}, "committed")]
    request, answer = rig.log.messages
    assert (request.kind, answer.kind, answer.correlation) == ("request", "answer", request.seq)
    assert (request.api, request.intent_id, request.idempotency_key) == (
        "submit_purchase_order",
        "REQ-1",
        "A-0001",
    )
    for message in (request, answer):
        assert message.fate == "delivered" and message.delivered_vt is not None
        assert 5 <= message.delivered_vt - message.sent_vt <= 50
        assert message.delivered_seq is not None and message.delivered_seq > message.seq
    assert rig.scheduler.now == answer.delivered_vt


def test_messages_arrive_in_order_of_arrival_time_then_send_order() -> None:
    rig = Rig(seed=3)
    arrivals: list[tuple[int, int]] = []

    def send_all() -> None:
        for n in range(20):
            rig.transport.post(
                hop="agent_sim",
                kind="notification",
                sender="agent-1",
                receiver="sim",
                body={"n": n},
                on_arrival=lambda e: arrivals.append((rig.scheduler.now, e.seq)),
            )

    rig.go(send_all)
    assert arrivals == sorted(arrivals)
    assert len({vt for vt, _ in arrivals}) > 1, "latencies should vary with the generator"


def test_the_same_seed_gives_the_same_log_and_another_seed_a_different_one() -> None:
    def history(seed: int) -> list[MessageRecord]:
        rig = Rig(seed=seed)

        async def caller(n: int) -> None:
            await rig.call(n)

        rig.go(lambda: [rig.scheduler.spawn("gateway", caller(n), name=f"c{n}") for n in range(5)])
        return rig.log.messages

    assert history(11) == history(11)
    assert history(11) != history(12)


def test_a_late_answer_is_dropped_after_the_caller_times_out() -> None:
    rig = Rig()
    rig.delay_ms = 5_000
    outcome: list[str] = []

    async def caller() -> None:
        try:
            await rig.call(1, timeout_ms=2_000)
        except TransportTimeout:
            outcome.append(f"timeout at {rig.scheduler.now}")

    rig.go(lambda: rig.scheduler.spawn("gateway", caller(), name="call"))
    assert outcome == ["timeout at 2000"]
    assert fates(rig.log) == [
        ("request", "delivered", None),
        ("answer", "dropped", "caller_timed_out"),
    ]


def test_a_component_that_is_down_sends_nothing() -> None:
    rig = Rig()
    sent: list[object] = []

    def start() -> None:
        rig.scheduler.crash("agent-1")
        sent.append(
            rig.transport.post(
                hop="agent_gw",
                kind="notification",
                sender="agent-1",
                receiver="gateway",
                body={},
                on_arrival=lambda e: None,
            )
        )

    rig.go(start)
    assert sent == [None] and rig.log.messages == []


def test_messages_to_a_component_that_is_down_are_dropped() -> None:
    rig = Rig()
    outcome: list[str] = []

    async def caller() -> None:
        try:
            await rig.call(1, timeout_ms=1_000)
        except TransportTimeout:
            outcome.append("timeout")

    def start() -> None:
        rig.scheduler.crash("sim")
        rig.scheduler.spawn("gateway", caller(), name="call")

    rig.go(start)
    assert outcome == ["timeout"]
    assert fates(rig.log) == [("request", "dropped", "receiver_down")]


def test_answers_to_requests_sent_before_a_crash_are_dropped_after_the_restart() -> None:
    """0010, change 3: a crashed component never gets answers to its earlier requests."""
    rig = Rig()
    rig.delay_ms = 3_000
    restarted: list[int] = []

    async def caller() -> None:
        await rig.call(1, timeout_ms=60_000, sender="agent-1")

    def start() -> None:
        rig.scheduler.spawn("agent-1", caller(), name="call")
        rig.scheduler.call_at(
            1_000,
            lambda: rig.scheduler.crash(
                "agent-1", restart_after_ms=10, restart=lambda: restarted.append(rig.scheduler.now)
            ),
        )

    rig.go(start)
    assert restarted == [1_010]
    assert fates(rig.log) == [
        ("request", "delivered", None),
        ("answer", "dropped", "caller_crashed"),
    ]


def test_a_receiver_crash_cancels_its_work_and_the_caller_times_out() -> None:
    rig = Rig()
    rig.delay_ms = 3_000
    outcome: list[str] = []

    async def caller() -> None:
        try:
            await rig.call(1, timeout_ms=5_000)
        except TransportTimeout:
            outcome.append(f"timeout at {rig.scheduler.now}")

    def start() -> None:
        rig.scheduler.spawn("gateway", caller(), name="call")
        rig.scheduler.call_at(1_000, lambda: rig.scheduler.crash("sim"))

    rig.go(start)
    assert outcome == ["timeout at 5000"]
    assert fates(rig.log) == [("request", "delivered", None)]


def test_f2_drops_the_committed_answer_and_the_fault_is_logged() -> None:
    plan: list[dict[str, object]] = [
        {
            "fault": "F2",
            "at": {
                "point": "net.gw_sim.answer",
                "match": {"api": "submit_purchase_order", "answer": "committed"},
                "occurrence": 2,
            },
        }
    ]
    rig = Rig(plan=plan)
    outcomes: list[str] = []

    async def caller(n: int) -> None:
        try:
            reply = await rig.call(n, timeout_ms=2_000)
            outcomes.append(f"{n}: {reply.answer_class}")
        except TransportTimeout:
            outcomes.append(f"{n}: timeout")

    async def sequence() -> None:
        for n in (1, 2, 3):
            await caller(n)

    rig.go(lambda: rig.scheduler.spawn("gateway", sequence(), name="calls"))
    assert outcomes == ["1: committed", "2: timeout", "3: committed"]
    dropped = [m for m in rig.log.messages if m.fate == "dropped"]
    assert [(m.kind, m.drop_reason) for m in dropped] == [("answer", "fault")]
    (fault,) = rig.log.faults
    assert (fault.fault, fault.point, fault.occurrence, fault.action, fault.target_seq) == (
        "F2",
        "net.gw_sim.answer",
        2,
        "drop",
        dropped[0].seq,
    )


def test_a_rejected_answer_does_not_match_f2() -> None:
    plan: list[dict[str, object]] = [
        {"fault": "F2", "at": {"point": "net.gw_sim.answer", "match": {"answer": "committed"}}}
    ]
    rig = Rig(plan=plan)
    answers: list[str | None] = []

    async def caller() -> None:
        reply = await rig.transport.call(
            hop="gw_sim",
            sender="gateway",
            receiver="sim",
            api="submit_purchase_order",
            body={"n": 1, "class": "rejected"},
            timeout_ms=2_000,
        )
        answers.append(reply.answer_class)

    rig.go(lambda: rig.scheduler.spawn("gateway", caller(), name="call"))
    assert answers == ["rejected"] and rig.log.faults == []


def test_calls_need_a_registered_receiver_and_valid_settings() -> None:
    rig = Rig()

    async def caller() -> None:
        with pytest.raises(ValueError, match="serves no requests"):
            await rig.transport.call(
                hop="gw_sim", sender="gateway", receiver="nobody", api="x", body={}, timeout_ms=1
            )

    rig.go(lambda: rig.scheduler.spawn("gateway", caller(), name="call"))
    with pytest.raises(ValueError, match="already has a handler"):
        rig.transport.serve("sim", rig.handle)
    with pytest.raises(ValueError, match="max_hop_latency_ms"):
        Transport(Scheduler(), MemoryHarnessLog(), Generators(1).get("x"), max_hop_latency_ms=4)
