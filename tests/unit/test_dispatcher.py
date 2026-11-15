"""The dispatcher drives actions through their states on the scheduler (0002, 0003, 0005).

A stand-in destination keeps a ledger with idempotency keys, as a C2 destination does,
and can lose answers. Every test checks the action's events, so the order of steps and
their virtual times are pinned.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.gateway.destination import ApprovalNow, FactNow, Unreachable
from fivefold.gateway.dispatcher import Dispatcher, Timing
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.records import (
    Action,
    ActionResult,
    Answer,
    ApprovalStatus,
    Attempt,
    BasisCheck,
    ProbeAnswer,
    WriteCall,
)
from fivefold.gateway.state_machine import (
    FINAL_STATES,
    ActionState,
    AnswerClass,
    GatewaySettings,
    Note,
    Reason,
)
from fivefold.harness.scheduler import Scheduler

S = ActionState
SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
)
HOP = 20
"""The stand-in's one-way latency, in virtual ms."""


@dataclass
class World:
    """The approvals service and a C2 destination with an idempotent ledger."""

    scheduler: Scheduler
    policy: ApprovalStatus = ApprovalStatus.APPROVED
    ttl_ms: int = 10**9
    approvals: dict[str, ApprovalStatus] = field(default_factory=dict)
    expires: dict[str, int] = field(default_factory=dict)
    ledger: dict[str, str] = field(default_factory=dict)
    lose_answers: int = 0
    lose_requests: int = 0
    unreachable_checks: int = 0
    stale: bool = False

    async def facts(self, keys: Sequence[str]) -> Mapping[str, FactNow]:
        await self.scheduler.sleep(2 * HOP)
        if self.unreachable_checks:
            self.unreachable_checks -= 1
            raise Unreachable("no answer")
        return {key: FactNow(key, 1, {}) for key in keys}

    async def request_approval(self, action: Action) -> ApprovalNow:
        await self.scheduler.sleep(2 * HOP)
        approval_id = f"APR-{action.action_id[2:]}"
        if approval_id not in self.approvals:
            self.approvals[approval_id] = self.policy
            if self.policy is ApprovalStatus.APPROVED:
                self.expires[approval_id] = self.scheduler.now + self.ttl_ms
        return self._now(approval_id)

    async def approval(self, approval_id: str) -> ApprovalNow:
        await self.scheduler.sleep(2 * HOP)
        return self._now(approval_id)

    def approve(self, approval_id: str) -> None:
        self.approvals[approval_id] = ApprovalStatus.APPROVED
        self.expires[approval_id] = self.scheduler.now + self.ttl_ms

    def _now(self, approval_id: str) -> ApprovalNow:
        status = self.approvals[approval_id]
        expires = self.expires.get(approval_id) if status is ApprovalStatus.APPROVED else None
        return ApprovalNow(approval_id, status, expires)

    async def send(self, action: Action, attempt: Attempt, approval_id: str) -> Answer:
        assert approval_id == f"APR-{action.action_id[2:]}", "the send cites the grant's approval"
        if self.lose_requests:
            self.lose_requests -= 1
            await self.scheduler.sleep(SETTINGS.dispatch_timeout_ms)
            return Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)
        await self.scheduler.sleep(HOP)
        effect = self.ledger.get(attempt.idempotency_key)
        if effect is None:
            effect = f"PO-{len(self.ledger) + 1:04d}"
            self.ledger[attempt.idempotency_key] = effect
        if self.lose_answers:
            self.lose_answers -= 1
            await self.scheduler.sleep(SETTINGS.dispatch_timeout_ms - HOP)
            return Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)
        await self.scheduler.sleep(HOP)
        return Answer(AnswerClass.COMMITTED, Reason.COMMITTED, effect)

    async def lookup(self, action: Action) -> ProbeAnswer:
        """An honest status answer: every committed effect, accurate to now (L is 0)."""
        await self.scheduler.sleep(2 * HOP)
        effects = tuple(self.ledger.values())
        return ProbeAnswer(answered=True, effect_ids=effects, as_of_vt=self.scheduler.now)


class Fresh:
    def __init__(self, world: World) -> None:
        self._world = world

    def keys(self, action: Action) -> Sequence[str]:
        return ["quote/S-1/W-100", "budget/CC-OPS"]

    def check(self, action: Action, now: Mapping[str, FactNow], *, full: bool) -> BasisCheck:
        if self._world.stale:
            return BasisCheck(False, False if full else None)
        return BasisCheck(True, True if full else None)


class Run:
    def __init__(self, contract: Contract = Contract.C2) -> None:
        self.scheduler = Scheduler()
        self.world = World(self.scheduler)
        self.store = MemoryStore(
            task_id="TASK-1",
            intents={"REQ-1": "submit_purchase_order"},
            contract=contract,
            settings=SETTINGS,
        )
        self.dispatcher = Dispatcher(
            self.scheduler, self.store, self.world, Fresh(self.world), timing=Timing()
        )

    def propose(self) -> str:
        call = WriteCall(
            "REQ-1", "submit_purchase_order", "supplier/S-1", {"quantity": 100}, "k", (), ()
        )
        result = self.store.propose(call, self.scheduler.now)
        assert isinstance(result, ActionResult)
        self.dispatcher.drive(result.action_id)
        return result.action_id

    def go(self, *, until_ms: int = 60_000, setup: object = None) -> str:
        holder: list[str] = []

        async def main() -> None:
            holder.append(self.propose())
            await self.scheduler.run(deadline_ms=until_ms)

        asyncio.run(main())
        return holder[0]

    def reasons(self, action_id: str) -> list[str]:
        return [str(e.reason) for e in self.store.view(action_id).events]


def test_an_auto_approved_action_is_sent_once_and_confirmed() -> None:
    run = Run()
    action_id = run.go()
    assert run.reasons(action_id) == [
        "admitted",
        "approval_requested",
        "approved",
        "dispatched",
        "committed",
    ]
    view = run.store.view(action_id)
    assert (view.action.state, view.action.effect_id) == (S.CONFIRMED, "PO-0001")
    assert len(view.attempts) == 1 and run.world.ledger == {action_id: "PO-0001"}


def test_a_lost_answer_is_recovered_by_a_retry_with_the_same_key_at_c2() -> None:
    """Demo 1's gateway side: the reply is lost, the retry gets the stored result."""
    run = Run()
    run.world.lose_answers = 1
    action_id = run.go()
    view = run.store.view(action_id)
    assert view.action.state is S.CONFIRMED and view.action.effect_id == "PO-0001"
    assert [t.idempotency_key for t in view.attempts] == [action_id, action_id]
    assert run.reasons(action_id)[-4:] == ["dispatched", "timeout", "retry", "committed"]
    first, second = view.attempts
    assert second.sent_vt - first.sent_vt >= SETTINGS.dispatch_timeout_ms
    assert run.world.ledger == {action_id: "PO-0001"}, "one effect, whatever the retries"


def test_a_pending_approval_is_polled_until_a_human_approves() -> None:
    run = Run()
    run.world.policy = ApprovalStatus.PENDING

    async def main() -> str:
        action_id = run.propose()
        run.scheduler.call_at(3_000, lambda: run.world.approve(f"APR-{action_id[2:]}"))
        await run.scheduler.run(deadline_ms=60_000)
        return action_id

    action_id = asyncio.run(main())
    assert run.store.view(action_id).action.state is S.CONFIRMED
    approved = next(e for e in run.store.view(action_id).events if e.reason is Reason.APPROVED)
    assert 3_000 <= approved.vt <= 3_000 + 500 + 2 * HOP * 3


def test_an_approval_with_no_decision_times_out() -> None:
    run = Run()
    run.world.policy = ApprovalStatus.PENDING
    action_id = run.go()
    view = run.store.view(action_id)
    assert (view.action.state, run.reasons(action_id)[-1]) == (S.REJECTED, "approval_timeout")
    assert view.events[-1].vt >= view.events[1].vt + SETTINGS.approval_wait_ms


def test_a_stale_basis_is_rejected_before_any_approval_request() -> None:
    run = Run()
    run.world.stale = True
    action_id = run.go()
    assert run.reasons(action_id) == ["admitted", "stale_input"]
    assert run.world.approvals == {}


def test_an_unreachable_check_is_tried_again_and_changes_nothing() -> None:
    run = Run()
    run.world.unreachable_checks = 2
    action_id = run.go()
    assert run.store.view(action_id).action.state is S.CONFIRMED
    assert run.reasons(action_id)[0:2] == ["admitted", "approval_requested"]


def test_c0_escalates_a_lost_answer_and_stops() -> None:
    run = Run(Contract.C0)
    run.world.lose_answers = 1
    action_id = run.go()
    view = run.store.view(action_id)
    assert view.action.state is S.OUTCOME_UNKNOWN and view.escalation is not None
    assert len(view.attempts) == 1, "C0 never retries"
    assert run.reasons(action_id)[-1] == str(Note.ESCALATED)


def test_c1_probes_and_finds_the_effect_whose_answer_was_lost() -> None:
    run = Run(Contract.C1)
    run.world.lose_answers = 1
    action_id = run.go()
    view = run.store.view(action_id)
    assert view.action.state is S.CONFIRMED and run.reasons(action_id)[-1] == "status_found"
    assert view.action.effect_id == "PO-0001"
    assert len(view.attempts) == 1, "C1 never retries"


def test_c1_settles_a_lost_request_as_failed_only_after_the_settle_time() -> None:
    run = Run(Contract.C1)
    run.world.lose_requests = 1
    action_id = run.go()
    view = run.store.view(action_id)
    assert view.action.state is S.FAILED
    assert run.reasons(action_id)[-1] == "status_settled_empty"
    settled = view.events[-1]
    assert settled.vt >= view.attempts[0].sent_vt + SETTINGS.settle_ms
    assert run.world.ledger == {}


def test_a_restart_recovers_an_open_attempt_and_retries() -> None:
    run = Run()

    async def main() -> str:
        action_id = run.propose()
        await run.scheduler.run(until=lambda: _state(run, action_id) is S.DISPATCHED)
        # The gateway crashes with the attempt in flight: its memory is gone.
        run.scheduler.crash("gateway", restart_after_ms=1_000, restart=run.dispatcher.start)
        await run.scheduler.run(deadline_ms=60_000)
        return action_id

    action_id = asyncio.run(main())
    view = run.store.view(action_id)
    assert view.action.state is S.CONFIRMED
    assert "recovered_open_attempt" in run.reasons(action_id)
    assert run.world.ledger == {action_id: "PO-0001"}


def test_wait_returns_at_the_wanted_state_or_when_time_is_up() -> None:
    run = Run()
    run.world.policy = ApprovalStatus.PENDING
    seen: list[tuple[int, ActionState]] = []

    async def waiter(action_id: str) -> None:
        await run.dispatcher.wait(action_id, lambda s: s is S.PENDING_APPROVAL, 2_500)
        seen.append((run.scheduler.now, _state(run, action_id)))
        await run.dispatcher.wait(action_id, lambda s: s in FINAL_STATES, 1_000)
        seen.append((run.scheduler.now, _state(run, action_id)))

    async def main() -> None:
        action_id = run.propose()
        run.scheduler.spawn("gateway", waiter(action_id), name="waiter")
        await run.scheduler.run(deadline_ms=10_000)

    asyncio.run(main())
    (first_vt, first), (second_vt, second) = seen
    assert first is S.PENDING_APPROVAL and first_vt < 2_500
    assert second is S.PENDING_APPROVAL and second_vt == first_vt + 1_000


def _state(run: Run, action_id: str) -> ActionState:
    return run.store.view(action_id).action.state


def _pending_then(run: Run, decide: Callable[[str], None], at: int) -> str:
    """Propose with approval pending; at `at`, decide and make every fact read fail."""
    run.world.policy = ApprovalStatus.PENDING

    def later(action_id: str) -> None:
        decide(f"APR-{action_id[2:]}")
        run.world.unreachable_checks = 10**6

    async def main() -> str:
        action_id = run.propose()
        run.scheduler.call_at(at, lambda: later(action_id))
        await run.scheduler.run(deadline_ms=60_000)
        return action_id

    return asyncio.run(main())


def test_a_denial_is_recorded_even_when_facts_cannot_be_read() -> None:
    run = Run()
    action_id = _pending_then(
        run,
        lambda approval_id: run.world.approvals.__setitem__(approval_id, ApprovalStatus.DENIED),
        3_000,
    )
    view = run.store.view(action_id)
    assert (view.action.state, run.reasons(action_id)[-1]) == (S.REJECTED, "approval_denied")
    assert view.events[-1].vt < 3_000 + 500 + 4 * HOP, "at the next poll, not at the timeout"


def test_the_approval_timeout_needs_no_fact_read() -> None:
    run = Run()
    action_id = _pending_then(run, lambda approval_id: None, 1_000)
    view = run.store.view(action_id)
    assert (view.action.state, run.reasons(action_id)[-1]) == (S.REJECTED, "approval_timeout")
