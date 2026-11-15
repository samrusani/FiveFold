"""A Hypothesis rule-based state machine for the gateway's records (0003, testing plan).

Rules follow record 0003's list: propose, approve, deny, revoke, expire, change a fact,
dispatch, answer (committed, rejected, in_progress, unknown), crash and restart, probe,
retry, attest and report, plus approval timeouts, run closing and status reads. A
dispatch check and its commit are separate rules, so other events can come between
them, as they can between the gateway's unlocked check and its commit.

Three things are kept apart:

- The store under test: `MemoryStore`, which runs the step functions. The Postgres
  machines at the end run every operation on `PgStore` too, through `TwinStore`, and
  require the same results, the same refusals and the same records as the in-memory
  store, the oracle (0003, testing plan, item 3).
- The world it cannot see (`World`): the approvals service, the facts, and the
  destination's ledger, including requests whose reply was lost. The world keeps its
  contract (0005), so a correct gateway must stay safe in it.
- The reference model (`Expected`): what each action's state should be, worked out
  here from the decision records without calling the step functions.

After every step the machine checks I1 to I7, that the log only grew, that effect ids
never changed, agreement with the model, and safety against the world: no action is
Failed while its effect committed or may still commit, every Confirmed action names the
effect the destination committed, and at C0 to C2 no effect commits after its approval
expired. At C3 the gateway does not check a retry, and the world does not model the
destination's own check, so the last property is not asserted there.

Approvals expire at the time the approvals service reports (0002). The world grants each
approval one of three lifetimes: one that ends before a send could commit, one that ends
before a retry, and one that outlasts the run (0010, change 4). The authority can revoke
an approval it granted at any time, also before the gateway has seen it approved (0010,
change 1). Two agent workers report, as when fault F9 starts a second one (0009): an
accepted report closes the intents it claims `not_done`, and the run closes with the
second worker's first accepted report (0010, item 6).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, cast

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from fivefold.domain.contracts import Capability, Contract, ContractParams
from fivefold.gateway.invariants import check_invariants
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.pg_store import PgStore
from fivefold.gateway.records import (
    ActionResult,
    Answer,
    ApprovalStatus,
    BasisCheck,
    CheckRecord,
    CheckTicket,
    Claim,
    ClaimStatus,
    ProbeAnswer,
    Refusal,
    RefusalReason,
    ReportAccepted,
    Resolution,
    Snapshot,
    StaleFact,
    WriteCall,
)
from fivefold.gateway.state_machine import (
    ActionState,
    AnswerClass,
    CheckKind,
    EventKind,
    GatewayRuleError,
    GatewaySettings,
    GrantState,
    Note,
    Reason,
    ReconcileHow,
)
from fivefold.gateway.twin import TwinStore

S = ActionState
G = GrantState
A = ApprovalStatus
SETTINGS = GatewaySettings(
    approval_wait_ms=3_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000, status_lag_ms=300),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
    agent_workers=2,
)
SETTLE = SETTINGS.settle_ms
LAG = SETTINGS.params.status_lag_ms
T = SETTINGS.dispatch_timeout_ms
TTLS = (SETTLE // 2, SETTLE + 2 * T, 10**9)
"""Approval lifetimes: ends before a send could commit, ends before a retry, outlasts all."""
WORKERS = ("W1", "W2")
QUOTE_CHANGED = StaleFact("quote", 1, 2, ("unit_price_minor",))
BUDGET_SHORT = StaleFact("budget", 1, 2, ("committed_minor",))
INTENTS = {
    "REQ-1": "submit_purchase_order",
    "REQ-2": "submit_purchase_order",
    "REQ-3": "reserve_inventory",
}
UNDISPATCHED = {S.PROPOSED, S.PENDING_APPROVAL, S.AUTHORIZED}
LIVE = UNDISPATCHED | {S.DISPATCHED, S.OUTCOME_UNKNOWN}
HOLDING = LIVE | {S.CONFIRMED}


@dataclass
class World:
    """What is true outside the gateway for one action."""

    approval: ApprovalStatus = A.PENDING
    expires_at: int | None = None
    """When a granted approval expires, as the approvals service reports it."""
    exact_ok: bool = True
    sufficient_ok: bool = True
    effect_id: str | None = None
    effect_vt: int = 0
    """When the effect committed; a status answer lists it only if accurate to then."""
    may_commit_until: int | None = None
    """A request reached the destination and may still commit until this time (sent + S)."""

    @property
    def fresh(self) -> bool:
        return self.exact_ok and self.sufficient_ok

    def status(self, vt: int) -> ApprovalStatus:
        """What the approvals service reports at `vt`: a granted approval expires."""
        expired = self.expires_at is not None and self.expires_at <= vt
        return A.EXPIRED if self.approval is A.APPROVED and expired else self.approval


@dataclass
class Expected:
    """The reference model's view of one action."""

    intent_id: str
    key: str
    basis: dict[str, int]
    state: ActionState = S.PROPOSED
    reason: Reason = Reason.ADMITTED
    stale: tuple[StaleFact, ...] = ()
    grant: GrantState | None = None
    requested_vt: int = 0
    attempts: list[int] = field(default_factory=list)
    effect_id: str | None = None
    escalated: bool = False
    probes: int = 0
    events: int = 1
    """How many events the action should have; a dispatch check is stale once it grows."""


@dataclass
class InFlight:
    """A dispatch check the dispatcher has made but not yet committed."""

    action_id: str
    ticket: CheckTicket
    record: CheckRecord
    events_seen: int


class GatewayMachine(RuleBasedStateMachine):
    contract: ClassVar[Contract]

    def __init__(self) -> None:
        super().__init__()
        self.vt = 0
        self.store: MemoryStore | TwinStore = self.new_store()
        self.world: dict[str, World] = {}
        self.expected: dict[str, Expected] = {}
        self.checks: list[InFlight] = []
        self.closed = False
        self.closed_intents: set[str] = set()
        self.reports: dict[str, int] = {}
        """Each worker with an accepted report: the call number of its first one."""
        self.effects = 0
        self.previous: Snapshot = self.store.snapshot()

    def new_store(self) -> MemoryStore | TwinStore:
        """The store under test; the Postgres machines override this."""
        return MemoryStore(
            task_id="TASK-1", intents=INTENTS, contract=self.contract, settings=SETTINGS
        )

    # Queries ------------------------------------------------------------------------------

    def seq(self, action_id: str) -> int:
        """The seq a check starting now would see."""
        return self.store.view(action_id).last_seq

    def ids(self, *states: ActionState) -> list[str]:
        return sorted(a for a, e in self.expected.items() if e.state in states)

    def retryable(self) -> list[str]:
        if not self.contract.has(Capability.IDEMPOTENCY_KEY):
            return []
        return [
            a
            for a in self.ids(S.OUTCOME_UNKNOWN)
            if not self.expected[a].escalated
            and len(self.expected[a].attempts) < SETTINGS.max_attempts
        ]

    def probeable(self) -> list[str]:
        if self.contract is not Contract.C1:
            return []
        return [a for a in self.ids(S.OUTCOME_UNKNOWN) if not self.expected[a].escalated]

    def escalated(self) -> list[str]:
        return [a for a in self.ids(S.OUTCOME_UNKNOWN) if self.expected[a].escalated]

    def awaiting_decision(self) -> list[str]:
        return [a for a in self.ids(S.PENDING_APPROVAL) if self.world[a].approval is A.PENDING]

    def revocable(self) -> list[str]:
        """Actions whose approval was granted and not revoked, whether seen or not."""
        return sorted(
            a
            for a, e in self.expected.items()
            if e.grant in (G.PENDING, G.ACTIVE, G.CONSUMED) and self.world[a].approval is A.APPROVED
        )

    # The world's bookkeeping ----------------------------------------------------------------

    def commit(self, world: World, at: int) -> str:
        """The destination commits this action's effect at `at`, once (keys or one attempt).

        Callers keep `at` within the commit deadline: a request sent at `s` commits by
        `s + S` or never (0005).
        """
        if world.effect_id is None:
            self.effects += 1
            world.effect_id, world.effect_vt = f"PO-{self.effects:04d}", at
        return world.effect_id

    def basis(self, world: World, *, full: bool = True) -> BasisCheck:
        stale: tuple[StaleFact, ...] = () if world.exact_ok else (QUOTE_CHANGED,)
        if full and not world.sufficient_ok:
            stale += (BUDGET_SHORT,)
        return BasisCheck(world.exact_ok, world.sufficient_ok if full else None, stale=stale)

    def settle_world(self, action_id: str, commit: bool) -> None:
        """The destination finishes a request that was still in progress."""
        world = self.world[action_id]
        if world.may_commit_until is None:
            return
        if commit:
            self.commit(world, min(self.vt, world.may_commit_until))
        world.may_commit_until = None

    def settle_if_due(self, action_id: str, commit: bool) -> None:
        """Past its deadline, an unfinished request has committed or never will (0005)."""
        until = self.world[action_id].may_commit_until
        if until is not None and self.vt >= until:
            self.settle_world(action_id, commit)

    def lose_reply(self, action_id: str, fate: str) -> None:
        """The reply never reaches the gateway; `fate` is what became of the request."""
        world = self.world[action_id]
        if world.effect_id is not None or world.may_commit_until is not None:
            return
        if fate == "committed":
            self.commit(world, min(self.vt, self.expected[action_id].attempts[-1] + SETTLE))
        elif fate == "in_progress":
            world.may_commit_until = self.expected[action_id].attempts[-1] + SETTLE

    def approve(self, world: World, ttl: int) -> None:
        """The policy or the approver grants the approval, valid for `ttl` from now."""
        world.approval, world.expires_at = A.APPROVED, self.vt + ttl

    # The model's bookkeeping ----------------------------------------------------------------

    def close(self, intents: set[str], *, whole_run: bool) -> None:
        """Close intents, or the whole run: what was never sent is rejected (0003, 0010)."""
        for expected in self.expected.values():
            if expected.state in UNDISPATCHED and (whole_run or expected.intent_id in intents):
                expected.state, expected.reason = S.REJECTED, Reason.RUN_CLOSED
                if expected.grant is not None:
                    expected.grant = G.RELEASED
                expected.events += 1
        self.closed_intents |= intents
        self.closed = self.closed or whole_run

    def enter_unknown(self, expected: Expected, reason: Reason) -> None:
        """T10, then escalation where record 0003 escalates at once."""
        expected.state, expected.reason = S.OUTCOME_UNKNOWN, reason
        expected.events += 1
        retries = self.contract.has(Capability.IDEMPOTENCY_KEY)
        no_recovery = not self.contract.capabilities
        if no_recovery or (retries and len(expected.attempts) >= SETTINGS.max_attempts):
            expected.escalated = True
            expected.events += 1

    def refused(self, call: Callable[[], object]) -> None:
        """Assert that a step is refused and changes nothing."""
        before = self.store.snapshot()
        try:
            call()
        except GatewayRuleError:
            pass
        else:
            raise AssertionError("the step should have been refused")
        assert self.store.snapshot() is before

    # Gateway steps, each with its expected outcome --------------------------------------------

    def do_file(self, action_id: str, policy: ApprovalStatus, ttl: int) -> None:
        world, expected = self.world[action_id], self.expected[action_id]
        expected.events += 1
        if world.fresh:
            if policy is A.APPROVED:
                self.approve(world, ttl)
            else:
                world.approval = policy
            self.store.file_approval(
                action_id,
                self.basis(world),
                f"AP-{action_id}",
                self.vt,
                seen_seq=self.seq(action_id),
            )
            expected.state, expected.grant = S.PENDING_APPROVAL, G.PENDING
            expected.reason = Reason.APPROVAL_REQUESTED
            expected.requested_vt = self.vt
        else:
            self.store.file_approval(
                action_id, self.basis(world), None, self.vt, seen_seq=self.seq(action_id)
            )
            expected.state, expected.reason = S.REJECTED, Reason.STALE_INPUT
            expected.stale = self.basis(world).stale

    def do_poll(self, action_id: str, check_basis: bool) -> None:
        world, expected = self.world[action_id], self.expected[action_id]
        approval = world.status(self.vt)
        basis = self.basis(world) if check_basis or approval is A.APPROVED else None
        self.store.poll_approval(action_id, approval, basis, self.vt, seen_seq=self.seq(action_id))
        if approval is A.DENIED:
            expected.state, expected.grant = S.REJECTED, G.DENIED
            expected.reason = Reason.APPROVAL_DENIED
        elif approval is A.REVOKED:
            expected.state, expected.grant = S.REJECTED, G.REVOKED
            expected.reason = Reason.APPROVAL_REVOKED
        elif approval is A.EXPIRED:
            expected.state, expected.grant = S.REJECTED, G.EXPIRED
            expected.reason = Reason.APPROVAL_EXPIRED
        elif approval is A.APPROVED and world.fresh:
            expected.state, expected.grant = S.AUTHORIZED, G.ACTIVE
            expected.reason = Reason.APPROVED
        elif approval is A.APPROVED or (basis is not None and not world.fresh):
            expected.state, expected.grant = S.REJECTED, G.RELEASED
            expected.reason = Reason.STALE_INPUT
            expected.stale = self.basis(world).stale
        else:
            return
        expected.events += 1

    def do_time_out(self, action_id: str) -> None:
        expected, approval = self.expected[action_id], self.world[action_id].status(self.vt)
        seen = self.seq(action_id)
        if approval is not A.PENDING or self.vt < expected.requested_vt + SETTINGS.approval_wait_ms:
            self.refused(
                lambda: self.store.time_out_approval(action_id, approval, self.vt, seen_seq=seen)
            )
            return
        self.store.time_out_approval(action_id, approval, self.vt, seen_seq=seen)
        expected.state, expected.grant = S.REJECTED, G.TIMED_OUT
        expected.reason = Reason.APPROVAL_TIMEOUT
        expected.events += 1

    def do_begin(self, action_id: str) -> InFlight:
        world, expected = self.world[action_id], self.expected[action_id]
        ticket = self.store.begin_check(action_id)
        attempt_no = len(expected.attempts) + 1
        assert ticket.attempt_no == attempt_no
        if attempt_no == 1:
            kind = CheckKind.FULL
        elif self.contract.has(Capability.CONDITIONAL_WRITE):
            kind = CheckKind.EXEMPT
        else:
            kind = CheckKind.EXACT_ONLY
        assert ticket.kind is kind
        if attempt_no > 1:
            backoff = min(T * 2 ** (attempt_no - 2), 8 * T)
            assert ticket.not_before_vt == expected.attempts[-1] + backoff
        if kind is CheckKind.EXEMPT:
            record = ticket.record(approval=None, basis=None, check_vt=self.vt)
        else:
            basis = self.basis(world, full=kind is CheckKind.FULL)
            status = world.status(self.vt)
            expires_at = world.expires_at if status is A.APPROVED else None
            record = ticket.record(
                approval=status, basis=basis, check_vt=self.vt, expires_at=expires_at
            )
        return InFlight(action_id, ticket, record, expected.events)

    def do_commit(self, flight: InFlight, wait: bool) -> None:
        action_id, record = flight.action_id, flight.record
        expected = self.expected[action_id]
        if wait:
            self.vt = max(self.vt, flight.ticket.not_before_vt)
        first = flight.ticket.attempt_no == 1

        def commit() -> object:
            if first:
                return self.store.dispatch(action_id, record, self.vt)
            return self.store.retry(action_id, record, self.vt)

        if expected.events != flight.events_seen:
            before = self.store.snapshot()
            assert commit() is None
            assert self.store.snapshot().events == before.events
            return
        if not first and self.vt < flight.ticket.not_before_vt:
            self.refused(commit)
            return
        sent = commit()
        expected.events += 1
        # The check allows the send only if the approval outlives the send's settle time
        # (0002 rule 17, 0010 change 4) and the basis held (I3).
        approval, basis = record.approval, record.basis
        expires_at = record.approval_expires_at
        if approval is A.APPROVED and expires_at is not None and expires_at <= self.vt + SETTLE:
            approval = A.EXPIRED
        passes = record.kind is CheckKind.EXEMPT or (
            approval is A.APPROVED
            and basis is not None
            and basis.exact_ok
            and basis.sufficient_ok is not False
        )
        if passes:
            assert sent is not None
            expected.state = S.DISPATCHED
            expected.reason = Reason.DISPATCHED if first else Reason.RETRY
            if first:
                expected.grant = G.CONSUMED
            expected.attempts.append(self.vt)
            return
        assert sent is None
        withdrawn = {A.REVOKED: G.REVOKED, A.EXPIRED: G.EXPIRED}
        reasons = {A.REVOKED: Reason.APPROVAL_REVOKED, A.EXPIRED: Reason.APPROVAL_EXPIRED}
        assert approval is not None
        if first:
            expected.state = S.REJECTED
            expected.grant = withdrawn.get(approval, G.RELEASED)
            expected.reason = reasons.get(approval, Reason.STALE_INPUT)
            if expected.reason is Reason.STALE_INPUT:
                assert record.basis is not None
                expected.stale = record.basis.stale
        else:
            expected.escalated = True
            expected.grant = withdrawn.get(approval, expected.grant)

    def do_answer(self, action_id: str, data: st.DataObject, finish: bool) -> None:
        world, expected = self.world[action_id], self.expected[action_id]
        self.settle_if_due(action_id, finish)
        attempt_no = len(expected.attempts)
        options = ["transport_error", "server_error"]
        if self.vt >= expected.attempts[-1] + T:
            options.append("timeout")
        if world.effect_id is not None:
            options.append("replay")
        elif world.may_commit_until is not None:
            options += ["in_progress", "in_progress"]
        else:
            options += ["committed", "committed", "destination_rejected"]
            if self.contract.has(Capability.CONDITIONAL_WRITE):
                options.append("precondition_failed")
        choice = data.draw(st.sampled_from(options))
        if choice in ("replay", "committed"):
            at = min(self.vt, expected.attempts[-1] + SETTLE)
            answer = Answer(AnswerClass.COMMITTED, Reason.COMMITTED, self.commit(world, at))
            self.store.answer(action_id, attempt_no, answer, self.vt)
            expected.state, expected.effect_id = S.CONFIRMED, world.effect_id
            expected.reason = Reason.COMMITTED
            expected.events += 1
        elif choice in ("destination_rejected", "precondition_failed"):
            answer = Answer(AnswerClass.REJECTED, Reason(choice))
            self.store.answer(action_id, attempt_no, answer, self.vt)
            expected.state, expected.reason = S.FAILED, Reason(choice)
            expected.events += 1
        else:
            if choice == "in_progress":
                answer = Answer(AnswerClass.IN_PROGRESS, Reason.IN_PROGRESS)
            else:
                answer = Answer(AnswerClass.UNKNOWN, Reason(choice))
                fate = data.draw(st.sampled_from(["lost", "committed", "in_progress"]))
                self.lose_reply(action_id, fate)
            self.store.answer(action_id, attempt_no, answer, self.vt)
            self.enter_unknown(expected, Reason(choice))

    def do_probe(self, action_id: str, answered: bool, finish: bool, lag: int) -> None:
        """A C1 status query. The answer is accurate to `as_of_vt`, at most L ago (0005)."""
        world, expected = self.world[action_id], self.expected[action_id]
        as_of_vt = max(0, self.vt - lag)
        if answered:
            self.settle_if_due(action_id, finish)
            visible = world.effect_id is not None and world.effect_vt <= as_of_vt
            listed = (world.effect_id,) if visible and world.effect_id else ()
            result = ProbeAnswer(answered=True, effect_ids=listed, as_of_vt=as_of_vt)
        else:
            result = ProbeAnswer(answered=False)
        self.store.probe(action_id, result, self.vt)
        expected.events += 1
        if result.effect_ids:
            expected.state, expected.effect_id = S.CONFIRMED, world.effect_id
            expected.reason = Reason.STATUS_FOUND
        elif answered and as_of_vt >= expected.attempts[-1] + SETTLE:
            expected.state, expected.reason = S.FAILED, Reason.STATUS_SETTLED_EMPTY
        else:
            expected.probes += 1
            if expected.probes >= SETTINGS.max_probes:
                expected.escalated = True
                expected.events += 1

    def do_attest(self, action_id: str, finish: bool) -> None:
        world, expected = self.world[action_id], self.expected[action_id]
        self.settle_if_due(action_id, finish)
        if world.effect_id is not None:
            self.store.attest(action_id, Resolution.COMMITTED, world.effect_id, "resolver", self.vt)
            expected.state, expected.effect_id = S.CONFIRMED, world.effect_id
            expected.reason = Reason.HUMAN_ATTESTED
        elif self.vt >= expected.attempts[-1] + SETTLE:
            assert world.may_commit_until is None
            self.store.attest(action_id, Resolution.NOT_COMMITTED, None, "resolver", self.vt)
            expected.state, expected.reason = S.FAILED, Reason.HUMAN_ATTESTED
        else:
            self.refused(
                lambda: self.store.attest(
                    action_id, Resolution.NOT_COMMITTED, None, "resolver", self.vt
                )
            )
            return
        expected.events += 1

    # Rules: the world ----------------------------------------------------------------------

    @rule(dt=st.sampled_from([1, 50, T, 2 * T, 4 * T, SETTLE, SETTINGS.approval_wait_ms]))
    def time_passes(self, dt: int) -> None:
        self.vt += dt

    @precondition(lambda self: self.awaiting_decision())
    @rule(data=st.data(), approve=st.booleans(), ttl=st.sampled_from(TTLS))
    def approver_decides(self, data: st.DataObject, approve: bool, ttl: int) -> None:
        world = self.world[data.draw(st.sampled_from(self.awaiting_decision()))]
        if approve:
            self.approve(world, ttl)
        else:
            world.approval = A.DENIED

    @precondition(lambda self: self.revocable())
    @rule(data=st.data())
    def authority_revokes(self, data: st.DataObject) -> None:
        """A revocation, also before the gateway has seen the approval (0010, change 1)."""
        self.world[data.draw(st.sampled_from(self.revocable()))].approval = A.REVOKED

    @precondition(lambda self: self.world)
    @rule(data=st.data(), exact=st.booleans(), holds=st.booleans())
    def fact_changes(self, data: st.DataObject, exact: bool, holds: bool) -> None:
        action_id = data.draw(st.sampled_from(sorted(self.world)))
        if exact:
            self.world[action_id].exact_ok = holds
        else:
            self.world[action_id].sufficient_ok = holds

    @precondition(lambda self: any(w.may_commit_until is not None for w in self.world.values()))
    @rule(data=st.data(), commit=st.booleans())
    def destination_finishes(self, data: st.DataObject, commit: bool) -> None:
        busy = sorted(a for a, w in self.world.items() if w.may_commit_until is not None)
        self.settle_world(data.draw(st.sampled_from(busy)), commit)

    # Rules: tools ---------------------------------------------------------------------------

    @rule(
        intent=st.sampled_from(sorted(INTENTS)),
        variant=st.sampled_from(["a", "b"]),
        quote_version=st.integers(1, 3),
        budget_version=st.integers(1, 2),
        citing=st.sampled_from(["both", "both", "twice", "missing", "unknown"]),
    )
    def propose(
        self, intent: str, variant: str, quote_version: int, budget_version: int, citing: str
    ) -> None:
        key = f"{intent}-{variant}"
        # Two keys each, in an order that PostgreSQL's jsonb does not keep, so the Postgres
        # machines show that key order never changes a result.
        args: dict[str, str | int] = {"quantity": 2, "variant": variant}
        quote = self.store.record_read("quote", quote_version, {}, self.vt)
        budget = self.store.record_read("budget", budget_version, {}, self.vt)
        based_on: tuple[str, ...] = (quote.read_id, budget.read_id)
        if citing == "twice":
            # An older read of the same quote, cited first: the newest version counts.
            older = self.store.record_read("quote", quote_version - 1, {}, self.vt)
            based_on = (older.read_id, *based_on)
        elif citing == "missing":
            based_on = (quote.read_id,)
        elif citing == "unknown":
            based_on = (*based_on, "R-99999")
        call = WriteCall(
            intent, INTENTS[intent], "target", args, key, based_on, ("budget", "quote")
        )
        result = self.store.propose(call, self.vt)
        basis = {"budget": budget_version, "quote": quote_version}
        holders = [
            a for a, e in self.expected.items() if e.intent_id == intent and e.state in HOLDING
        ]
        assert len(holders) <= 1
        if self.closed or intent in self.closed_intents:
            assert isinstance(result, Refusal) and result.reason is RefusalReason.RUN_CLOSED
            return
        if citing == "unknown":
            assert isinstance(result, Refusal) and result.reason is RefusalReason.UNKNOWN_READ
            assert result.unknown_reads == ("R-99999",)
            return
        if citing == "missing":
            assert isinstance(result, Refusal) and result.reason is RefusalReason.MISSING_BASIS
            assert result.missing_keys == ("budget",)
            return
        if not holders:
            action_id = f"A-{len(self.expected) + 1:04d}"
            assert isinstance(result, ActionResult)
            assert (result.action_id, result.state) == (action_id, S.PROPOSED)
            self.expected[action_id] = Expected(intent, key, basis)
            self.world[action_id] = World()
            self.check_status(action_id, tool=INTENTS[intent])
            return
        holder = self.expected[holders[0]]
        if key != holder.key:
            assert isinstance(result, Refusal)
            done = holder.state is S.CONFIRMED
            reason = RefusalReason.INTENT_DONE if done else RefusalReason.INTENT_BUSY
            assert (result.reason, result.action_id) == (reason, holders[0])
            return
        assert isinstance(result, ActionResult) and result.action_id == holders[0]
        assert result.state is holder.state
        newer = basis != holder.basis and all(basis[k] >= v for k, v in holder.basis.items())
        if holder.state in UNDISPATCHED and newer:
            holder.basis = basis
            holder.events += 1
        self.check_status(holders[0], tool=INTENTS[intent])

    @precondition(lambda self: self.expected)
    @rule(data=st.data())
    def status(self, data: st.DataObject) -> None:
        self.check_status(data.draw(st.sampled_from(sorted(self.expected))))

    def check_status(self, action_id: str, tool: str = "get_action_status") -> None:
        """A tool's answer once its wait ends: the result must match the model (0003, I5)."""
        result = self.store.status(action_id, self.vt, tool=tool)
        expected = self.expected[action_id]
        assert (result.state, result.reason) == (expected.state, expected.reason)
        rejected_stale = expected.state is S.REJECTED and expected.reason is Reason.STALE_INPUT
        assert result.stale == (expected.stale if rejected_stale else ())
        assert result.effect_id == (expected.effect_id if expected.state is S.CONFIRMED else None)
        if expected.state is S.OUTCOME_UNKNOWN:
            assert result.reconcile is not None
            if expected.escalated or not self.contract.capabilities:
                assert result.reconcile.how is ReconcileHow.ESCALATED_TO_HUMAN
            elif self.contract is Contract.C1:
                assert result.reconcile.how is ReconcileHow.QUERYING_STATUS
            else:
                assert result.reconcile.how is ReconcileHow.RETRYING_WITH_SAME_KEY
        else:
            assert result.reconcile is None

    @rule(data=st.data())
    def report(self, data: st.DataObject) -> None:
        """A worker's final report: true or drawn at random.

        True reports are always accepted, so the closing they cause (0010, item 6) is
        reached often; random ones are mostly refused. Like closing the run, true reports
        wait until the run has made some progress, so that runs still reach deep states.
        """
        claims: list[Claim] = []
        accept = True
        progressed = len(self.store.snapshot().events) >= 12
        honest = progressed and data.draw(st.booleans())
        for intent in sorted(INTENTS):
            actions = [e for e in self.expected.values() if e.intent_id == intent]
            states = {e.state for e in actions}
            effects = sorted(e.effect_id for e in actions if e.effect_id is not None)
            if not honest:
                status = data.draw(st.sampled_from(list(ClaimStatus)))
            elif S.CONFIRMED in states:
                status = ClaimStatus.DONE
            elif states & {S.DISPATCHED, S.OUTCOME_UNKNOWN}:
                status = ClaimStatus.UNKNOWN
            else:
                status = ClaimStatus.NOT_DONE
            if status is ClaimStatus.DONE:
                use_recorded = bool(effects) and (honest or data.draw(st.booleans()))
                ids = tuple(effects) if use_recorded else ("PO-9999",)
                claims.append(Claim(intent, status, ids))
                accept &= S.CONFIRMED in states and list(ids) == effects
            else:
                claims.append(Claim(intent, status))
                if status is ClaimStatus.NOT_DONE:
                    accept &= states <= UNDISPATCHED | {S.REJECTED, S.FAILED}
                else:
                    accept &= bool(states & {S.DISPATCHED, S.OUTCOME_UNKNOWN})
        worker = data.draw(st.sampled_from(WORKERS))
        result = self.store.submit_report(claims, self.vt, worker=worker)
        assert isinstance(result, ReportAccepted) == accept
        if accept:
            self.reports.setdefault(worker, len(self.store.snapshot().tool_calls))
            not_done = {c.intent_id for c in claims if c.status is ClaimStatus.NOT_DONE}
            self.close(not_done, whole_run=len(self.reports) >= SETTINGS.agent_workers)

    # Rules: one gateway step at a time ------------------------------------------------------

    @precondition(lambda self: self.ids(S.PROPOSED))
    @rule(
        data=st.data(),
        policy=st.sampled_from([A.PENDING, A.APPROVED, A.DENIED]),
        ttl=st.sampled_from(TTLS),
    )
    def file_approval(self, data: st.DataObject, policy: ApprovalStatus, ttl: int) -> None:
        self.do_file(data.draw(st.sampled_from(self.ids(S.PROPOSED))), policy, ttl)

    @precondition(lambda self: self.ids(S.PENDING_APPROVAL))
    @rule(data=st.data(), check_basis=st.booleans())
    def poll_approval(self, data: st.DataObject, check_basis: bool) -> None:
        self.do_poll(data.draw(st.sampled_from(self.ids(S.PENDING_APPROVAL))), check_basis)

    @precondition(lambda self: self.ids(S.PENDING_APPROVAL))
    @rule(data=st.data(), jump=st.booleans())
    def approval_times_out(self, data: st.DataObject, jump: bool) -> None:
        action_id = data.draw(st.sampled_from(self.ids(S.PENDING_APPROVAL)))
        if jump:
            due = self.expected[action_id].requested_vt + SETTINGS.approval_wait_ms
            self.vt = max(self.vt, due)
        self.do_time_out(action_id)

    @precondition(lambda self: self.ids(S.AUTHORIZED) or self.retryable())
    @rule(data=st.data())
    def begin_check(self, data: st.DataObject) -> None:
        candidates = sorted(self.ids(S.AUTHORIZED) + self.retryable())
        self.checks.append(self.do_begin(data.draw(st.sampled_from(candidates))))

    @precondition(lambda self: self.checks)
    @rule(data=st.data(), wait=st.booleans())
    def commit_check(self, data: st.DataObject, wait: bool) -> None:
        flight = self.checks.pop(data.draw(st.integers(0, len(self.checks) - 1)))
        self.do_commit(flight, wait)

    @precondition(lambda self: self.ids(S.DISPATCHED))
    @rule(data=st.data(), finish=st.booleans())
    def destination_answers(self, data: st.DataObject, finish: bool) -> None:
        self.do_answer(data.draw(st.sampled_from(self.ids(S.DISPATCHED))), data, finish)

    @precondition(lambda self: self.probeable())
    @rule(
        data=st.data(),
        answered=st.booleans(),
        finish=st.booleans(),
        lag=st.sampled_from([0, 1, LAG]),
    )
    def probe(self, data: st.DataObject, answered: bool, finish: bool, lag: int) -> None:
        self.do_probe(data.draw(st.sampled_from(self.probeable())), answered, finish, lag)

    @precondition(lambda self: self.escalated())
    @rule(data=st.data(), finish=st.booleans())
    def human_attests(self, data: st.DataObject, finish: bool) -> None:
        self.do_attest(data.draw(st.sampled_from(self.escalated())), finish)

    @rule(data=st.data())
    def gateway_restarts(self, data: st.DataObject) -> None:
        self.checks.clear()
        for action_id in self.ids(S.DISPATCHED):
            self.lose_reply(
                action_id, data.draw(st.sampled_from(["lost", "committed", "in_progress"]))
            )
        dispatched = self.ids(S.DISPATCHED)
        self.store.restart(self.vt)
        for action_id in dispatched:
            self.enter_unknown(self.expected[action_id], Reason.RECOVERED_OPEN_ATTEMPT)

    @precondition(lambda self: not self.closed and len(self.store.snapshot().events) >= 12)
    @rule()
    def close_run(self) -> None:
        self.store.close_run(self.vt)
        self.close(set(), whole_run=True)

    @initialize()
    def one_proposal_per_intent(self) -> None:
        for intent in sorted(INTENTS):
            self.propose(intent, "a", 1, 1, "both")

    @precondition(lambda self: self.ids(*LIVE))
    @rule(data=st.data(), steps=st.integers(1, 8))
    def gateway_progresses(self, data: st.DataObject, steps: int) -> None:
        """Move live actions along their usual paths, so runs reach every state."""
        for _ in range(steps):
            live = self.ids(*LIVE)
            if not live:
                return
            action_id = data.draw(st.sampled_from(live))
            self.progress(action_id, data)
            if data.draw(st.booleans()):
                self.check_status(action_id)

    def progress(self, action_id: str, data: st.DataObject) -> None:
        expected, world = self.expected[action_id], self.world[action_id]
        state = expected.state
        jump, finish = data.draw(st.booleans()), data.draw(st.booleans())
        if data.draw(st.integers(0, 7)) == 0:
            if data.draw(st.booleans()):
                world.exact_ok = not world.exact_ok
            else:
                world.sufficient_ok = not world.sufficient_ok
        if state is S.PROPOSED:
            policy = data.draw(st.sampled_from([A.APPROVED, A.APPROVED, A.PENDING]))
            self.do_file(action_id, policy, data.draw(st.sampled_from(TTLS)))
        elif state is S.PENDING_APPROVAL:
            if world.approval is A.PENDING:
                if data.draw(st.sampled_from([True, True, False])):
                    self.approve(world, data.draw(st.sampled_from(TTLS)))
                else:
                    world.approval = A.DENIED
            self.do_poll(action_id, check_basis=True)
        elif state is S.AUTHORIZED or action_id in self.retryable():
            self.do_commit(self.do_begin(action_id), wait=jump or finish)
        elif state is S.DISPATCHED:
            due = expected.attempts[-1] + T
            if not jump and self.vt < due and finish:
                early = Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)
                attempt_no = len(expected.attempts)
                self.refused(lambda: self.store.answer(action_id, attempt_no, early, self.vt))
            if jump:
                self.vt = max(self.vt, due)
            self.do_answer(action_id, data, finish)
        else:
            if jump:
                self.vt = max(self.vt, expected.attempts[-1] + SETTLE)
            if expected.escalated:
                self.do_attest(action_id, finish)
            else:
                assert self.contract is Contract.C1, "only C1 waits without escalating"
                lag = data.draw(st.sampled_from([0, 1, LAG]))
                self.do_probe(action_id, data.draw(st.booleans()), finish, lag)

    @precondition(lambda self: self.expected)
    @rule(data=st.data())
    def misplaced_step(self, data: st.DataObject) -> None:
        """A step called in a state where it does not apply is refused and changes nothing."""
        action_id = data.draw(st.sampled_from(sorted(self.expected)))
        state = self.expected[action_id].state
        store, vt = self.store, self.vt
        fresh = BasisCheck(True, True)
        candidates: list[Callable[[], object]] = []
        seen = self.seq(action_id)
        if state is not S.PROPOSED:
            candidates.append(
                lambda: store.file_approval(action_id, fresh, "AP-x", vt, seen_seq=seen)
            )
        if state is not S.PENDING_APPROVAL:
            candidates.append(
                lambda: store.poll_approval(action_id, A.APPROVED, fresh, vt, seen_seq=seen)
            )
        if state is not S.DISPATCHED:
            answer = Answer(AnswerClass.UNKNOWN, Reason.TRANSPORT_ERROR)
            candidates.append(lambda: store.answer(action_id, 1, answer, vt))
        elif vt < self.expected[action_id].attempts[-1] + T:
            attempt_no = len(self.expected[action_id].attempts)
            early = Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)
            candidates.append(lambda: store.answer(action_id, attempt_no, early, vt))
        if state is not S.OUTCOME_UNKNOWN:
            candidates.append(lambda: store.probe(action_id, ProbeAnswer(answered=False), vt))
            candidates.append(
                lambda: store.attest(action_id, Resolution.COMMITTED, "PO-1", "resolver", vt)
            )
        self.refused(data.draw(st.sampled_from(candidates)))

    # Checks after every step ----------------------------------------------------------------

    @invariant()
    def records_keep_the_invariants(self) -> None:
        snapshot = self.store.snapshot()
        assert check_invariants(snapshot, SETTINGS) == []
        previous = self.previous
        assert snapshot.events[: len(previous.events)] == previous.events, "the log was rewritten"
        assert snapshot.tool_calls[: len(previous.tool_calls)] == previous.tool_calls
        for action_id, before in previous.actions.items():
            if before.effect_id is not None:
                assert snapshot.actions[action_id].effect_id == before.effect_id, "I1"
        for grant_id, grant in previous.grants.items():
            assert snapshot.grants[grant_id].action_id == grant.action_id, "I2"
        self.previous = snapshot

    @invariant()
    def records_agree_with_the_model(self) -> None:
        snapshot = self.store.snapshot()
        assert sorted(snapshot.actions) == sorted(self.expected)
        assert snapshot.run_closed == self.closed
        assert snapshot.closed_intents == self.closed_intents
        assert dict(snapshot.reports) == self.reports
        events: dict[str, int] = {}
        reasons: dict[str, Reason | Note] = {}
        for event in snapshot.events:
            events[event.action_id] = events.get(event.action_id, 0) + 1
            if event.kind is EventKind.TRANSITION:
                reasons[event.action_id] = event.reason
        for action_id, expected in self.expected.items():
            action = snapshot.actions[action_id]
            assert action.state is expected.state, action_id
            assert reasons[action_id] is expected.reason, action_id
            assert action.effect_id == expected.effect_id, action_id
            attempts = snapshot.attempts.get(action_id, ())
            assert [t.sent_vt for t in attempts] == expected.attempts, action_id
            grant = snapshot.grants.get(action.grant_id) if action.grant_id else None
            assert (grant.state if grant else None) is expected.grant, action_id
            escalated = f"ESC-{action_id[2:]}" in snapshot.escalations
            assert escalated == expected.escalated, action_id
            assert events[action_id] == expected.events, action_id

    @invariant()
    def no_action_contradicts_the_world(self) -> None:
        for action_id, expected in self.expected.items():
            world = self.world[action_id]
            if expected.state is S.FAILED:
                assert world.effect_id is None and world.may_commit_until is None, action_id
            if expected.state is S.CONFIRMED:
                assert expected.effect_id == world.effect_id, action_id
            checked_sends = self.contract is not Contract.C3
            if checked_sends and world.effect_id is not None and world.expires_at is not None:
                assert world.effect_vt < world.expires_at, f"{action_id} outlived its approval"
            assert all(b - a >= T for a, b in itertools.pairwise(expected.attempts))


# Rare interleavings, such as a dispatch check overtaken by another event, need more than
# Hypothesis's default 100 examples to appear reliably. The floor keeps larger profiles.
MACHINE_SETTINGS = settings(max_examples=max(300, settings().max_examples))


@MACHINE_SETTINGS
class GatewayC0(GatewayMachine):
    contract = Contract.C0


@MACHINE_SETTINGS
class GatewayC1(GatewayMachine):
    contract = Contract.C1


@MACHINE_SETTINGS
class GatewayC2(GatewayMachine):
    contract = Contract.C2


@MACHINE_SETTINGS
class GatewayC3(GatewayMachine):
    contract = Contract.C3


TestGatewayC0 = GatewayC0.TestCase
TestGatewayC1 = GatewayC1.TestCase
TestGatewayC2 = GatewayC2.TestCase
TestGatewayC3 = GatewayC3.TestCase


# The same machine against Postgres --------------------------------------------------------


class RunDatabase(Protocol):
    """What the Postgres machines use of the `gateway_database` fixture."""

    @property
    def gateway(self) -> str: ...

    def reset(self) -> None: ...


class GatewayPgMachine(GatewayMachine):
    """The machine with `TwinStore`: Postgres, with the in-memory store as the oracle."""

    database: ClassVar[RunDatabase | None] = None
    """The run database, set by the `pg_machines` fixture while a Postgres machine runs."""

    def new_store(self) -> MemoryStore | TwinStore:
        database = GatewayPgMachine.database
        if database is None:
            raise RuntimeError("the Postgres machines need the pg_machines fixture")
        database.reset()
        kwargs: dict[str, Any] = {
            "task_id": "TASK-1",
            "intents": INTENTS,
            "contract": self.contract,
            "settings": SETTINGS,
        }
        return TwinStore(MemoryStore(**kwargs), PgStore(database.gateway, **kwargs))

    def teardown(self) -> None:
        if isinstance(self.store, TwinStore):
            self.store.pg.close()


@pytest.fixture
def pg_machines(gateway_database: RunDatabase) -> Iterator[None]:
    """Point the Postgres machines at the session's run database while one runs."""
    GatewayPgMachine.database = gateway_database
    yield
    GatewayPgMachine.database = None


# Every step reaches the database several times, so the Postgres machines run fewer
# examples; the nightly profile scales them up with everything else.
PG_MACHINE_SETTINGS = settings(max_examples=max(25, settings().max_examples // 10))


@PG_MACHINE_SETTINGS
class GatewayPgC0(GatewayPgMachine):
    contract = Contract.C0


@PG_MACHINE_SETTINGS
class GatewayPgC1(GatewayPgMachine):
    contract = Contract.C1


@PG_MACHINE_SETTINGS
class GatewayPgC2(GatewayPgMachine):
    contract = Contract.C2


@PG_MACHINE_SETTINGS
class GatewayPgC3(GatewayPgMachine):
    contract = Contract.C3


def _on_postgres(case: type[Any]) -> type[Any]:
    return cast(type[Any], pytest.mark.pg(pytest.mark.usefixtures("pg_machines")(case)))


TestGatewayPgC0 = _on_postgres(GatewayPgC0.TestCase)
TestGatewayPgC1 = _on_postgres(GatewayPgC1.TestCase)
TestGatewayPgC2 = _on_postgres(GatewayPgC2.TestCase)
TestGatewayPgC3 = _on_postgres(GatewayPgC3.TestCase)
