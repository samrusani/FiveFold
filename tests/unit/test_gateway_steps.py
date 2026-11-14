"""The gateway's steps, one rule at a time, through the in-memory store (0001 to 0005).

Each test drives one path from the decision records and checks the records it leaves.
The store checks I1 to I7 after every operation, so every test also checks them.
"""

from __future__ import annotations

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
)
from fivefold.gateway.state_machine import (
    ActionState,
    AnswerClass,
    CheckKind,
    GatewaySettings,
    GrantState,
    IllegalTransition,
    Note,
    Reason,
    ReconcileHow,
)
from fivefold.gateway.steps import StepRefused, UnspecifiedCase

S = ActionState
SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
)
SETTLE = SETTINGS.settle_ms
FRESH = BasisCheck(exact_ok=True, sufficient_ok=True)
EXACT_FRESH = BasisCheck(exact_ok=True, sufficient_ok=None)
QUOTE_STALE = StaleFact("quote/S-1/W-100", 1, 2, ("unit_price_minor",))
TIMEOUT = Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)


def proposal(intent: str = "REQ-1", key: str = "k1", quote_version: int = 1) -> Proposal:
    basis = {"quote/S-1/W-100": quote_version, "budget/CC-OPS": 1}
    return Proposal(intent, "submit_purchase_order", "supplier/S-1", {"quantity": 100}, key, basis)


class Run:
    """One run's store with a virtual clock and shortcuts to common states."""

    def __init__(self, contract: Contract, *, identity: bool = True) -> None:
        self.store = MemoryStore(
            task_id="TASK-1",
            intents={"REQ-1": "submit_purchase_order", "REQ-2": "submit_purchase_order"},
            contract=contract,
            settings=SETTINGS,
            identity=identity,
        )
        self.vt = 0
        self.approvals = 0

    def at(self, dt: int = 0) -> int:
        self.vt += dt
        return self.vt

    def state(self, action_id: str) -> ActionState:
        return self.store.snapshot().actions[action_id].state

    def propose(self, intent: str = "REQ-1", key: str = "k1") -> str:
        result = self.store.propose(proposal(intent, key), self.at(1))
        assert isinstance(result, ActionResult)
        return result.action_id

    def pending(self, intent: str = "REQ-1", key: str = "k1") -> str:
        action_id = self.propose(intent, key)
        self.approvals += 1
        self.store.file_approval(action_id, FRESH, f"AP-{self.approvals}", self.at(1))
        return action_id

    def authorized(self, intent: str = "REQ-1", key: str = "k1") -> str:
        action_id = self.pending(intent, key)
        self.store.poll_approval(action_id, ApprovalStatus.APPROVED, FRESH, self.at(1))
        return action_id

    def sent(self, intent: str = "REQ-1", key: str = "k1") -> str:
        action_id = self.authorized(intent, key)
        ticket = self.store.begin_check(action_id)
        check = ticket.record(approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=self.at(1))
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
            check = ticket.record(approval=ApprovalStatus.APPROVED, basis=basis, check_vt=self.vt)
        return self.store.retry(action_id, check, self.vt) is not None


# Approval -----------------------------------------------------------------------------


def test_a_stale_basis_is_rejected_before_any_approval_is_requested() -> None:
    run = Run(Contract.C2)
    action_id = run.propose()
    stale = BasisCheck(exact_ok=False, sufficient_ok=True, stale=(QUOTE_STALE,))
    run.store.file_approval(action_id, stale, None, run.at(1))
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
    run.store.poll_approval(action_id, approval, basis, run.at(1))
    snapshot = run.store.snapshot()
    assert snapshot.actions[action_id].state is S.REJECTED
    assert snapshot.events[-1].reason is reason
    assert snapshot.grants["G-0001"].state is grant_state


def test_a_pending_poll_with_a_fresh_basis_changes_nothing() -> None:
    run = Run(Contract.C2)
    action_id = run.pending()
    before = run.store.snapshot()
    run.store.poll_approval(action_id, ApprovalStatus.PENDING, FRESH, run.at(1))
    run.store.poll_approval(action_id, ApprovalStatus.PENDING, None, run.at(1))
    assert run.store.snapshot() == before


def test_the_approval_times_out_only_after_approval_wait_ms() -> None:
    run = Run(Contract.C2)
    action_id = run.pending()
    with pytest.raises(StepRefused):
        run.store.time_out_approval(action_id, run.at(SETTINGS.approval_wait_ms - 1))
    run.store.time_out_approval(action_id, run.at(1))
    snapshot = run.store.snapshot()
    assert snapshot.events[-1].reason is Reason.APPROVAL_TIMEOUT
    assert snapshot.grants["G-0001"].state is GrantState.TIMED_OUT


@pytest.mark.parametrize("approval", [ApprovalStatus.REVOKED, ApprovalStatus.EXPIRED])
def test_an_approval_already_dead_at_the_first_poll_is_unspecified(
    approval: ApprovalStatus,
) -> None:
    run = Run(Contract.C2)
    action_id = run.pending()
    before = run.store.snapshot()
    with pytest.raises(UnspecifiedCase, match="0010"):
        run.store.poll_approval(action_id, approval, FRESH, run.at(1))
    assert run.store.snapshot() == before


# Dispatch -----------------------------------------------------------------------------


def test_the_first_send_consumes_the_grant_and_records_its_check() -> None:
    run = Run(Contract.C2)
    action_id = run.sent()
    snapshot = run.store.snapshot()
    (attempt,) = snapshot.attempts[action_id]
    assert attempt.idempotency_key == action_id
    assert attempt.check.kind is CheckKind.FULL and attempt.check.passed
    grant = snapshot.grants["G-0001"]
    assert (grant.state, grant.consumed_vt) == (GrantState.CONSUMED, attempt.sent_vt)


def test_a_check_overtaken_by_a_newer_event_commits_nothing() -> None:
    run = Run(Contract.C2)
    action_id = run.authorized()
    ticket = run.store.begin_check(action_id)
    run.store.propose(proposal(quote_version=2), run.at(1))
    assert run.store.snapshot().events[-1].reason is Note.BASIS_UPDATED
    check = ticket.record(approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=run.at(1))
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
    check = ticket.record(approval=approval, basis=basis, check_vt=run.at(1))
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
    with pytest.raises(StepRefused, match="S has passed"):
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
    early = ticket.record(approval=ApprovalStatus.APPROVED, basis=EXACT_FRESH, check_vt=run.at())
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
    check = ticket.record(approval=approval, basis=basis, check_vt=vt)
    assert run.store.retry(action_id, check, vt) is None
    snapshot = run.store.snapshot()
    assert snapshot.events[-1].reason is Note.ESCALATED
    assert len(snapshot.attempts[action_id]) == 1
    assert snapshot.grants["G-0001"].state is grant_state


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
    attached = run.store.propose(proposal(), run.at(1))
    assert isinstance(attached, ActionResult) and attached.action_id == action_id
    busy = run.store.propose(proposal(key="k2"), run.at(1))
    assert isinstance(busy, Refusal) and busy.reason is RefusalReason.INTENT_BUSY
    assert (busy.action_id, busy.state) == (action_id, S.DISPATCHED)
    run.store.answer(
        action_id, 1, Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001"), run.at(1)
    )
    done = run.store.propose(proposal(key="k2"), run.at(1))
    assert isinstance(done, Refusal) and done.reason is RefusalReason.INTENT_DONE
    assert done.effect_id == "PO-0001"
    again = run.store.propose(proposal(), run.at(1))
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


def test_attaching_takes_only_a_newer_basis_and_only_before_dispatch() -> None:
    run = Run(Contract.C2)
    action_id = run.authorized()
    run.store.propose(proposal(quote_version=2), run.at(1))
    run.store.propose(proposal(quote_version=1), run.at(1))
    assert run.store.snapshot().actions[action_id].basis["quote/S-1/W-100"] == 2
    ticket = run.store.begin_check(action_id)
    check = ticket.record(approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=run.at(1))
    run.store.dispatch(action_id, check, run.at(1))
    run.store.propose(proposal(quote_version=3), run.at(1))
    assert run.store.snapshot().actions[action_id].basis["quote/S-1/W-100"] == 2


def test_unknown_intent_and_wrong_operation_are_refused() -> None:
    run = Run(Contract.C2)
    unknown = run.store.propose(proposal(intent="REQ-9"), run.at(1))
    wrong = run.store.propose(
        Proposal("REQ-1", "reserve_inventory", "inventory/W-1/W-100", {}, "k", {}), run.at(1)
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


# Closing and reporting (0003) ---------------------------------------------------------


def test_closing_rejects_what_was_never_sent_and_keeps_reconciling() -> None:
    run = Run(Contract.C2)
    waiting = run.pending("REQ-2")
    action_id = run.unknown()
    run.store.close_run(run.at(1))
    snapshot = run.store.snapshot()
    assert snapshot.actions[waiting].state is S.REJECTED
    assert snapshot.grants["G-0001"].state is GrantState.RELEASED
    refused = run.store.propose(proposal(), run.at(1))
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
    accepted = run.store.submit_report([done, unknown], run.at(1))
    assert isinstance(accepted, ReportAccepted)
    for claims in (
        [Claim("REQ-1", ClaimStatus.DONE, ("PO-0002",)), unknown],
        [Claim("REQ-1", ClaimStatus.UNKNOWN), unknown],
        [done, Claim("REQ-2", ClaimStatus.NOT_DONE)],
        [done, Claim("REQ-2", ClaimStatus.DONE, ("PO-0001",))],
    ):
        refused = run.store.submit_report(claims, run.at(1))
        assert isinstance(refused, Refusal) and refused.reason is RefusalReason.REPORT_INCONSISTENT
        assert len(refused.conflicts) == 1
    with pytest.raises(ValueError, match="one claim per task intent"):
        run.store.submit_report([done], run.at(1))


def test_not_done_is_accepted_for_actions_never_sent() -> None:
    run = Run(Contract.C2)
    run.authorized("REQ-1")
    claims = [Claim("REQ-1", ClaimStatus.NOT_DONE), Claim("REQ-2", ClaimStatus.NOT_DONE)]
    assert isinstance(run.store.submit_report(claims, run.at(1)), ReportAccepted)


def test_a_refused_step_changes_nothing() -> None:
    run = Run(Contract.C2)
    action_id = run.authorized()
    proposed = run.propose("REQ-2")
    before = run.store.snapshot()
    with pytest.raises(StepRefused):
        run.store.file_approval(action_id, FRESH, "AP-9", run.at(1))
    with pytest.raises(ValueError, match="sufficient"):
        run.store.file_approval(proposed, EXACT_FRESH, "AP-9", run.at(1))
    with pytest.raises(StepRefused):
        run.store.probe(action_id, ProbeAnswer(answered=False), run.at(1))
    with pytest.raises(ValueError, match="backwards"):
        run.store.status(action_id, 0)
    assert run.store.snapshot() is before
