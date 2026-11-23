"""The gateway's steps, one rule at a time, through both stores (0001 to 0005).

Each test drives one path from the decision records and checks the records it leaves.
The store checks I1 to I7 after every operation, so every test also checks them. Every
test runs twice: on the in-memory store, and on PostgreSQL checked against the in-memory
store at every operation (marker `pg`), so each rule is also shown to hold in the
database.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import ClassVar

import pytest

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.records import (
    ActionResult,
    Answer,
    ApprovalStatus,
    BasisCheck,
    Claim,
    ClaimStatus,
    ProbeAnswer,
    Proposal,
    Refusal,
    RefusalReason,
    ReportAccepted,
    Resolution,
    StaleFact,
    WriteCall,
)
from fivefold.gateway.state_machine import (
    ActionState,
    AnswerClass,
    CheckKind,
    EventKind,
    GatewaySettings,
    GrantState,
    IllegalTransition,
    Note,
    Reason,
    ReconcileHow,
    check_transition,
)
from fivefold.gateway.steps import StepRefused
from fivefold.gateway.twin import TwinStore

S = ActionState
SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
)
SETTLE = SETTINGS.settle_ms
TWO_WORKERS = replace(SETTINGS, agent_workers=2)
"""A run where fault F9 starts a second agent worker, so two reports are expected (0009)."""
FRESH = BasisCheck(exact_ok=True, sufficient_ok=True)
EXACT_FRESH = BasisCheck(exact_ok=True, sufficient_ok=None)
QUOTE_STALE = StaleFact("quote/S-1/W-100", 1, 2, ("unit_price_minor",))
TIMEOUT = Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)
FAR = 10**9
"""An approval expiry far beyond any test's virtual time."""


@pytest.fixture(autouse=True)
def _on_each_store(make_store: Callable[..., MemoryStore | TwinStore]) -> None:
    Run.factory = make_store


class Run:
    """One run's store with a virtual clock and shortcuts to common states."""

    factory: ClassVar[Callable[..., MemoryStore | TwinStore]]

    def __init__(
        self, contract: Contract, *, identity: bool = True, settings: GatewaySettings = SETTINGS
    ) -> None:
        self.store = Run.factory(
            task_id="TASK-1",
            intents={"REQ-1": "submit_purchase_order", "REQ-2": "submit_purchase_order"},
            contract=contract,
            settings=settings,
            identity=identity,
        )
        self.vt = 0
        self.approvals = 0

    def at(self, dt: int = 0) -> int:
        self.vt += dt
        return self.vt

    def state(self, action_id: str) -> ActionState:
        return self.store.snapshot().actions[action_id].state

    def seq(self, action_id: str) -> int:
        """The seq a check starting now would see."""
        return self.store.view(action_id).last_seq

    def write(
        self, intent: str = "REQ-1", key: str = "k1", quote_version: int = 1, *, quantity: int = 100
    ) -> ActionResult | Refusal:
        """A write call that cites fresh reads of the order's two dependencies."""
        vt = self.at(1)
        quote = self.store.record_read("quote/S-1/W-100", quote_version, {}, vt)
        budget = self.store.record_read("budget/CC-OPS", 1, {}, vt)
        return self.store.propose(
            WriteCall(
                intent,
                "submit_purchase_order",
                "supplier/S-1",
                {"quantity": quantity},
                key,
                (quote.read_id, budget.read_id),
                ("quote/S-1/W-100", "budget/CC-OPS"),
            ),
            vt,
        )

    def propose(self, intent: str = "REQ-1", key: str = "k1") -> str:
        result = self.write(intent, key)
        assert isinstance(result, ActionResult)
        return result.action_id

    def pending(self, intent: str = "REQ-1", key: str = "k1") -> str:
        action_id = self.propose(intent, key)
        self.approvals += 1
        self.store.file_approval(
            action_id, FRESH, f"AP-{self.approvals}", self.at(1), seen_seq=self.seq(action_id)
        )
        return action_id

    def authorized(self, intent: str = "REQ-1", key: str = "k1") -> str:
        action_id = self.pending(intent, key)
        self.store.poll_approval(
            action_id, ApprovalStatus.APPROVED, FRESH, self.at(1), seen_seq=self.seq(action_id)
        )
        return action_id

    def sent(self, intent: str = "REQ-1", key: str = "k1") -> str:
        action_id = self.authorized(intent, key)
        ticket = self.store.begin_check(action_id)
        check = ticket.record(
            approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=self.at(1), expires_at=FAR
        )
        assert self.store.dispatch(action_id, check, self.at(1)) is not None
        return action_id

    def unknown(self, intent: str = "REQ-1", key: str = "k1") -> str:
        action_id = self.sent(intent, key)
        self.store.answer(action_id, 1, TIMEOUT, self.at(SETTINGS.dispatch_timeout_ms))
        return action_id

    def retry(self, action_id: str, basis: BasisCheck | None = EXACT_FRESH) -> bool:
        ticket = self.store.begin_check(action_id)
        self.vt = max(self.vt, ticket.not_before_vt)
        if ticket.kind is CheckKind.EXEMPT:
            check = ticket.record(approval=None, basis=None, check_vt=self.vt)
        else:
            check = ticket.record(
                approval=ApprovalStatus.APPROVED, basis=basis, check_vt=self.vt, expires_at=FAR
            )
        return self.store.retry(action_id, check, self.vt) is not None


# Approval -----------------------------------------------------------------------------


def test_a_stale_basis_is_rejected_before_any_approval_is_requested() -> None:
    run = Run(Contract.C2)
    action_id = run.propose()
    stale = BasisCheck(exact_ok=False, sufficient_ok=True, stale=(QUOTE_STALE,))
    run.store.file_approval(action_id, stale, None, run.at(1), seen_seq=run.seq(action_id))
    snapshot = run.store.snapshot()
    assert snapshot.actions[action_id].state is S.REJECTED
    assert not snapshot.grants
    result = run.store.status(action_id, run.at())
    assert result.reason is Reason.STALE_INPUT
    assert result.stale == (QUOTE_STALE,)


def test_approval_binds_one_grant_to_the_action() -> None:
    run = Run(Contract.C2)
    action_id = run.authorized()
    snapshot = run.store.snapshot()
    grant = snapshot.grants["G-0001"]
    assert (grant.action_id, grant.approval_id, grant.state) == (
        action_id,
        "AP-1",
        GrantState.ACTIVE,
    )
    assert snapshot.actions[action_id].grant_id == "G-0001"


@pytest.mark.parametrize(
    ("approval", "basis", "reason", "grant_state"),
    [
        (ApprovalStatus.DENIED, None, Reason.APPROVAL_DENIED, GrantState.DENIED),
        (ApprovalStatus.APPROVED, BasisCheck(True, False), Reason.STALE_INPUT, GrantState.RELEASED),
        (ApprovalStatus.PENDING, BasisCheck(False, True), Reason.STALE_INPUT, GrantState.RELEASED),
    ],
)
def test_a_waiting_action_is_rejected(
    approval: ApprovalStatus, basis: BasisCheck | None, reason: Reason, grant_state: GrantState
) -> None:
    run = Run(Contract.C2)
    action_id = run.pending()
    run.store.poll_approval(action_id, approval, basis, run.at(1), seen_seq=run.seq(action_id))
    snapshot = run.store.snapshot()
    assert snapshot.actions[action_id].state is S.REJECTED
    assert snapshot.events[-1].reason is reason
    assert snapshot.grants["G-0001"].state is grant_state


def test_a_pending_poll_with_a_fresh_basis_changes_nothing() -> None:
    run = Run(Contract.C2)
    action_id = run.pending()
    before = run.store.snapshot()
    seen = run.seq(action_id)
    run.store.poll_approval(action_id, ApprovalStatus.PENDING, FRESH, run.at(1), seen_seq=seen)
    run.store.poll_approval(action_id, ApprovalStatus.PENDING, None, run.at(1), seen_seq=seen)
    assert run.store.snapshot() == before


def test_the_approval_times_out_only_after_approval_wait_ms() -> None:
    run = Run(Contract.C2)
    action_id = run.pending()
    seen, pending = run.seq(action_id), ApprovalStatus.PENDING
    with pytest.raises(StepRefused):
        early = run.at(SETTINGS.approval_wait_ms - 1)
        run.store.time_out_approval(action_id, pending, early, seen_seq=seen)
    run.store.time_out_approval(action_id, pending, run.at(1), seen_seq=seen)
    snapshot = run.store.snapshot()
    assert snapshot.events[-1].reason is Reason.APPROVAL_TIMEOUT
    assert snapshot.grants["G-0001"].state is GrantState.TIMED_OUT


@pytest.mark.parametrize(
    ("approval", "reason", "grant_state"),
    [
        (ApprovalStatus.REVOKED, Reason.APPROVAL_REVOKED, GrantState.REVOKED),
        (ApprovalStatus.EXPIRED, Reason.APPROVAL_EXPIRED, GrantState.EXPIRED),
    ],
)
def test_an_approval_already_dead_at_the_first_poll_rejects_the_action(
    approval: ApprovalStatus, reason: Reason, grant_state: GrantState
) -> None:
    """Record 0010, change 1: T5 names a revocation or expiry the gateway saw too late."""
    run = Run(Contract.C2)
    action_id = run.pending()
    stale = BasisCheck(exact_ok=False, sufficient_ok=True, stale=(QUOTE_STALE,))
    run.store.poll_approval(action_id, approval, stale, run.at(1), seen_seq=run.seq(action_id))
    snapshot = run.store.snapshot()
    assert snapshot.actions[action_id].state is S.REJECTED
    assert snapshot.events[-1].reason is reason
    assert snapshot.grants["G-0001"].state is grant_state


# Dispatch -----------------------------------------------------------------------------


def test_the_first_send_consumes_the_grant_and_records_its_check() -> None:
    run = Run(Contract.C2)
    action_id = run.sent()
    snapshot = run.store.snapshot()
    (attempt,) = snapshot.attempts[action_id]
    assert attempt.idempotency_key == action_id
    assert attempt.check.kind is CheckKind.FULL
    assert attempt.check.passes(attempt.sent_vt, SETTLE)
    grant = snapshot.grants["G-0001"]
    assert (grant.state, grant.consumed_vt) == (GrantState.CONSUMED, attempt.sent_vt)


def test_a_check_overtaken_by_a_newer_event_commits_nothing() -> None:
    run = Run(Contract.C2)
    action_id = run.authorized()
    ticket = run.store.begin_check(action_id)
    run.write(quote_version=2)
    assert run.store.snapshot().events[-1].reason is Note.BASIS_UPDATED
    check = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=run.at(1), expires_at=FAR
    )
    assert run.store.dispatch(action_id, check, run.at(1)) is None
    assert run.state(action_id) is S.AUTHORIZED
    assert action_id not in run.store.snapshot().attempts


@pytest.mark.parametrize(
    ("approval", "basis", "reason", "grant_state"),
    [
        (ApprovalStatus.REVOKED, FRESH, Reason.APPROVAL_REVOKED, GrantState.REVOKED),
        (ApprovalStatus.EXPIRED, FRESH, Reason.APPROVAL_EXPIRED, GrantState.EXPIRED),
        (ApprovalStatus.APPROVED, BasisCheck(True, False), Reason.STALE_INPUT, GrantState.RELEASED),
    ],
)
def test_a_failing_dispatch_check_rejects_without_sending(
    approval: ApprovalStatus, basis: BasisCheck, reason: Reason, grant_state: GrantState
) -> None:
    run = Run(Contract.C2)
    action_id = run.authorized()
    ticket = run.store.begin_check(action_id)
    expires_at = FAR if approval is ApprovalStatus.APPROVED else None
    check = ticket.record(approval=approval, basis=basis, check_vt=run.at(1), expires_at=expires_at)
    assert run.store.dispatch(action_id, check, run.at(1)) is None
    snapshot = run.store.snapshot()
    assert (snapshot.actions[action_id].state, snapshot.events[-1].reason) == (S.REJECTED, reason)
    assert snapshot.grants["G-0001"].state is grant_state
    assert action_id not in snapshot.attempts


def test_answers_come_only_for_the_open_attempt_and_timeouts_only_when_due() -> None:
    run = Run(Contract.C2)
    action_id = run.sent()
    due = run.store.snapshot().attempts[action_id][0].sent_vt + SETTINGS.dispatch_timeout_ms
    with pytest.raises(StepRefused, match="timeout"):
        run.store.answer(action_id, 1, TIMEOUT, due - 1)
    with pytest.raises(StepRefused, match="latest attempt"):
        run.store.answer(action_id, 2, TIMEOUT, due)
    run.store.answer(action_id, 1, Answer(AnswerClass.UNKNOWN, Reason.SERVER_ERROR), due)
    with pytest.raises(StepRefused):
        run.store.answer(action_id, 1, TIMEOUT, due)


@pytest.mark.parametrize(
    ("contract", "answer"),
    [
        (Contract.C0, Answer(AnswerClass.IN_PROGRESS, Reason.IN_PROGRESS)),
        (Contract.C1, Answer(AnswerClass.IN_PROGRESS, Reason.IN_PROGRESS)),
        (Contract.C2, Answer(AnswerClass.REJECTED, Reason.PRECONDITION_FAILED)),
    ],
)
def test_answers_a_contract_cannot_give_are_illegal(contract: Contract, answer: Answer) -> None:
    run = Run(contract)
    action_id = run.sent()
    with pytest.raises(IllegalTransition):
        run.store.answer(action_id, 1, answer, run.at(1))
    assert run.state(action_id) is S.DISPATCHED


def test_a_committed_answer_confirms_with_its_effect_id() -> None:
    run = Run(Contract.C0)
    action_id = run.sent()
    run.store.answer(
        action_id, 1, Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001"), run.at(1)
    )
    result = run.store.status(action_id, run.at())
    assert (result.state, result.effect_id, result.reconcile) == (S.CONFIRMED, "PO-0001", None)


def test_an_approval_expiring_before_the_send_could_commit_counts_as_expired() -> None:
    """Record 0010, change 4: the approval must outlive the send by the settle time S."""
    run = Run(Contract.C2)
    action_id = run.authorized()
    ticket = run.store.begin_check(action_id)
    vt = run.at(1)
    check = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=vt, expires_at=vt + SETTLE
    )
    assert run.store.dispatch(action_id, check, vt) is None
    snapshot = run.store.snapshot()
    assert snapshot.events[-1].reason is Reason.APPROVAL_EXPIRED
    assert snapshot.grants["G-0001"].state is GrantState.EXPIRED


def test_an_approval_that_outlives_the_settle_time_allows_the_send() -> None:
    run = Run(Contract.C2)
    action_id = run.authorized()
    ticket = run.store.begin_check(action_id)
    vt = run.at(1)
    check = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=vt, expires_at=vt + SETTLE + 1
    )
    assert run.store.dispatch(action_id, check, vt) is not None


def test_a_c2_retry_escalates_when_the_approval_expires_too_soon() -> None:
    run = Run(Contract.C2)
    action_id = run.unknown()
    ticket = run.store.begin_check(action_id)
    vt = max(run.at(), ticket.not_before_vt)
    check = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=EXACT_FRESH, check_vt=vt, expires_at=vt + SETTLE
    )
    assert run.store.retry(action_id, check, vt) is None
    snapshot = run.store.snapshot()
    assert snapshot.events[-1].reason is Note.ESCALATED
    # The note keeps what the approvals service said, and the expiry that made it count
    # as expired, so the record explains the escalation.
    assert snapshot.events[-1].details["approval"] == ApprovalStatus.APPROVED
    assert snapshot.events[-1].details["expires_at"] == vt + SETTLE
    assert snapshot.grants["G-0001"].state is GrantState.EXPIRED


def test_the_settle_time_counts_from_the_send_not_from_the_check() -> None:
    """Record 0010, change 4, as the maintainer decided: S counts from the send.

    The check runs at 100 ms before the send. The approval outlives the check by more than
    S, but not the send, so it counts as expired, and the event says when it expires.
    """
    run = Run(Contract.C2)
    action_id = run.authorized()
    ticket = run.store.begin_check(action_id)
    check_vt = run.at(1)
    sent_vt = check_vt + 100
    expires_at = check_vt + SETTLE + 50
    check = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=check_vt, expires_at=expires_at
    )
    assert run.store.dispatch(action_id, check, sent_vt) is None
    event = run.store.snapshot().events[-1]
    assert (event.reason, event.details["expires_at"]) == (Reason.APPROVAL_EXPIRED, expires_at)


def test_an_approved_check_records_the_approvals_expiry() -> None:
    run = Run(Contract.C2)
    ticket = run.store.begin_check(run.authorized())
    with pytest.raises(ValueError, match="expiry"):
        ticket.record(approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=run.at())
    with pytest.raises(ValueError, match="expiry"):
        ticket.record(approval=ApprovalStatus.REVOKED, basis=FRESH, check_vt=run.at(), expires_at=1)


# Recovery per contract (0005) -----------------------------------------------------------


def test_c0_escalates_at_once_and_only_a_human_settles_it() -> None:
    run = Run(Contract.C0)
    action_id = run.unknown()
    result = run.store.status(action_id, run.at())
    assert result.reconcile is not None
    assert (result.reconcile.how, result.reconcile.escalation_id) == (
        ReconcileHow.ESCALATED_TO_HUMAN,
        "ESC-0001",
    )
    with pytest.raises(StepRefused):
        run.store.begin_check(action_id)
    with pytest.raises(StepRefused, match="before vt"):
        run.store.attest(action_id, Resolution.NOT_COMMITTED, None, "resolver", run.at(1))
    sent_vt = run.store.snapshot().attempts[action_id][0].sent_vt
    run.store.attest(action_id, Resolution.NOT_COMMITTED, None, "resolver", sent_vt + SETTLE)
    snapshot = run.store.snapshot()
    assert snapshot.events[-1].reason is Reason.HUMAN_ATTESTED
    assert snapshot.escalations["ESC-0001"].resolution is Resolution.NOT_COMMITTED
    assert run.state(action_id) is S.FAILED


def test_a_human_can_attest_a_commit_at_once() -> None:
    run = Run(Contract.C0)
    action_id = run.unknown()
    run.store.attest(action_id, Resolution.COMMITTED, "PO-0007", "resolver", run.at(1))
    assert run.store.snapshot().actions[action_id].effect_id == "PO-0007"


def test_c1_fails_an_empty_status_only_after_the_settle_time() -> None:
    run = Run(Contract.C1)
    action_id = run.unknown()
    sent_vt = run.store.snapshot().attempts[action_id][0].sent_vt
    early = ProbeAnswer(answered=True, as_of_vt=sent_vt + SETTLE - 1)
    run.store.probe(action_id, early, max(run.at(), sent_vt + SETTLE - 1))
    assert run.state(action_id) is S.OUTCOME_UNKNOWN
    assert run.store.snapshot().events[-1].reason is Note.PROBE
    settled = ProbeAnswer(answered=True, as_of_vt=sent_vt + SETTLE)
    run.store.probe(action_id, settled, sent_vt + SETTLE)
    assert run.state(action_id) is S.FAILED
    assert run.store.snapshot().events[-1].reason is Reason.STATUS_SETTLED_EMPTY


def test_c1_confirms_a_listed_effect() -> None:
    run = Run(Contract.C1)
    action_id = run.unknown()
    run.store.probe(action_id, ProbeAnswer(True, ("PO-0003",), run.at()), run.at())
    assert run.store.snapshot().actions[action_id].effect_id == "PO-0003"


def test_c1_escalates_after_max_probes_and_stops_probing() -> None:
    run = Run(Contract.C1)
    action_id = run.unknown()
    for _ in range(SETTINGS.max_probes):
        run.store.probe(action_id, ProbeAnswer(answered=False), run.at(100))
    snapshot = run.store.snapshot()
    assert [e.reason for e in snapshot.events[-2:]] == [Note.PROBE, Note.ESCALATED]
    with pytest.raises(StepRefused, match="escalated"):
        run.store.probe(action_id, ProbeAnswer(answered=False), run.at(100))


def test_c2_retries_with_the_same_key_after_the_backoff() -> None:
    run = Run(Contract.C2)
    action_id = run.sent()
    server_error = Answer(AnswerClass.UNKNOWN, Reason.SERVER_ERROR)
    run.store.answer(action_id, 1, server_error, run.at(1))
    ticket = run.store.begin_check(action_id)
    assert ticket.kind is CheckKind.EXACT_ONLY
    first_sent = run.store.snapshot().attempts[action_id][0].sent_vt
    assert ticket.not_before_vt == first_sent + SETTINGS.dispatch_timeout_ms
    early = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=EXACT_FRESH, check_vt=run.at(), expires_at=FAR
    )
    with pytest.raises(StepRefused, match="backoff"):
        run.store.retry(action_id, early, run.at())
    assert run.retry(action_id)
    committed = Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001")
    run.store.answer(action_id, 2, committed, run.at(1))
    snapshot = run.store.snapshot()
    attempts = snapshot.attempts[action_id]
    assert {a.idempotency_key for a in attempts} == {action_id}
    assert {a.grant_id for a in attempts} == {"G-0001"}
    assert snapshot.actions[action_id].state is S.CONFIRMED


@pytest.mark.parametrize(
    ("approval", "basis", "grant_state"),
    [
        (ApprovalStatus.REVOKED, EXACT_FRESH, GrantState.REVOKED),
        (ApprovalStatus.EXPIRED, EXACT_FRESH, GrantState.EXPIRED),
        (ApprovalStatus.APPROVED, BasisCheck(False, None), GrantState.CONSUMED),
    ],
)
def test_c2_escalates_instead_of_retrying_when_the_retry_check_fails(
    approval: ApprovalStatus, basis: BasisCheck, grant_state: GrantState
) -> None:
    run = Run(Contract.C2)
    action_id = run.unknown()
    ticket = run.store.begin_check(action_id)
    vt = max(run.at(), ticket.not_before_vt)
    expires_at = FAR if approval is ApprovalStatus.APPROVED else None
    check = ticket.record(approval=approval, basis=basis, check_vt=vt, expires_at=expires_at)
    assert run.store.retry(action_id, check, vt) is None
    snapshot = run.store.snapshot()
    assert snapshot.events[-1].reason is Note.ESCALATED
    assert len(snapshot.attempts[action_id]) == 1
    assert snapshot.grants["G-0001"].state is grant_state
    reconcile = run.store.status(action_id, vt).reconcile
    assert reconcile is not None
    assert (reconcile.how, reconcile.escalation_id) == (ReconcileHow.ESCALATED_TO_HUMAN, "ESC-0001")


@pytest.mark.parametrize("contract", [Contract.C2, Contract.C3])
def test_in_progress_means_wait_and_retry(contract: Contract) -> None:
    run = Run(contract)
    action_id = run.unknown()
    assert run.retry(action_id)
    in_progress = Answer(AnswerClass.IN_PROGRESS, Reason.IN_PROGRESS)
    run.store.answer(action_id, 2, in_progress, run.at(1))
    result = run.store.status(action_id, run.at())
    assert (result.state, result.reason) == (S.OUTCOME_UNKNOWN, Reason.IN_PROGRESS)
    assert result.reconcile is not None
    assert result.reconcile.how is ReconcileHow.RETRYING_WITH_SAME_KEY
    assert run.store.snapshot().attempts[action_id][1].answer_class is AnswerClass.IN_PROGRESS
    assert run.retry(action_id)


@pytest.mark.parametrize("contract", [Contract.C2, Contract.C3])
def test_retries_stop_at_max_attempts(contract: Contract) -> None:
    run = Run(contract)
    action_id = run.unknown()
    for attempt_no in range(2, SETTINGS.max_attempts + 1):
        assert run.retry(action_id)
        run.store.answer(action_id, attempt_no, TIMEOUT, run.at(SETTINGS.dispatch_timeout_ms))
    assert run.store.snapshot().events[-1].reason is Note.ESCALATED
    with pytest.raises(StepRefused):
        run.store.begin_check(action_id)


def test_c3_retries_without_a_gateway_check() -> None:
    run = Run(Contract.C3)
    action_id = run.unknown()
    assert run.store.begin_check(action_id).kind is CheckKind.EXEMPT
    assert run.retry(action_id)
    assert run.store.snapshot().attempts[action_id][1].check.approval is None


# Startup recovery (0003) --------------------------------------------------------------


@pytest.mark.parametrize("contract", list(Contract))
def test_restart_moves_an_open_attempt_to_outcome_unknown(contract: Contract) -> None:
    run = Run(contract)
    proposed = run.propose("REQ-2")
    action_id = run.sent()
    run.store.restart(run.at(1))
    snapshot = run.store.snapshot()
    assert snapshot.actions[action_id].state is S.OUTCOME_UNKNOWN
    assert snapshot.attempts[action_id][0].answer_class is None
    assert snapshot.actions[proposed].state is S.PROPOSED
    escalated = snapshot.events[-1].reason is Note.ESCALATED
    assert escalated == (contract is Contract.C0)


# Identity (0001) ---------------------------------------------------------------------


def test_the_matching_rule() -> None:
    run = Run(Contract.C2)
    action_id = run.sent()
    attached = run.write()
    assert isinstance(attached, ActionResult) and attached.action_id == action_id
    busy = run.write(key="k2")
    assert isinstance(busy, Refusal) and busy.reason is RefusalReason.INTENT_BUSY
    assert (busy.action_id, busy.state) == (action_id, S.DISPATCHED)
    run.store.answer(
        action_id, 1, Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001"), run.at(1)
    )
    done = run.write(key="k2")
    assert isinstance(done, Refusal) and done.reason is RefusalReason.INTENT_DONE
    assert done.effect_id == "PO-0001"
    again = run.write()
    assert isinstance(again, ActionResult) and again.state is S.CONFIRMED
    assert len(run.store.snapshot().actions) == 1


def test_a_new_action_follows_a_failed_one_with_a_new_key_and_grant() -> None:
    run = Run(Contract.C2)
    first = run.sent()
    run.store.answer(first, 1, Answer(AnswerClass.REJECTED, Reason.DESTINATION_REJECTED), run.at(1))
    second = run.sent(key="k2")
    snapshot = run.store.snapshot()
    assert second != first
    assert snapshot.actions[second].idempotency_key == second
    assert snapshot.actions[second].grant_id == "G-0002"


# Trace replays for Gateway.tla (0021) ---------------------------------------------------


def _tla_edges(
    run: Run, action_id: str, contract: Contract
) -> tuple[tuple[str, ActionState | None, ActionState, Reason], ...]:
    edges = []
    for event in run.store.view(action_id).events:
        if event.kind is EventKind.TRANSITION:
            assert isinstance(event.reason, Reason)
            row = check_transition(event.from_state, event.to_state, event.reason, contract)
            edges.append((row.tid, event.from_state, event.to_state, event.reason))
    return tuple(edges)


@pytest.mark.parametrize("quantity", [100, 101], ids=["form-A", "form-B"])
def test_tla_replay_offer_and_dispatch_after_denial(quantity: int) -> None:
    contract = Contract.C2
    run = Run(contract)
    first = run.pending()
    before = run.store.snapshot()
    attached = run.write()
    assert isinstance(attached, ActionResult) and attached.action_id == first
    busy = run.write(key="k2", quantity=101)
    assert isinstance(busy, Refusal) and busy.reason is RefusalReason.INTENT_BUSY
    assert run.store.snapshot().events == before.events
    assert run.store.snapshot().grants == before.grants

    run.store.poll_approval(first, ApprovalStatus.DENIED, None, run.at(1), seen_seq=run.seq(first))
    assert _tla_edges(run, first, contract) == (
        ("T1", None, S.PROPOSED, Reason.ADMITTED),
        ("T2", S.PROPOSED, S.PENDING_APPROVAL, Reason.APPROVAL_REQUESTED),
        ("T5", S.PENDING_APPROVAL, S.REJECTED, Reason.APPROVAL_DENIED),
    )
    new = run.write(key="k1" if quantity == 100 else "k2", quantity=quantity)
    assert isinstance(new, ActionResult) and new.action_id != first
    second = new.action_id
    run.store.file_approval(second, FRESH, "AP-2", run.at(1), seen_seq=run.seq(second))
    run.store.poll_approval(
        second, ApprovalStatus.APPROVED, FRESH, run.at(1), seen_seq=run.seq(second)
    )
    ticket = run.store.begin_check(second)
    check = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=run.at(1), expires_at=FAR
    )
    attempt = run.store.dispatch(second, check, run.at(1))
    assert attempt is not None and attempt.idempotency_key == second
    assert _tla_edges(run, second, contract) == (
        ("T1", None, S.PROPOSED, Reason.ADMITTED),
        ("T2", S.PROPOSED, S.PENDING_APPROVAL, Reason.APPROVAL_REQUESTED),
        ("T4", S.PENDING_APPROVAL, S.AUTHORIZED, Reason.APPROVED),
        ("T6", S.AUTHORIZED, S.DISPATCHED, Reason.DISPATCHED),
    )
    snapshot = run.store.snapshot()
    assert (snapshot.actions[first].state, snapshot.actions[second].state) == (
        S.REJECTED,
        S.DISPATCHED,
    )
    assert [(g.action_id, g.state) for g in snapshot.grants.values()] == [
        (first, GrantState.DENIED),
        (second, GrantState.CONSUMED),
    ]


def test_tla_replay_status_confirms_one_effect_and_rejects_a_conflicting_report() -> None:
    contract = Contract.C1
    run = Run(contract)
    action_id = run.unknown()
    before = run.store.snapshot()
    with pytest.raises(StepRefused):
        run.store.begin_check(action_id)
    assert run.store.snapshot() == before

    run.store.probe(action_id, ProbeAnswer(True, ("PO-2", "PO-1"), run.at()), run.at())
    snapshot = run.store.snapshot()
    assert snapshot.events[:-1] == before.events
    assert _tla_edges(run, action_id, contract)[-2:] == (
        ("T10", S.DISPATCHED, S.OUTCOME_UNKNOWN, Reason.TIMEOUT),
        ("T11", S.OUTCOME_UNKNOWN, S.CONFIRMED, Reason.STATUS_FOUND),
    )
    action = snapshot.actions[action_id]
    assert (action.state, action.effect_id) == (S.CONFIRMED, "PO-2")
    assert snapshot.grants == before.grants
    other = Claim("REQ-2", ClaimStatus.NOT_DONE)
    wrong = Claim("REQ-1", ClaimStatus.DONE, ("PO-1",))
    refused = run.store.submit_report([wrong, other], run.at(1), worker="W1")
    assert isinstance(refused, Refusal) and refused.reason is RefusalReason.REPORT_INCONSISTENT
    assert not run.store.snapshot().run_closed
    right = Claim("REQ-1", ClaimStatus.DONE, ("PO-1", "PO-2"))
    assert isinstance(
        run.store.submit_report([right, other], run.at(1), worker="W1"), ReportAccepted
    )
    assert run.store.snapshot().run_closed


def test_attaching_takes_only_a_newer_basis_and_only_before_dispatch() -> None:
    run = Run(Contract.C2)
    action_id = run.authorized()
    run.write(quote_version=2)
    run.write(quote_version=1)
    assert run.store.snapshot().actions[action_id].basis["quote/S-1/W-100"] == 2
    ticket = run.store.begin_check(action_id)
    check = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=run.at(1), expires_at=FAR
    )
    run.store.dispatch(action_id, check, run.at(1))
    run.write(quote_version=3)
    assert run.store.snapshot().actions[action_id].basis["quote/S-1/W-100"] == 2


def test_unknown_intent_and_wrong_operation_are_refused() -> None:
    run = Run(Contract.C2)
    unknown = run.write(intent="REQ-9")
    wrong = run.store.propose(
        WriteCall("REQ-1", "reserve_inventory", "inventory/W-1/W-100", {}, "k", (), ()), run.at(1)
    )
    assert isinstance(unknown, Refusal) and unknown.reason is RefusalReason.UNKNOWN_INTENT
    assert isinstance(wrong, Refusal) and wrong.reason is RefusalReason.WRONG_OPERATION
    assert not run.store.snapshot().actions


def test_g1_actions_have_no_identity_and_are_exempt_from_i7() -> None:
    run = Run(Contract.C2, identity=False)
    first = run.propose()
    second = run.propose()
    snapshot = run.store.snapshot()
    assert first != second
    assert all(not a.identity and a.state is S.PROPOSED for a in snapshot.actions.values())


def test_a_decided_approval_does_not_time_out() -> None:
    run = Run(Contract.C2)
    action_id = run.pending()
    after_wait = run.at(SETTINGS.approval_wait_ms)
    with pytest.raises(StepRefused, match="act on the decision"):
        run.store.time_out_approval(
            action_id, ApprovalStatus.APPROVED, after_wait, seen_seq=run.seq(action_id)
        )
    assert run.state(action_id) is S.PENDING_APPROVAL


def test_a_basis_check_overtaken_by_a_newer_basis_writes_nothing() -> None:
    run = Run(Contract.C2)
    action_id = run.pending()
    seen = run.seq(action_id)
    run.write(quote_version=2)
    stale = BasisCheck(exact_ok=False, sufficient_ok=True, stale=(QUOTE_STALE,))
    before = run.store.snapshot()
    run.store.poll_approval(action_id, ApprovalStatus.APPROVED, stale, run.at(1), seen_seq=seen)
    assert run.store.snapshot().actions == before.actions
    assert run.state(action_id) is S.PENDING_APPROVAL


def test_c1_waits_for_a_status_answer_accurate_to_the_settle_time() -> None:
    run = Run(Contract.C1)
    action_id = run.unknown()
    sent_vt = run.store.snapshot().attempts[action_id][0].sent_vt
    lagging = ProbeAnswer(answered=True, as_of_vt=sent_vt + SETTLE - 1)
    run.store.probe(action_id, lagging, sent_vt + SETTLE + 500)
    assert run.state(action_id) is S.OUTCOME_UNKNOWN
    assert run.store.snapshot().events[-1].reason is Note.PROBE


def test_the_retry_backoff_doubles_from_the_latest_send() -> None:
    run = Run(Contract.C2)
    action_id = run.unknown()
    assert run.retry(action_id)
    second_sent = run.store.snapshot().attempts[action_id][1].sent_vt
    run.store.answer(action_id, 2, TIMEOUT, run.at(SETTINGS.dispatch_timeout_ms))
    ticket = run.store.begin_check(action_id)
    assert ticket.not_before_vt == second_sent + 2 * SETTINGS.dispatch_timeout_ms


def test_a_report_must_name_every_effect_a_status_answer_listed() -> None:
    """At C1 a duplicated request (fault F4) can leave two effects for one intent."""
    run = Run(Contract.C1)
    action_id = run.unknown("REQ-1")
    listing = ProbeAnswer(answered=True, effect_ids=("PO-0001", "PO-0002"), as_of_vt=run.at())
    run.store.probe(action_id, listing, run.at())
    assert run.store.snapshot().actions[action_id].effect_id == "PO-0001"
    other = Claim("REQ-2", ClaimStatus.NOT_DONE)
    one = run.store.submit_report(
        [Claim("REQ-1", ClaimStatus.DONE, ("PO-0001",)), other], run.at(), worker="W1"
    )
    assert isinstance(one, Refusal) and one.reason is RefusalReason.REPORT_INCONSISTENT
    assert one.conflicts[0].recorded[0].effect_ids == ("PO-0001", "PO-0002")
    both = Claim("REQ-1", ClaimStatus.DONE, ("PO-0002", "PO-0001"))
    assert isinstance(run.store.submit_report([both, other], run.at(), worker="W1"), ReportAccepted)


def test_reconcile_says_in_words_what_may_exist() -> None:
    run = Run(Contract.C2)
    action_id = run.unknown()
    result = run.store.status(action_id, run.at())
    assert result.reconcile is not None
    assert result.reconcile.what == (
        "The submit_purchase_order for REQ-1 at supplier/S-1 may or may not have taken effect."
    )


def test_only_answers_the_agent_receives_are_logged() -> None:
    run = Run(Contract.C2)
    action_id = run.propose()
    run.write(key="k2")
    run.store.status(action_id, run.at(1), tool="submit_purchase_order")
    logged = [(call.tool, type(call.result).__name__) for call in run.store.snapshot().tool_calls]
    assert logged == [
        ("submit_purchase_order", "Refusal"),
        ("submit_purchase_order", "ActionResult"),
    ]


# Closing and reporting (0003) ---------------------------------------------------------


def test_closing_rejects_what_was_never_sent_and_keeps_reconciling() -> None:
    run = Run(Contract.C2)
    waiting = run.pending("REQ-2")
    action_id = run.unknown()
    run.store.close_run(run.at(1))
    snapshot = run.store.snapshot()
    assert snapshot.actions[waiting].state is S.REJECTED
    assert snapshot.grants["G-0001"].state is GrantState.RELEASED
    refused = run.write()
    assert isinstance(refused, Refusal) and refused.reason is RefusalReason.RUN_CLOSED
    assert run.retry(action_id)
    assert run.state(action_id) is S.DISPATCHED


def test_the_report_check() -> None:
    run = Run(Contract.C2)
    confirmed = run.sent("REQ-1")
    run.store.answer(
        confirmed, 1, Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001"), run.at(1)
    )
    run.unknown("REQ-2")
    done = Claim("REQ-1", ClaimStatus.DONE, ("PO-0001",))
    unknown = Claim("REQ-2", ClaimStatus.UNKNOWN)
    accepted = run.store.submit_report([done, unknown], run.at(1), worker="W1")
    assert isinstance(accepted, ReportAccepted)
    for claims in (
        [Claim("REQ-1", ClaimStatus.DONE, ("PO-0002",)), unknown],
        [Claim("REQ-1", ClaimStatus.UNKNOWN), unknown],
        [done, Claim("REQ-2", ClaimStatus.NOT_DONE)],
        [done, Claim("REQ-2", ClaimStatus.DONE, ("PO-0001",))],
    ):
        refused = run.store.submit_report(claims, run.at(1), worker="W1")
        assert isinstance(refused, Refusal) and refused.reason is RefusalReason.REPORT_INCONSISTENT
        assert len(refused.conflicts) == 1
    with pytest.raises(ValueError, match="one claim per task intent"):
        run.store.submit_report([done], run.at(1), worker="W1")


def test_not_done_is_accepted_for_actions_never_sent() -> None:
    run = Run(Contract.C2)
    run.authorized("REQ-1")
    claims = [Claim("REQ-1", ClaimStatus.NOT_DONE), Claim("REQ-2", ClaimStatus.NOT_DONE)]
    assert isinstance(run.store.submit_report(claims, run.at(1), worker="W1"), ReportAccepted)


# Reports close what they claim (0010, item 6) -----------------------------------------


def test_an_accepted_not_done_closes_its_intent_at_once() -> None:
    """The claim stays true whichever report counts: nothing more can happen to it."""
    run = Run(Contract.C2, settings=TWO_WORKERS)
    waiting = run.pending("REQ-1")
    sent = run.unknown("REQ-2")
    claims = [Claim("REQ-1", ClaimStatus.NOT_DONE), Claim("REQ-2", ClaimStatus.UNKNOWN)]
    assert isinstance(run.store.submit_report(claims, run.at(1), worker="W1"), ReportAccepted)
    snapshot = run.store.snapshot()
    assert snapshot.closed_intents == {"REQ-1"}
    assert not snapshot.run_closed
    assert run.state(waiting) is S.REJECTED
    assert run.store.view(waiting).events[-1].reason is Reason.RUN_CLOSED
    assert snapshot.grants["G-0001"].state is GrantState.RELEASED
    refused = run.write("REQ-1", "k2")
    assert isinstance(refused, Refusal) and refused.reason is RefusalReason.RUN_CLOSED
    assert run.retry(sent), "the other intent's recovery goes on"


def test_the_last_expected_report_closes_the_run_with_it() -> None:
    run = Run(Contract.C2, settings=TWO_WORKERS)
    sent = run.unknown("REQ-1")
    run.unknown("REQ-2")
    claims = [Claim("REQ-1", ClaimStatus.UNKNOWN), Claim("REQ-2", ClaimStatus.UNKNOWN)]
    first = run.store.submit_report(claims, run.at(1), worker="W1")
    again = run.store.submit_report(claims, run.at(1), worker="W1")
    assert isinstance(first, ReportAccepted) and isinstance(again, ReportAccepted)
    snapshot = run.store.snapshot()
    assert not snapshot.run_closed, "one worker's two reports are not two workers' reports"
    assert dict(snapshot.reports) == {"W1": snapshot.tool_calls[-2].call_no}
    last = run.store.submit_report(claims, run.at(1), worker="W2")
    assert isinstance(last, ReportAccepted)
    snapshot = run.store.snapshot()
    assert snapshot.run_closed
    assert run.state(sent) is S.OUTCOME_UNKNOWN, "reconciliation continues after the close"
    assert snapshot.tool_calls[-1].as_of_seq == snapshot.events[-1].seq
    refused = run.write("REQ-2", "k3")
    assert isinstance(refused, Refusal) and refused.reason is RefusalReason.RUN_CLOSED


NOT_DONE = [Claim("REQ-1", ClaimStatus.NOT_DONE), Claim("REQ-2", ClaimStatus.NOT_DONE)]


def test_only_g2_checks_reports() -> None:
    with pytest.raises(ValueError, match="G1 has no report check"):
        Run(Contract.C2, identity=False).store.submit_report(NOT_DONE, 1, worker="W1")


def test_a_report_names_its_worker() -> None:
    with pytest.raises(ValueError, match="names the worker"):
        Run(Contract.C2).store.submit_report(NOT_DONE, 1, worker="")


def test_a_run_has_at_least_one_agent_worker() -> None:
    with pytest.raises(ValueError, match="at least one agent worker"):
        replace(SETTINGS, agent_workers=0)


def test_a_refused_report_closes_nothing() -> None:
    run = Run(Contract.C2)
    waiting = run.pending("REQ-1")
    claims = [Claim("REQ-1", ClaimStatus.NOT_DONE), Claim("REQ-2", ClaimStatus.DONE, ("PO-9",))]
    refused = run.store.submit_report(claims, run.at(1), worker="W1")
    assert isinstance(refused, Refusal) and refused.reason is RefusalReason.REPORT_INCONSISTENT
    snapshot = run.store.snapshot()
    assert not snapshot.run_closed and not snapshot.closed_intents and not snapshot.reports
    assert run.state(waiting) is S.PENDING_APPROVAL
    with pytest.raises(ValueError, match="NUL"):
        run.store.submit_report(claims, run.at(1), worker="W\x00")


def test_a_refused_step_changes_nothing() -> None:
    run = Run(Contract.C2)
    action_id = run.authorized()
    proposed = run.propose("REQ-2")
    before = run.store.snapshot()
    with pytest.raises(StepRefused):
        run.store.file_approval(action_id, FRESH, "AP-9", run.at(1), seen_seq=run.seq(action_id))
    with pytest.raises(ValueError, match="sufficient"):
        run.store.file_approval(
            proposed, EXACT_FRESH, "AP-9", run.at(1), seen_seq=run.seq(proposed)
        )
    with pytest.raises(StepRefused):
        run.store.probe(action_id, ProbeAnswer(answered=False), run.at(1))
    with pytest.raises(ValueError, match="backwards"):
        run.store.status(action_id, 0)
    assert run.store.snapshot() is before


@pytest.mark.parametrize(
    ("args", "basis", "message"),
    [
        ({"sku": "W\x00100"}, {"quote": 1}, "NUL"),
        ({"quantity": 1.5}, {"quote": 1}, "text or an integer"),
        ({"urgent": True}, {"quote": 1}, "text or an integer"),
        ({"sku": "W-100"}, {"quote": True}, "integer"),
    ],
)
def test_a_proposal_no_store_can_keep_is_refused_before_any_store(
    args: dict[str, object], basis: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Proposal("REQ-1", "submit_purchase_order", "t", args, "k", basis)  # type: ignore[arg-type]
