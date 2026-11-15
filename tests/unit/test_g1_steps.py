"""G1's steps, one rule at a time, through both stores (0008, "G1: authorization only").

G1 is the store with action identity off. Its actions follow the transition table of
0003 with fewer steps: no basis check, a check of the approval only before the one send,
and no retries, probes, escalation, report check or run closing. Each test drives one
rule and checks the records it leaves. The store checks the invariants, scoped to the
rules G1 follows, after every operation. As in `test_gateway_steps.py`, every test runs
on the in-memory store and on PostgreSQL checked against it (marker `pg`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

import pytest

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.records import (
    ActionResult,
    Admission,
    Answer,
    ApprovalStatus,
    BasisCheck,
    CheckRecord,
    Claim,
    ClaimStatus,
    ProbeAnswer,
    Resolution,
    WriteCall,
)
from fivefold.gateway.state_machine import (
    ActionState,
    AnswerClass,
    CheckKind,
    GatewaySettings,
    GrantState,
    Reason,
)
from fivefold.gateway.steps import StepRefused
from fivefold.gateway.twin import TwinStore

S = ActionState
A = ApprovalStatus
SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
)
SETTLE = SETTINGS.settle_ms
QUOTE, BUDGET = "quote/S-1/W-100", "budget/CC-OPS"
FRESH = BasisCheck(exact_ok=True, sufficient_ok=True)
TIMEOUT = Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)
FAR = 10**9
"""An approval expiry far beyond any test's virtual time."""
LEVELS = [Contract.C0, Contract.C1, Contract.C2, Contract.C3]


@pytest.fixture(autouse=True)
def _on_each_store(make_store: Callable[..., MemoryStore | TwinStore]) -> None:
    Run.factory = make_store


class Run:
    """One run's G1 store (identity off, unless asked) with shortcuts to its states."""

    factory: ClassVar[Callable[..., MemoryStore | TwinStore]]

    def __init__(self, contract: Contract = Contract.C2, *, identity: bool = False) -> None:
        self.store = Run.factory(
            task_id="TASK-1",
            intents={"REQ-1": "submit_purchase_order"},
            contract=contract,
            settings=SETTINGS,
            identity=identity,
        )
        self.vt = 0

    def at(self, dt: int = 0) -> int:
        self.vt += dt
        return self.vt

    def seq(self, action_id: str) -> int:
        return self.store.view(action_id).last_seq

    def state(self, action_id: str) -> ActionState:
        return self.store.snapshot().actions[action_id].state

    def propose(self) -> str:
        """A write call that cites reads of the order's two dependencies."""
        vt = self.at(1)
        quote = self.store.record_read(QUOTE, 1, {}, vt)
        budget = self.store.record_read(BUDGET, 1, {}, vt)
        call = WriteCall(
            "REQ-1",
            "submit_purchase_order",
            "supplier/S-1",
            {"quantity": 100},
            "k1",
            (quote.read_id, budget.read_id),
            (QUOTE, BUDGET),
        )
        result = self.store.propose(call, vt)
        assert isinstance(result, ActionResult)
        return result.action_id

    def pending(self) -> str:
        action_id = self.propose()
        approval_id = f"AP-{action_id}"
        self.store.file_approval(
            action_id, None, approval_id, self.at(1), seen_seq=self.seq(action_id)
        )
        return action_id

    def authorized(self) -> str:
        action_id = self.pending()
        self.store.poll_approval(
            action_id, A.APPROVED, None, self.at(1), seen_seq=self.seq(action_id)
        )
        return action_id

    def check(
        self, action_id: str, approval: ApprovalStatus = A.APPROVED, expires_at: int | None = FAR
    ) -> CheckRecord:
        ticket = self.store.begin_check(action_id)
        return ticket.record(
            approval=approval, basis=None, check_vt=self.at(1), expires_at=expires_at
        )

    def sent(self) -> str:
        action_id = self.authorized()
        assert self.store.dispatch(action_id, self.check(action_id), self.at(1)) is not None
        return action_id

    def unknown(self) -> str:
        action_id = self.sent()
        self.store.answer(action_id, 1, TIMEOUT, self.at(SETTINGS.dispatch_timeout_ms))
        return action_id


# Rule 1: one approval request, a check of the approval, one send ------------------------


def test_a_g1_write_is_approved_checked_and_sent_once() -> None:
    run = Run()
    action_id = run.sent()
    committed = Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001")
    run.store.answer(action_id, 1, committed, run.at(10))
    snapshot = run.store.snapshot()
    assert [str(e.reason) for e in snapshot.events] == [
        "admitted",
        "approval_requested",
        "approved",
        "dispatched",
        "committed",
    ]
    (attempt,) = snapshot.attempts[action_id]
    check = attempt.check
    assert check.kind is CheckKind.AUTHORITY_ONLY
    assert (check.approval, check.approval_expires_at, check.basis) == (A.APPROVED, FAR, None)
    grant = snapshot.grants["G-0001"]
    assert (grant.approval_id, grant.state, grant.consumed_vt) == (
        f"AP-{action_id}",
        GrantState.CONSUMED,
        attempt.sent_vt,
    )
    assert snapshot.actions[action_id].effect_id == "PO-0001"


@pytest.mark.parametrize(
    ("approval", "expires_in", "reason", "grant_state"),
    [
        (A.REVOKED, None, Reason.APPROVAL_REVOKED, GrantState.REVOKED),
        (A.EXPIRED, None, Reason.APPROVAL_EXPIRED, GrantState.EXPIRED),
        (A.APPROVED, SETTLE, Reason.APPROVAL_EXPIRED, GrantState.EXPIRED),
    ],
)
def test_g1_checks_the_approval_before_its_send(
    approval: ApprovalStatus, expires_in: int | None, reason: Reason, grant_state: GrantState
) -> None:
    """Rule 17 of 0002, with 0010's settle margin: an approval that expires by the send's
    time plus S counts as expired."""
    run = Run()
    action_id = run.authorized()
    send_vt = run.vt + 1
    expires_at = None if expires_in is None else send_vt + expires_in
    check = run.check(action_id, approval, expires_at)
    assert run.store.dispatch(action_id, check, send_vt) is None
    snapshot = run.store.snapshot()
    assert (run.state(action_id), snapshot.events[-1].reason) == (S.REJECTED, reason)
    assert snapshot.grants["G-0001"].state is grant_state
    assert action_id not in snapshot.attempts, "nothing was sent"


def test_g1_sends_only_after_its_own_kind_of_check() -> None:
    """G1's first send needs the `authority_only` check; G2's needs the full one."""
    run = Run()
    action_id = run.authorized()
    ticket = run.store.begin_check(action_id)
    assert (ticket.kind, ticket.attempt_no) == (CheckKind.AUTHORITY_ONLY, 1)
    full = CheckRecord(CheckKind.FULL, A.APPROVED, FRESH, ticket.seen_seq, run.at(1), FAR)
    with pytest.raises(StepRefused, match="needs the authority_only check"):
        run.store.dispatch(action_id, full, run.at(1))
    assert run.state(action_id) is S.AUTHORIZED


def test_g2_does_not_send_after_an_authority_only_check() -> None:
    run = Run(identity=True)
    action_id = run.propose()
    run.store.file_approval(action_id, FRESH, "AP-1", run.at(1), seen_seq=run.seq(action_id))
    run.store.poll_approval(action_id, A.APPROVED, FRESH, run.at(1), seen_seq=run.seq(action_id))
    ticket = run.store.begin_check(action_id)
    assert ticket.kind is CheckKind.FULL
    approval_only = CheckRecord(
        CheckKind.AUTHORITY_ONLY, A.APPROVED, None, ticket.seen_seq, run.at(1), FAR
    )
    with pytest.raises(StepRefused, match="needs the full check"):
        run.store.dispatch(action_id, approval_only, run.at(1))


@pytest.mark.parametrize(
    ("approval", "reason", "grant_state"),
    [
        (A.DENIED, Reason.APPROVAL_DENIED, GrantState.DENIED),
        (A.REVOKED, Reason.APPROVAL_REVOKED, GrantState.REVOKED),
        (A.EXPIRED, Reason.APPROVAL_EXPIRED, GrantState.EXPIRED),
    ],
)
def test_dead_authority_rejects_a_waiting_g1_action(
    approval: ApprovalStatus, reason: Reason, grant_state: GrantState
) -> None:
    run = Run()
    action_id = run.pending()
    run.store.poll_approval(action_id, approval, None, run.at(1), seen_seq=run.seq(action_id))
    snapshot = run.store.snapshot()
    assert (run.state(action_id), snapshot.events[-1].reason) == (S.REJECTED, reason)
    assert snapshot.grants["G-0001"].state is grant_state


def test_a_g1_approval_with_no_decision_times_out() -> None:
    run = Run()
    action_id = run.pending()
    seen = run.seq(action_id)
    run.store.poll_approval(action_id, A.PENDING, None, run.at(1), seen_seq=seen)
    assert run.state(action_id) is S.PENDING_APPROVAL, "a pending poll changes nothing"
    run.store.time_out_approval(
        action_id, A.PENDING, run.at(SETTINGS.approval_wait_ms), seen_seq=seen
    )
    snapshot = run.store.snapshot()
    assert snapshot.events[-1].reason is Reason.APPROVAL_TIMEOUT
    assert snapshot.grants["G-0001"].state is GrantState.TIMED_OUT


# Rule 2: no action identity ----------------------------------------------------------------


def test_every_g1_write_call_is_a_new_action_with_its_own_approval() -> None:
    """The same call twice: two actions, two approvals, two sends, two effects (0008)."""
    run = Run()
    first, second = run.sent(), run.sent()
    run.store.answer(
        first, 1, Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001"), run.at(1)
    )
    run.store.answer(
        second, 1, Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0002"), run.at(1)
    )
    snapshot = run.store.snapshot()
    assert [(a.action_id, a.state, a.effect_id) for a in snapshot.actions.values()] == [
        ("A-0001", S.CONFIRMED, "PO-0001"),
        ("A-0002", S.CONFIRMED, "PO-0002"),
    ]
    assert [p.admission for p in snapshot.proposals] == [Admission.ADMITTED, Admission.ADMITTED]
    assert sorted(g.approval_id for g in snapshot.grants.values()) == ["AP-A-0001", "AP-A-0002"]


# Rule 3: no freshness checks ---------------------------------------------------------------


def test_g1_forwards_the_cited_reads_as_the_basis_and_never_checks_them() -> None:
    run = Run()
    action_id = run.propose()
    stale = BasisCheck(exact_ok=False, sufficient_ok=True)
    for basis in (FRESH, stale):
        with pytest.raises(ValueError, match="checks no basis"):
            run.store.file_approval(
                action_id, basis, "AP-1", run.at(1), seen_seq=run.seq(action_id)
            )
    run.store.file_approval(action_id, None, "AP-1", run.at(1), seen_seq=run.seq(action_id))
    with pytest.raises(ValueError, match="checks no basis"):
        run.store.poll_approval(
            action_id, A.APPROVED, FRESH, run.at(1), seen_seq=run.seq(action_id)
        )
    run.store.poll_approval(action_id, A.APPROVED, None, run.at(1), seen_seq=run.seq(action_id))
    action = run.store.snapshot().actions[action_id]
    assert action.state is S.AUTHORIZED
    assert dict(action.basis) == {QUOTE: 1, BUDGET: 1}, "the cited versions, sent with the order"


def test_g2_still_needs_a_basis_check_to_file_and_to_authorize() -> None:
    run = Run(identity=True)
    action_id = run.propose()
    with pytest.raises(ValueError, match="only after a basis check"):
        run.store.file_approval(action_id, None, "AP-1", run.at(1), seen_seq=run.seq(action_id))
    run.store.file_approval(action_id, FRESH, "AP-1", run.at(1), seen_seq=run.seq(action_id))
    with pytest.raises(ValueError, match="only with a basis check"):
        run.store.poll_approval(action_id, A.APPROVED, None, run.at(1), seen_seq=run.seq(action_id))
    assert run.state(action_id) is S.PENDING_APPROVAL


# Rules 4 and 5: no keys, probes or retries; an unknown outcome stays unknown --------------


@pytest.mark.parametrize("contract", LEVELS)
def test_a_g1_send_with_no_answer_stays_outcome_unknown(contract: Contract) -> None:
    """No retry, no status probe and no escalation, at any level, so no human resolves it
    either: G1 has no recovery (0008). The agent is told the state, with no way of
    reconciling it, because G1 has none."""
    run = Run(contract)
    action_id = run.unknown()
    view = run.store.view(action_id)
    assert (view.action.state, view.escalation) == (S.OUTCOME_UNKNOWN, None)
    assert [str(e.reason) for e in view.events][-1] == "timeout"
    result = run.store.status(action_id, run.at(1))
    assert (result.state, result.reason, result.reconcile) == (
        S.OUTCOME_UNKNOWN,
        Reason.TIMEOUT,
        None,
    )
    with pytest.raises(StepRefused, match="G1 never retries"):
        run.store.begin_check(action_id)
    retry = CheckRecord(CheckKind.EXEMPT, None, None, run.seq(action_id), run.at(SETTLE))
    with pytest.raises(StepRefused, match="G1 never retries"):
        run.store.retry(action_id, retry, run.vt)
    with pytest.raises(StepRefused, match="G1 never probes"):
        run.store.probe(action_id, ProbeAnswer(answered=True, as_of_vt=run.vt), run.vt)
    with pytest.raises(StepRefused, match="no open escalation"):
        run.store.attest(action_id, Resolution.NOT_COMMITTED, None, "resolver", run.at(SETTLE))
    assert run.state(action_id) is S.OUTCOME_UNKNOWN
    assert len(run.store.snapshot().attempts[action_id]) == 1


@pytest.mark.parametrize("contract", LEVELS)
def test_a_restart_records_an_open_g1_attempt_as_unknown_and_nothing_more(
    contract: Contract,
) -> None:
    run = Run(contract)
    action_id = run.sent()
    run.store.restart(run.at(1_000))
    view = run.store.view(action_id)
    assert (view.action.state, view.events[-1].reason) == (
        S.OUTCOME_UNKNOWN,
        Reason.RECOVERED_OPEN_ATTEMPT,
    )
    assert view.escalation is None and view.attempts[-1].answer_class is None


@pytest.mark.parametrize("contract", LEVELS)
@pytest.mark.parametrize("identity", [False, True])
def test_only_g2_requests_carry_a_key_and_only_where_the_contract_takes_one(
    contract: Contract, identity: bool
) -> None:
    run = Run(contract, identity=identity)
    action_id = run.propose()
    action = run.store.snapshot().actions[action_id]
    assert action.idempotency_key == action.action_id, "the action's own key, as in G2"
    keyed = identity and contract in (Contract.C2, Contract.C3)
    assert action.request_key == (action.action_id if keyed else None)


# Rule 6 and run closing: G1 checks no report and never closes the run ---------------------


def test_g1_has_no_report_check_and_no_run_closing() -> None:
    run = Run()
    action_id = run.pending()
    with pytest.raises(ValueError, match="G1 has no report check"):
        run.store.submit_report([Claim("REQ-1", ClaimStatus.NOT_DONE)], run.at(1), worker="W1")
    with pytest.raises(ValueError, match="no run closing"):
        run.store.close_run(run.at(1))
    snapshot = run.store.snapshot()
    assert not snapshot.run_closed and not snapshot.reports and not snapshot.tool_calls
    assert run.state(action_id) is S.PENDING_APPROVAL
