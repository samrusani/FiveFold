"""The dispatcher drives G1's actions on the scheduler (0008, "G1: authorization only").

A stand-in world counts what the gateway asks it: fact reads, approval calls, sends and
status lookups. G1 files one approval request per action, asks for the approval once
more before its one send, and reads no fact. After a lost answer it does nothing at any
contract level. After a crash it restarts through startup recovery, as every gateway
does (0006), without contract-aware recovery (0003, 0008). The last test checks the
request the simulator client puts on the wire.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pytest

from fivefold.domain.api import ApiName
from fivefold.domain.contracts import Contract, ContractParams
from fivefold.faults.plan import FaultInjector, Fired, parse_plan
from fivefold.gateway.destination import ApprovalNow, FactNow
from fivefold.gateway.dispatcher import Dispatcher, FaultHandler, Timing
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
from fivefold.gateway.sim_client import SimClient
from fivefold.gateway.state_machine import (
    ActionState,
    AnswerClass,
    CheckKind,
    GatewaySettings,
    Reason,
)
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Envelope, Reply, Transport

S = ActionState
SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
)
HOP = 20
LEVELS = [Contract.C0, Contract.C1, Contract.C2, Contract.C3]


@dataclass
class World:
    """The approvals service and a destination with no idempotency: each send commits."""

    scheduler: Scheduler
    policy: ApprovalStatus = ApprovalStatus.APPROVED
    approvals: dict[str, ApprovalStatus] = field(default_factory=dict)
    ledger: list[str] = field(default_factory=list)
    lose_answers: int = 0
    fact_reads: int = 0
    approval_checks: int = 0
    lookups: int = 0

    async def facts(self, keys: Sequence[str]) -> Mapping[str, FactNow]:
        self.fact_reads += 1
        await self.scheduler.sleep(2 * HOP)
        return {key: FactNow(key, 1, {}) for key in keys}

    async def request_approval(self, action: Action) -> ApprovalNow:
        await self.scheduler.sleep(2 * HOP)
        approval_id = f"APR-{action.action_id[2:]}"
        self.approvals.setdefault(approval_id, self.policy)
        return self._now(approval_id)

    async def approval(self, action: Action, approval_id: str) -> ApprovalNow:
        self.approval_checks += 1
        await self.scheduler.sleep(2 * HOP)
        return self._now(approval_id)

    def _now(self, approval_id: str) -> ApprovalNow:
        status = self.approvals[approval_id]
        expires = 10**9 if status is ApprovalStatus.APPROVED else None
        return ApprovalNow(approval_id, status, expires)

    async def send(self, action: Action, attempt: Attempt, approval_id: str) -> Answer:
        assert approval_id == f"APR-{action.action_id[2:]}", "the send cites the grant's approval"
        await self.scheduler.sleep(HOP)
        effect = f"PO-{len(self.ledger) + 1:04d}"
        self.ledger.append(effect)
        if self.lose_answers:
            self.lose_answers -= 1
            await self.scheduler.sleep(SETTINGS.dispatch_timeout_ms - HOP)
            return Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)
        await self.scheduler.sleep(HOP)
        return Answer(AnswerClass.COMMITTED, Reason.COMMITTED, effect)

    async def lookup(self, action: Action) -> ProbeAnswer:
        self.lookups += 1
        await self.scheduler.sleep(2 * HOP)
        return ProbeAnswer(
            answered=True, effect_ids=tuple(self.ledger), as_of_vt=self.scheduler.now
        )


class Checker:
    """A basis check that counts its calls. For G1 it finds the inputs stale, which would
    stop any G2 action; G1 must never ask it."""

    def __init__(self, *, stale: bool) -> None:
        self.stale = stale
        self.calls = 0

    def keys(self, action: Action) -> Sequence[str]:
        return ["quote/S-1/W-100", "budget/CC-OPS"]

    def check(self, action: Action, now: Mapping[str, FactNow], *, full: bool) -> BasisCheck:
        self.calls += 1
        fresh = not self.stale
        return BasisCheck(fresh, fresh if full else None)


class Run:
    def __init__(
        self,
        contract: Contract = Contract.C2,
        *,
        identity: bool = False,
        faults: FaultInjector | None = None,
        on_fault: FaultHandler | None = None,
    ) -> None:
        self.scheduler = Scheduler()
        self.world = World(self.scheduler)
        self.freshness = Checker(stale=not identity)
        self.store = MemoryStore(
            task_id="TASK-1",
            intents={"REQ-1": "submit_purchase_order"},
            contract=contract,
            settings=SETTINGS,
            identity=identity,
        )
        self.dispatcher = Dispatcher(
            self.scheduler,
            self.store,
            self.world,
            self.freshness,
            timing=Timing(),
            faults=faults,
            on_fault=on_fault,
        )

    def propose(self) -> str:
        call = WriteCall(
            "REQ-1", "submit_purchase_order", "supplier/S-1", {"quantity": 100}, "k", (), ()
        )
        result = self.store.propose(call, self.scheduler.now)
        assert isinstance(result, ActionResult)
        self.dispatcher.drive(result.action_id)
        return result.action_id

    def go(self, *, until_ms: int = 60_000) -> str:
        holder: list[str] = []

        async def main() -> None:
            holder.append(self.propose())
            await self.scheduler.run(deadline_ms=until_ms)

        asyncio.run(main())
        return holder[0]

    def reasons(self, action_id: str) -> list[str]:
        return [str(e.reason) for e in self.store.view(action_id).events]


def test_g1_sends_once_after_checking_the_approval_and_reads_no_fact() -> None:
    """The basis check would find the inputs stale; G1 never runs it (0008)."""
    run = Run()
    action_id = run.go()
    assert run.reasons(action_id) == [
        "admitted",
        "approval_requested",
        "approved",
        "dispatched",
        "committed",
    ]
    (attempt,) = run.store.view(action_id).attempts
    assert (attempt.check.kind, attempt.check.basis) == (CheckKind.AUTHORITY_ONLY, None)
    assert (run.world.fact_reads, run.freshness.calls) == (0, 0)
    assert run.world.approval_checks == 1, "the approval, asked for once before the send"
    assert run.world.ledger == ["PO-0001"]


@pytest.mark.parametrize("contract", LEVELS)
def test_after_a_lost_answer_g1_does_nothing_at_any_level(contract: Contract) -> None:
    """No probe at C1, no retry at C2 or C3, no escalation at C0 (0008)."""
    run = Run(contract)
    run.world.lose_answers = 1
    action_id = run.go()
    view = run.store.view(action_id)
    assert (view.action.state, view.escalation) == (S.OUTCOME_UNKNOWN, None)
    assert run.reasons(action_id)[-1] == "timeout"
    assert len(view.attempts) == 1 and run.world.lookups == 0
    assert run.world.ledger == ["PO-0001"], "the order committed; G1 cannot know"


def test_a_revocation_before_the_check_stops_the_send() -> None:
    def revoke(fired: Fired, point: str, context: Mapping[str, object]) -> None:
        run.world.approvals[str(context["approval_id"])] = ApprovalStatus.REVOKED

    plan = [{"fault": "F6", "variant": "early", "at": {"point": "gw.dispatch.before_check"}}]
    run = Run(faults=FaultInjector(parse_plan(plan)), on_fault=revoke)
    action_id = run.go()
    assert run.reasons(action_id)[-1] == "approval_revoked"
    assert run.store.view(action_id).action.state is S.REJECTED and run.world.ledger == []


def test_a_pending_g1_approval_is_polled_until_a_human_approves() -> None:
    run = Run()
    run.world.policy = ApprovalStatus.PENDING

    async def main() -> str:
        action_id = run.propose()
        approval_id = f"APR-{action_id[2:]}"
        run.scheduler.call_at(
            3_000, lambda: run.world.approvals.__setitem__(approval_id, ApprovalStatus.APPROVED)
        )
        await run.scheduler.run(deadline_ms=60_000)
        return action_id

    action_id = asyncio.run(main())
    assert run.store.view(action_id).action.state is S.CONFIRMED
    assert (run.world.fact_reads, run.freshness.calls) == (0, 0)


# A gateway crash at a dispatch point (F1, 0006) ------------------------------------------------


class Crash:
    """Crashes the gateway when F1 fires; the restart runs startup recovery (0003, 0006)."""

    def __init__(
        self, point: str, contract: Contract = Contract.C2, *, identity: bool = False
    ) -> None:
        self.faults = FaultInjector(parse_plan([{"fault": "F1", "at": {"point": point}}]))
        self.run = Run(contract, identity=identity, faults=self.faults, on_fault=self.handle)
        self.contexts: list[Mapping[str, object]] = []

    def handle(self, fired: Fired, point: str, context: Mapping[str, object]) -> None:
        self.contexts.append(dict(context))
        self.run.scheduler.crash(
            "gateway", restart_after_ms=1_000, restart=self.run.dispatcher.start
        )


def test_after_a_crash_before_the_send_g1_checks_again_and_sends_its_action() -> None:
    """Startup recovery returns the Authorized action to the outbox (0003, step 3)."""
    crash = Crash("gw.dispatch.after_check")
    action_id = crash.run.go()
    view = crash.run.store.view(action_id)
    assert view.action.state is S.CONFIRMED and len(view.attempts) == 1
    assert crash.run.world.approval_checks == 2, "the check before the crash, and a fresh one"
    assert crash.run.world.ledger == ["PO-0001"]


@pytest.mark.parametrize("contract", LEVELS)
def test_after_a_crash_with_an_open_attempt_g1_records_it_unknown_and_stops(
    contract: Contract,
) -> None:
    """Startup recovery, step 1; G1 has no contract recovery to resume (0003, 0008)."""
    crash = Crash("gw.dispatch.after_record", contract)
    action_id = crash.run.go()
    view = crash.run.store.view(action_id)
    assert (view.action.state, view.escalation) == (S.OUTCOME_UNKNOWN, None)
    assert crash.run.reasons(action_id)[-1] == "recovered_open_attempt"
    assert crash.run.world.ledger == [], "the request never left, and nothing was retried"


@pytest.mark.parametrize(
    ("identity", "contract", "key"),
    [
        (False, Contract.C2, None),
        (False, Contract.C3, None),
        (True, Contract.C0, None),
        (True, Contract.C2, "A-0001"),
    ],
)
def test_a_crash_after_the_record_names_the_key_the_request_would_carry(
    identity: bool, contract: Contract, key: str | None
) -> None:
    """The harness records the request that may have left (0006); G1's carries no key, nor
    does G2's below C2. Before G1, the context named the action's key at every level."""
    crash = Crash("gw.dispatch.after_record", contract, identity=identity)
    crash.run.go()
    (context,) = crash.contexts
    assert {k: context[k] for k in ("intent_id", "api", "idempotency_key")} == {
        "intent_id": "REQ-1",
        "api": "submit_purchase_order",
        "idempotency_key": key,
    }


# The request on the wire ------------------------------------------------------------------------


@pytest.mark.parametrize("contract", LEVELS)
@pytest.mark.parametrize("identity", [False, True])
def test_only_g2_sends_a_key_and_only_where_the_contract_takes_one(
    contract: Contract, identity: bool
) -> None:
    """G1 has no idempotency keys at any level (0008); G2 sends its action's key at C2 and
    C3 (0005). Both send the cited versions as the basis."""
    scheduler = Scheduler()
    log = MemoryHarnessLog()
    transport = Transport(scheduler, log, Generators(7).get("latency"), max_hop_latency_ms=50)

    async def simulator(request: Envelope) -> Reply:
        body = {
            "answer": "committed",
            "effect_id": "PO-0001",
            "replay": False,
            "intent_id": "REQ-1",
            "operation": "submit_purchase_order",
            "args": {},
            "committed_vt": scheduler.now,
        }
        return Reply(body, "committed")

    transport.serve("simulator", simulator)
    store = MemoryStore(
        task_id="TASK-1",
        intents={"REQ-1": "submit_purchase_order"},
        contract=contract,
        settings=SETTINGS,
        identity=identity,
    )
    args: dict[str, str | int] = {
        "supplier_id": "S-1",
        "sku": "W-100",
        "quantity": 100,
        "unit_price_minor": 540,
        "currency": "USD",
        "cost_center": "CC-OPS",
    }
    quote = store.record_read("quote/S-1/W-100", 1, {}, 0)
    budget = store.record_read("budget/CC-OPS", 2, {}, 0)
    call = WriteCall(
        "REQ-1",
        "submit_purchase_order",
        "supplier/S-1",
        args,
        "k",
        (quote.read_id, budget.read_id),
        ("quote/S-1/W-100", "budget/CC-OPS"),
    )
    result = store.propose(call, 0)
    assert isinstance(result, ActionResult)
    action_id = result.action_id
    fresh = None if not identity else BasisCheck(True, True)
    store.file_approval(action_id, fresh, "APR-0001", 1, seen_seq=store.view(action_id).last_seq)
    approved = ApprovalStatus.APPROVED
    store.poll_approval(action_id, approved, fresh, 2, seen_seq=store.view(action_id).last_seq)
    ticket = store.begin_check(action_id)
    check = ticket.record(approval=approved, basis=fresh, check_vt=3, expires_at=10**9)
    attempt = store.dispatch(action_id, check, 3)
    assert attempt is not None
    client = SimClient(transport, timeout_ms=SETTINGS.dispatch_timeout_ms)
    answers: list[Answer] = []

    async def main() -> None:
        async def send() -> None:
            answers.append(await client.send(store.view(action_id).action, attempt, "APR-0001"))

        scheduler.spawn("gateway", send(), name="send")
        await scheduler.run(deadline_ms=10_000)

    asyncio.run(main())
    assert [a.answer_class for a in answers] == [AnswerClass.COMMITTED]
    (request,) = [m for m in log.messages if m.kind == "request"]
    assert request.api == str(ApiName.SUBMIT_PURCHASE_ORDER)
    keyed = identity and contract in (Contract.C2, Contract.C3)
    expected = action_id if keyed else None
    assert isinstance(request.body, dict)
    assert (request.idempotency_key, request.body["idempotency_key"]) == (expected, expected)
    assert request.body["basis"] == {"quote/S-1/W-100": 1, "budget/CC-OPS": 2}
