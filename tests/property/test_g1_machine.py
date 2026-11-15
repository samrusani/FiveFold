"""A Hypothesis rule-based state machine for G1's records (0003, testing plan; 0008).

G1 is the store with action identity off. Every write call is a new action, even with
the same key. The approval request is filed with no basis check; an approval authorizes
the action, and a denial, revocation, expiry or timeout rejects it. The check before the
one send asks for the approval only, and commits only if no event came since the check
began, so other steps can come between a check and its commit. An answer settles the one
attempt; an unknown one leaves the action OutcomeUnknown for good, and a restart moves an
open attempt there too. Retries, probes, attestations, reports and run closing are
always refused.

After every step the machine checks the invariants as scoped for G1, that the log only
grew and effect ids never changed, and G1's own safety rules: at most one attempt per
action, no escalation and no note, and every send made after a check that found the
approval approved and outliving the send by the settle time (0002, rule 17; 0010).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

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
from fivefold.gateway.records import (
    ActionResult,
    Answer,
    ApprovalStatus,
    CheckRecord,
    Claim,
    ClaimStatus,
    ProbeAnswer,
    Resolution,
    Snapshot,
    WriteCall,
)
from fivefold.gateway.state_machine import (
    ActionState,
    AnswerClass,
    CheckKind,
    EventKind,
    GatewayRuleError,
    GatewaySettings,
    Reason,
)

S = ActionState
A = ApprovalStatus
SETTINGS = GatewaySettings(
    approval_wait_ms=3_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
)
SETTLE = SETTINGS.settle_ms
T = SETTINGS.dispatch_timeout_ms
TTLS = (SETTLE // 2, SETTLE + 2 * T, 10**9)
"""Approval lifetimes: ends before a send could commit, ends soon after, outlasts all."""
INTENTS = {"REQ-1": "submit_purchase_order", "REQ-2": "reserve_inventory"}
MAX_ACTIONS = 6
"""Enough actions for interleavings, few enough that each one gets far."""
LIVE = (S.PROPOSED, S.PENDING_APPROVAL, S.AUTHORIZED, S.DISPATCHED)
POLICIES = st.sampled_from([A.APPROVED, A.APPROVED, A.PENDING, A.DENIED])
ANSWERS = st.sampled_from(["committed", "rejected", "timeout", "server_error"])
DEAD = {A.DENIED: Reason.APPROVAL_DENIED, A.REVOKED: Reason.APPROVAL_REVOKED}


@dataclass
class Authority:
    """What the approvals service holds for one action's approval request."""

    status: ApprovalStatus
    expires_at: int | None = None

    def now(self, vt: int) -> tuple[ApprovalStatus, int | None]:
        """The status it reports at `vt`, with the expiry exactly when approved."""
        if self.status is A.APPROVED and self.expires_at is not None:
            if self.expires_at <= vt:
                return A.EXPIRED, None
            return A.APPROVED, self.expires_at
        return self.status, None


class G1Machine(RuleBasedStateMachine):
    contract: ClassVar[Contract]

    def __init__(self) -> None:
        super().__init__()
        self.vt = 0
        self.store = MemoryStore(
            task_id="TASK-1",
            intents=INTENTS,
            contract=self.contract,
            settings=SETTINGS,
            identity=False,
        )
        self.authority: dict[str, Authority] = {}
        self.checks: list[tuple[str, CheckRecord]] = []
        self.effects = 0
        self.previous: Snapshot = self.store.snapshot()

    def tick(self, dt: int = 1) -> int:
        self.vt += dt
        return self.vt

    def ids(self, *states: ActionState) -> list[str]:
        actions = self.store.snapshot().actions
        return sorted(a for a, action in actions.items() if action.state in states)

    def state(self, action_id: str) -> ActionState:
        return self.store.view(action_id).action.state

    def seq(self, action_id: str) -> int:
        return self.store.view(action_id).last_seq

    def refused(self, call: Callable[[], object]) -> None:
        """Assert that a step is refused and changes nothing."""
        before = self.store.snapshot()
        try:
            call()
        except (GatewayRuleError, ValueError):
            pass
        else:
            raise AssertionError("the step should have been refused")
        assert self.store.snapshot() is before

    # Write calls and approvals --------------------------------------------------------------

    @initialize()
    def one_action_per_intent(self) -> None:
        for intent in sorted(INTENTS):
            self.propose(intent)

    @precondition(lambda self: len(self.store.snapshot().actions) < MAX_ACTIONS)
    @rule(intent=st.sampled_from(sorted(INTENTS)))
    def propose(self, intent: str) -> None:
        """The same call each time: G1 has no action identity, so each is a new action."""
        vt = self.tick()
        read = self.store.record_read("f", 1, {}, vt)
        call = WriteCall(intent, INTENTS[intent], "t", {}, "same-key", (read.read_id,), ("f",))
        known = set(self.store.snapshot().actions)
        result = self.store.propose(call, vt)
        assert isinstance(result, ActionResult) and result.action_id not in known
        assert result.state is S.PROPOSED

    @precondition(lambda self: self.ids(S.PROPOSED))
    @rule(data=st.data(), policy=POLICIES, ttl=st.sampled_from(TTLS))
    def file(self, data: st.DataObject, policy: ApprovalStatus, ttl: int) -> None:
        self.do_file(data.draw(st.sampled_from(self.ids(S.PROPOSED))), policy, ttl)

    def do_file(self, action_id: str, policy: ApprovalStatus, ttl: int) -> None:
        vt = self.tick()
        expires_at = vt + ttl if policy is A.APPROVED else None
        self.authority[action_id] = Authority(policy, expires_at)
        approval_id = f"AP-{action_id}"
        self.store.file_approval(action_id, None, approval_id, vt, seen_seq=self.seq(action_id))
        assert self.state(action_id) is S.PENDING_APPROVAL

    def undecided(self) -> list[str]:
        return [a for a, auth in sorted(self.authority.items()) if auth.status is A.PENDING]

    def granted(self) -> list[str]:
        return [a for a, auth in sorted(self.authority.items()) if auth.status is A.APPROVED]

    @precondition(lambda self: self.undecided())
    @rule(data=st.data(), approve=st.booleans(), ttl=st.sampled_from(TTLS))
    def human_decides(self, data: st.DataObject, approve: bool, ttl: int) -> None:
        action_id = data.draw(st.sampled_from(self.undecided()))
        if approve:
            self.authority[action_id] = Authority(A.APPROVED, self.vt + ttl)
        else:
            self.authority[action_id] = Authority(A.DENIED)

    @precondition(lambda self: self.granted())
    @rule(data=st.data())
    def revoke(self, data: st.DataObject) -> None:
        """The authority can revoke a granted approval at any time, seen or not (0010)."""
        action_id = data.draw(st.sampled_from(self.granted()))
        self.authority[action_id] = Authority(A.REVOKED)

    @rule(dt=st.sampled_from([1, T, SETTLE, SETTINGS.approval_wait_ms]))
    def time_passes(self, dt: int) -> None:
        self.tick(dt)

    @precondition(lambda self: self.ids(S.PENDING_APPROVAL))
    @rule(data=st.data())
    def poll(self, data: st.DataObject) -> None:
        self.do_poll(data.draw(st.sampled_from(self.ids(S.PENDING_APPROVAL))))

    def do_poll(self, action_id: str) -> None:
        view = self.store.view(action_id)
        assert view.grant is not None
        status, _ = self.authority[action_id].now(self.vt)
        due = view.grant.requested_vt + SETTINGS.approval_wait_ms
        seen = view.last_seq
        if status is A.PENDING and self.vt >= due:
            self.store.time_out_approval(action_id, status, self.vt, seen_seq=seen)
            expected, reason = S.REJECTED, Reason.APPROVAL_TIMEOUT
        else:
            self.store.poll_approval(action_id, status, None, self.vt, seen_seq=seen)
            if status is A.APPROVED:
                expected, reason = S.AUTHORIZED, Reason.APPROVED
            elif status is A.PENDING:
                expected, reason = S.PENDING_APPROVAL, Reason.APPROVAL_REQUESTED
            elif status is A.EXPIRED:
                expected, reason = S.REJECTED, Reason.APPROVAL_EXPIRED
            else:
                expected, reason = S.REJECTED, DEAD[status]
        assert self.store.status(action_id, self.vt).reason is reason
        assert self.state(action_id) is expected

    # The check, the send and the answer -----------------------------------------------------

    @precondition(lambda self: self.ids(S.AUTHORIZED))
    @rule(data=st.data())
    def check(self, data: st.DataObject) -> None:
        """The dispatcher's check, made without a lock; it commits in `send`, later."""
        action_id = data.draw(st.sampled_from(self.ids(S.AUTHORIZED)))
        self.checks.append((action_id, self.do_check(action_id)))

    def do_check(self, action_id: str) -> CheckRecord:
        ticket = self.store.begin_check(action_id)
        assert (ticket.kind, ticket.attempt_no) == (CheckKind.AUTHORITY_ONLY, 1)
        status, expires_at = self.authority[action_id].now(self.vt)
        return ticket.record(approval=status, basis=None, check_vt=self.vt, expires_at=expires_at)

    @precondition(lambda self: self.checks)
    @rule(data=st.data(), delay=st.sampled_from([0, 1, SETTLE]))
    def send(self, data: st.DataObject, delay: int) -> None:
        action_id, record = self.checks.pop(data.draw(st.integers(0, len(self.checks) - 1)))
        self.do_send(action_id, record, delay)

    def do_send(self, action_id: str, record: CheckRecord, delay: int) -> None:
        vt = self.tick(delay)
        overtaken = record.seen_seq != self.seq(action_id)
        attempt = self.store.dispatch(action_id, record, vt)
        state = self.state(action_id)
        reason = self.store.status(action_id, vt).reason
        if overtaken:
            assert attempt is None, "a check overtaken by another event commits nothing"
        elif record.passes(vt, SETTLE):
            assert (attempt is not None, state, reason) == (True, S.DISPATCHED, Reason.DISPATCHED)
        else:
            dead = (
                Reason.APPROVAL_REVOKED if record.approval is A.REVOKED else Reason.APPROVAL_EXPIRED
            )
            assert (attempt, state, reason) == (None, S.REJECTED, dead), (
                "dead authority sends nothing"
            )

    @precondition(lambda self: self.ids(S.DISPATCHED))
    @rule(data=st.data(), kind=ANSWERS)
    def answer(self, data: st.DataObject, kind: str) -> None:
        self.do_answer(data.draw(st.sampled_from(self.ids(S.DISPATCHED))), kind)

    def do_answer(self, action_id: str, kind: str) -> None:
        (attempt,) = self.store.view(action_id).attempts
        if kind == "committed":
            self.effects += 1
            answer = Answer(AnswerClass.COMMITTED, Reason.COMMITTED, f"PO-{self.effects:04d}")
            expected = S.CONFIRMED
        elif kind == "rejected":
            checked = self.contract.has(Capability.CONDITIONAL_WRITE)
            why = Reason.PRECONDITION_FAILED if checked else Reason.DESTINATION_REJECTED
            answer = Answer(AnswerClass.REJECTED, why)
            expected = S.FAILED
        elif kind == "timeout":
            self.vt = max(self.vt, attempt.sent_vt + T)
            answer = Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)
            expected = S.OUTCOME_UNKNOWN
        else:
            answer = Answer(AnswerClass.UNKNOWN, Reason.SERVER_ERROR)
            expected = S.OUTCOME_UNKNOWN
        self.store.answer(action_id, 1, answer, self.tick(0))
        assert self.state(action_id) is expected

    @precondition(lambda self: self.ids(*LIVE))
    @rule(data=st.data())
    def progress(self, data: st.DataObject) -> None:
        """Move one live action along its usual path, several steps in one rule."""
        action_id = data.draw(st.sampled_from(self.ids(*LIVE)))
        for _ in range(data.draw(st.integers(1, 4))):
            state = self.state(action_id)
            if state is S.PROPOSED:
                self.do_file(action_id, data.draw(POLICIES), data.draw(st.sampled_from(TTLS)))
            elif state is S.PENDING_APPROVAL:
                self.do_poll(action_id)
            elif state is S.AUTHORIZED:
                delay = data.draw(st.sampled_from([0, 1, SETTLE]))
                self.do_send(action_id, self.do_check(action_id), delay)
            elif state is S.DISPATCHED:
                self.do_answer(action_id, data.draw(ANSWERS))
            else:
                return

    @rule()
    def restart(self) -> None:
        """The gateway crashes and restarts: checks in memory are gone, and recovery moves
        each open attempt to OutcomeUnknown (0003, startup recovery)."""
        dispatched = self.ids(S.DISPATCHED)
        self.checks.clear()
        self.store.restart(self.tick())
        for action_id in dispatched:
            assert self.state(action_id) is S.OUTCOME_UNKNOWN

    # What G1 never does ----------------------------------------------------------------------

    @precondition(lambda self: self.store.snapshot().actions)
    @rule(data=st.data())
    def status(self, data: st.DataObject) -> None:
        action_id = data.draw(st.sampled_from(sorted(self.store.snapshot().actions)))
        sent = {"action_id": action_id, "wait_ms": data.draw(st.integers(0, 60_000))}
        result = self.store.status(action_id, self.tick(0), args=sent)
        assert result.state is self.state(action_id) and result.reconcile is None
        logged = self.store.snapshot().tool_calls[-1]
        assert (logged.tool, logged.args) == ("get_action_status", sent), "logged as sent"

    @precondition(lambda self: self.ids(S.OUTCOME_UNKNOWN))
    @rule(data=st.data())
    def no_recovery(self, data: st.DataObject) -> None:
        action_id = data.draw(st.sampled_from(self.ids(S.OUTCOME_UNKNOWN)))
        vt = self.tick(SETTLE)
        retry = CheckRecord(CheckKind.EXEMPT, None, None, self.seq(action_id), vt)
        steps: list[Callable[[], object]] = [
            lambda: self.store.begin_check(action_id),
            lambda: self.store.retry(action_id, retry, vt),
            lambda: self.store.probe(action_id, ProbeAnswer(answered=True, as_of_vt=vt), vt),
            lambda: self.store.attest(action_id, Resolution.NOT_COMMITTED, None, "resolver", vt),
        ]
        self.refused(data.draw(st.sampled_from(steps)))

    @rule(close=st.booleans())
    def no_report_check_or_closing(self, close: bool) -> None:
        vt = self.tick(0)
        if close:
            self.refused(lambda: self.store.close_run(vt))
        else:
            claims = [Claim(intent, ClaimStatus.NOT_DONE) for intent in sorted(INTENTS)]
            self.refused(lambda: self.store.submit_report(claims, vt, worker="W1"))

    # Checks after every step ----------------------------------------------------------------

    @invariant()
    def records_keep_the_invariants_as_scoped_for_g1(self) -> None:
        snapshot = self.store.snapshot()
        assert check_invariants(snapshot, SETTINGS) == []
        previous = self.previous
        assert snapshot.events[: len(previous.events)] == previous.events, "the log was rewritten"
        for action_id, before in previous.actions.items():
            if before.effect_id is not None:
                assert snapshot.actions[action_id].effect_id == before.effect_id, "I1"
        self.previous = snapshot

    @invariant()
    def g1_sends_once_after_a_passing_authority_check(self) -> None:
        snapshot = self.store.snapshot()
        assert not snapshot.escalations and not snapshot.run_closed and not snapshot.reports
        assert all(event.kind is EventKind.TRANSITION for event in snapshot.events)
        for action_id, attempts in snapshot.attempts.items():
            assert len(attempts) <= 1, action_id
            for attempt in attempts:
                check = attempt.check
                assert (check.kind, check.basis, check.approval) == (
                    CheckKind.AUTHORITY_ONLY,
                    None,
                    A.APPROVED,
                )
                assert check.approval_expires_at is not None
                assert check.approval_expires_at > attempt.sent_vt + SETTLE


# As in the G2 machines, rare interleavings need more than the default 100 examples. G1
# behaves the same at every level; C3 adds the destination's own precondition rejections.
MACHINE_SETTINGS = settings(max_examples=max(300, settings().max_examples))


@MACHINE_SETTINGS
class G1C0(G1Machine):
    contract = Contract.C0


@MACHINE_SETTINGS
class G1C3(G1Machine):
    contract = Contract.C3


TestG1C0 = G1C0.TestCase
TestG1C3 = G1C3.TestCase
