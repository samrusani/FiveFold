"""Each invariant check fires on records that break its rule (0003).

The records come from a real run through the in-memory store, so they start valid.
Each test breaks one rule with `dataclasses.replace` and checks which invariant reports
it. A check that never fires would pass every other test, so these tests matter.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.gateway.invariants import check_invariants
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.records import (
    ActionEvent,
    ActionResult,
    Admission,
    Answer,
    ApprovalStatus,
    BasisCheck,
    Claim,
    ClaimConflict,
    ClaimStatus,
    Escalation,
    RecordedAction,
    Refusal,
    RefusalReason,
    ReportAccepted,
    Snapshot,
    ToolCall,
    WriteCall,
    frozen,
)
from fivefold.gateway.state_machine import (
    ActionState,
    AnswerClass,
    CheckKind,
    EventKind,
    GatewaySettings,
    GrantState,
    Note,
    Reason,
    ReconcileHow,
)

S = ActionState
SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
)
FRESH = BasisCheck(exact_ok=True, sufficient_ok=True)
FAR = 10**9
INTENTS = {"REQ-1": "submit_purchase_order", "REQ-2": "submit_purchase_order", "REQ-3": "x"}


def _write(
    store: MemoryStore, intent: str, key: str, version: int, vt: int
) -> ActionResult | Refusal:
    """A write call for `intent` that cites a read of fact `f` at `version`."""
    read = store.record_read("f", version, {}, vt)
    return store.propose(
        WriteCall(intent, INTENTS[intent], "t", {}, key, (read.read_id,), ("f",)), vt
    )


def _send(store: MemoryStore, intent: str, vt: int) -> tuple[str, int]:
    """Propose, approve and send one action for `intent`; return its id and the time."""
    result = _write(store, intent, f"key-{intent}", 1, vt)
    assert isinstance(result, ActionResult)
    action_id = result.action_id
    store.file_approval(
        action_id, FRESH, f"AP-{action_id}", vt + 1, seen_seq=store.view(action_id).last_seq
    )
    store.poll_approval(
        action_id, ApprovalStatus.APPROVED, FRESH, vt + 2, seen_seq=store.view(action_id).last_seq
    )
    ticket = store.begin_check(action_id)
    check = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=vt + 3, expires_at=FAR
    )
    assert store.dispatch(action_id, check, vt + 3) is not None
    return action_id, vt + 3


@pytest.fixture
def snapshot() -> Snapshot:
    """A valid C2 run with every kind of record.

    REQ-1 is confirmed after a retry. REQ-2 is unknown and escalated, because its retry
    check found the approval revoked. REQ-3 was authorized and took a newer basis (a
    note). The run's only worker then reports REQ-3 `not_done`, which closes the intent
    and, as the last report the run expects, the run: REQ-3's action is rejected with
    `run_closed` (0010).
    """
    store = MemoryStore(task_id="TASK-1", intents=INTENTS, contract=Contract.C2, settings=SETTINGS)
    first, vt = _send(store, "REQ-1", 0)
    store.answer(first, 1, Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT), vt + 200)
    ticket = store.begin_check(first)
    retry = ticket.record(
        approval=ApprovalStatus.APPROVED,
        basis=BasisCheck(exact_ok=True, sufficient_ok=None),
        check_vt=ticket.not_before_vt,
        expires_at=FAR,
    )
    assert store.retry(first, retry, ticket.not_before_vt) is not None
    committed = Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001")
    store.answer(first, 2, committed, ticket.not_before_vt + 10)
    second, vt = _send(store, "REQ-2", ticket.not_before_vt + 20)
    store.answer(second, 1, Answer(AnswerClass.UNKNOWN, Reason.SERVER_ERROR), vt + 5)
    ticket = store.begin_check(second)
    revoked = ticket.record(
        approval=ApprovalStatus.REVOKED,
        basis=BasisCheck(exact_ok=True, sufficient_ok=None),
        check_vt=ticket.not_before_vt,
    )
    assert store.retry(second, revoked, ticket.not_before_vt) is None
    vt = ticket.not_before_vt
    third = _write(store, "REQ-3", "key-REQ-3", 1, vt + 6)
    assert isinstance(third, ActionResult)
    seen = store.view(third.action_id).last_seq
    store.file_approval(third.action_id, FRESH, "AP-REQ-3", vt + 7, seen_seq=seen)
    seen = store.view(third.action_id).last_seq
    store.poll_approval(third.action_id, ApprovalStatus.APPROVED, FRESH, vt + 8, seen_seq=seen)
    _write(store, "REQ-3", "key-REQ-3", 2, vt + 8)
    store.status(first, vt + 9)
    store.status(second, vt + 9)
    claims = [
        Claim("REQ-1", ClaimStatus.DONE, ("PO-0001",)),
        Claim("REQ-2", ClaimStatus.UNKNOWN),
        Claim("REQ-3", ClaimStatus.NOT_DONE),
    ]
    assert isinstance(store.submit_report(claims, vt + 10, worker="W1"), ReportAccepted)
    return store.snapshot()


def labels(snapshot: Snapshot) -> set[str]:
    return {violation.invariant for violation in check_invariants(snapshot, SETTINGS)}


def with_action(snapshot: Snapshot, action_id: str, **changes: object) -> Snapshot:
    actions = dict(snapshot.actions)
    actions[action_id] = replace(actions[action_id], **changes)  # type: ignore[arg-type]
    return replace(snapshot, actions=frozen(actions))


def test_the_fixture_is_valid(snapshot: Snapshot) -> None:
    assert check_invariants(snapshot, SETTINGS) == []
    states = [a.state for a in snapshot.actions.values()]
    assert states == [S.CONFIRMED, S.OUTCOME_UNKNOWN, S.REJECTED]
    assert snapshot.run_closed and snapshot.closed_intents == {"REQ-3"}
    assert dict(snapshot.reports) == {"W1": snapshot.tool_calls[-1].call_no}
    reasons = {str(e.reason) for e in snapshot.events if e.kind is EventKind.NOTE}
    assert reasons == {"escalated", "basis_updated"}


def test_i1_an_effect_id_on_an_unconfirmed_action(snapshot: Snapshot) -> None:
    assert labels(with_action(snapshot, "A-0002", effect_id="PO-0009")) == {"I1"}


def test_i1_an_effect_id_that_differs_from_its_event(snapshot: Snapshot) -> None:
    assert "I1" in labels(with_action(snapshot, "A-0001", effect_id="PO-0009"))


def test_i2_two_grants_for_one_approval(snapshot: Snapshot) -> None:
    grants = dict(snapshot.grants)
    grants["G-0002"] = replace(grants["G-0002"], approval_id=grants["G-0001"].approval_id)
    assert labels(replace(snapshot, grants=frozen(grants))) == {"I2"}


def test_i2_an_attempt_under_another_grant(snapshot: Snapshot) -> None:
    attempts = dict(snapshot.attempts)
    first, second = attempts["A-0001"]
    attempts["A-0001"] = (first, replace(second, grant_id="G-0002"))
    assert "I2" in labels(replace(snapshot, attempts=frozen(attempts)))


def test_i2_a_grant_state_that_does_not_fit_the_action(snapshot: Snapshot) -> None:
    grants = dict(snapshot.grants)
    grants["G-0003"] = replace(grants["G-0003"], state=GrantState.CONSUMED, consumed_vt=1)
    assert labels(replace(snapshot, grants=frozen(grants))) == {"I2"}


def test_i3_a_send_after_a_failing_check(snapshot: Snapshot) -> None:
    attempts = dict(snapshot.attempts)
    (attempt,) = attempts["A-0002"]
    failing = replace(attempt.check, approval=ApprovalStatus.REVOKED, approval_expires_at=None)
    attempts["A-0002"] = (replace(attempt, check=failing),)
    assert labels(replace(snapshot, attempts=frozen(attempts))) == {"I3"}


def test_i3_a_send_under_an_approval_that_expires_too_soon(snapshot: Snapshot) -> None:
    attempts = dict(snapshot.attempts)
    (attempt,) = attempts["A-0002"]
    expiring = replace(attempt.check, approval_expires_at=attempt.sent_vt + SETTINGS.settle_ms)
    attempts["A-0002"] = (replace(attempt, check=expiring),)
    assert labels(replace(snapshot, attempts=frozen(attempts))) == {"I3"}


def test_i3_counts_the_settle_time_from_the_send_not_from_the_check(snapshot: Snapshot) -> None:
    """The approval outlives the check by S but not the send (0010, change 4)."""
    attempts = dict(snapshot.attempts)
    (attempt,) = attempts["A-0002"]
    check_vt = attempt.sent_vt - 100
    expiring = replace(
        attempt.check,
        check_vt=check_vt,
        approval_expires_at=check_vt + SETTINGS.settle_ms + 50,
    )
    attempts["A-0002"] = (replace(attempt, check=expiring),)
    assert labels(replace(snapshot, attempts=frozen(attempts))) == {"I3"}


def test_i3_an_event_between_the_check_and_the_send(snapshot: Snapshot) -> None:
    attempts = dict(snapshot.attempts)
    (attempt,) = attempts["A-0002"]
    older = replace(attempt.check, seen_seq=attempt.check.seen_seq - 1)
    attempts["A-0002"] = (replace(attempt, check=older),)
    assert labels(replace(snapshot, attempts=frozen(attempts))) == {"I3"}


def test_i3_a_c2_retry_with_the_wrong_kind_of_check(snapshot: Snapshot) -> None:
    attempts = dict(snapshot.attempts)
    first, second = attempts["A-0001"]
    full = replace(second.check, kind=CheckKind.FULL, basis=FRESH)
    attempts["A-0001"] = (first, replace(second, check=full))
    assert labels(replace(snapshot, attempts=frozen(attempts))) == {"I3"}


def test_i4_a_retry_with_another_key(snapshot: Snapshot) -> None:
    attempts = dict(snapshot.attempts)
    first, second = attempts["A-0001"]
    attempts["A-0001"] = (first, replace(second, idempotency_key="other"))
    assert labels(replace(snapshot, attempts=frozen(attempts))) == {"I4"}


def test_i4_two_attempts_where_the_contract_allows_one(snapshot: Snapshot) -> None:
    found = labels(with_action(snapshot, "A-0001", contract=Contract.C1))
    assert "I4" in found


def test_i5_a_response_that_reports_another_state(snapshot: Snapshot) -> None:
    calls = list(snapshot.tool_calls)
    index = next(
        i for i, c in enumerate(calls) if c.action_id == "A-0002" and c.tool == "get_action_status"
    )
    call = calls[index]
    assert isinstance(call.result, ActionResult)
    lie = replace(call.result, state=S.DISPATCHED)
    calls[index] = replace(call, result=lie)
    found = check_invariants(replace(snapshot, tool_calls=tuple(calls)), SETTINGS)
    assert [(v.invariant, v.message) for v in found] == [
        ("I5", "reported dispatched, records say outcome_unknown")
    ]


def test_i5_outcome_unknown_reported_without_how_it_is_reconciled(snapshot: Snapshot) -> None:
    calls = list(snapshot.tool_calls)
    index = next(
        i for i, c in enumerate(calls) if c.action_id == "A-0002" and c.tool == "get_action_status"
    )
    call = calls[index]
    assert isinstance(call.result, ActionResult)
    calls[index] = replace(call, result=replace(call.result, reconcile=None))
    assert labels(replace(snapshot, tool_calls=tuple(calls))) == {"I5"}


def test_i5_an_accepted_report_that_contradicts_the_records(snapshot: Snapshot) -> None:
    calls = list(snapshot.tool_calls)
    last = calls[-1]
    assert isinstance(last.result, ReportAccepted)
    claims = (
        Claim("REQ-1", ClaimStatus.UNKNOWN),
        Claim("REQ-2", ClaimStatus.UNKNOWN),
        Claim("REQ-3", ClaimStatus.NOT_DONE),
    )
    calls[-1] = replace(last, result=ReportAccepted(claims))
    assert labels(replace(snapshot, tool_calls=tuple(calls))) == {"I5"}


def test_i5_a_response_from_before_the_action_existed(snapshot: Snapshot) -> None:
    result = ActionResult("A-0003", "REQ-3", S.PROPOSED, None, Reason.ADMITTED, (), None)
    early = ToolCall(len(snapshot.tool_calls) + 1, "get_action_status", "A-0003", result, 1, 0)
    assert labels(replace(snapshot, tool_calls=(*snapshot.tool_calls, early))) == {"I5"}


def test_i6_a_state_the_log_does_not_reproduce(snapshot: Snapshot) -> None:
    assert "I6" in labels(with_action(snapshot, "A-0003", state=S.PENDING_APPROVAL))


def test_i6_a_missing_event_breaks_the_chain(snapshot: Snapshot) -> None:
    events = snapshot.events
    assert "I6" in labels(replace(snapshot, events=events[:3] + events[4:]))


def test_i6_an_illegal_transition_in_the_log(snapshot: Snapshot) -> None:
    events = list(snapshot.events)
    index = next(i for i, e in enumerate(events) if e.reason is Reason.APPROVAL_REQUESTED)
    events[index] = replace(events[index], to_state=S.AUTHORIZED)
    assert "I6" in labels(replace(snapshot, events=tuple(events)))


def test_i6_time_that_runs_backwards(snapshot: Snapshot) -> None:
    events = list(snapshot.events)
    events[-1] = replace(events[-1], vt=0)
    assert "I6" in labels(replace(snapshot, events=tuple(events)))


def _send_g1(store: MemoryStore, intent: str, vt: int) -> str:
    """Propose, approve and send one G1 action for `intent`, with G1's steps (0008): no
    basis check, and a check of the approval only before the send."""
    result = _write(store, intent, f"key-{intent}", 1, vt)
    assert isinstance(result, ActionResult)
    action_id = result.action_id
    store.file_approval(
        action_id, None, f"AP-{action_id}", vt + 1, seen_seq=store.view(action_id).last_seq
    )
    store.poll_approval(
        action_id, ApprovalStatus.APPROVED, None, vt + 2, seen_seq=store.view(action_id).last_seq
    )
    ticket = store.begin_check(action_id)
    check = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=None, check_vt=vt + 3, expires_at=FAR
    )
    assert store.dispatch(action_id, check, vt + 3) is not None
    return action_id


def g1_store(contract: Contract = Contract.C2) -> MemoryStore:
    return MemoryStore(
        task_id="TASK-1", intents=INTENTS, contract=contract, settings=SETTINGS, identity=False
    )


def test_i7_two_live_actions_for_one_intent() -> None:
    store = g1_store()
    _send_g1(store, "REQ-1", 0)
    _send_g1(store, "REQ-1", 10)
    g1 = store.snapshot()
    assert check_invariants(g1, SETTINGS) == []
    identity_on = {k: replace(a, identity=True) for k, a in g1.actions.items()}
    # With identity on, the same records also break G2's I3: G1 checked the approval only.
    assert labels(replace(g1, actions=frozen(identity_on))) == {"I3", "I7"}


def with_attempt(snapshot: Snapshot, action_id: str, index: int, **changes: object) -> Snapshot:
    attempts = dict(snapshot.attempts)
    rows = list(attempts[action_id])
    rows[index] = replace(rows[index], **changes)  # type: ignore[arg-type]
    attempts[action_id] = tuple(rows)
    return replace(snapshot, attempts=frozen(attempts))


def with_call(snapshot: Snapshot, result: ActionResult | Refusal, as_of_seq: int) -> Snapshot:
    call = ToolCall(len(snapshot.tool_calls) + 1, "t", None, result, as_of_seq, 10**6)
    return replace(snapshot, tool_calls=(*snapshot.tool_calls, call))


def messages(snapshot: Snapshot, settings: GatewaySettings = SETTINGS) -> list[str]:
    return [f"{v.invariant}: {v.message}" for v in check_invariants(snapshot, settings)]


def test_i1_a_committed_attempt_that_names_another_effect(snapshot: Snapshot) -> None:
    found = messages(with_attempt(snapshot, "A-0001", 1, answer_ref="PO-0009"))
    assert found == ["I1: attempt 2 committed another effect"]


def test_i2_a_grant_consumed_at_another_time(snapshot: Snapshot) -> None:
    grants = dict(snapshot.grants)
    grants["G-0001"] = replace(grants["G-0001"], consumed_vt=1)
    found = messages(replace(snapshot, grants=frozen(grants)))
    assert found == ["I2: the grant was not consumed at the first send"]


def test_i3_a_check_made_after_the_send(snapshot: Snapshot) -> None:
    attempt = snapshot.attempts["A-0002"][0]
    late = replace(attempt.check, check_vt=attempt.sent_vt + 1)
    assert messages(with_attempt(snapshot, "A-0002", 0, check=late)) == [
        "I3: attempt 1 was checked after it was sent"
    ]


def test_i4_more_attempts_than_max_attempts(snapshot: Snapshot) -> None:
    found = messages(snapshot, replace(SETTINGS, max_attempts=1))
    assert found == ["I4: 2 attempts, above max_attempts"]


def test_i5_intent_busy_naming_a_finished_action(snapshot: Snapshot) -> None:
    busy = Refusal(RefusalReason.INTENT_BUSY, "A-0001", S.CONFIRMED)
    last = snapshot.events[-1].seq
    assert messages(with_call(snapshot, busy, last)) == ["I5: intent_busy names a confirmed action"]


def test_i5_reconcile_that_hides_the_escalation(snapshot: Snapshot) -> None:
    calls = list(snapshot.tool_calls)
    index = next(i for i, c in enumerate(calls) if c.action_id == "A-0002")
    result = calls[index].result
    assert isinstance(result, ActionResult) and result.reconcile is not None
    assert result.reconcile.how is ReconcileHow.ESCALATED_TO_HUMAN
    hidden = replace(result.reconcile, how=ReconcileHow.RETRYING_WITH_SAME_KEY)
    calls[index] = replace(calls[index], result=replace(result, reconcile=hidden))
    found = messages(replace(snapshot, tool_calls=tuple(calls)))
    assert found == ["I5: reconcile says retrying_with_same_key"]


def test_i5_a_response_that_reflects_an_older_log(snapshot: Snapshot) -> None:
    early = ActionResult("A-0001", "REQ-1", S.AUTHORIZED, None, Reason.APPROVED, (), None)
    assert messages(with_call(snapshot, early, 3)) == [
        "I5: reflects an older log than the response before it"
    ]


def test_i5_a_report_refusal_that_misstates_the_records(snapshot: Snapshot) -> None:
    wrong = RecordedAction("A-0001", S.FAILED, ())
    conflict = ClaimConflict("REQ-1", ClaimStatus.DONE, ("PO-0001",), (wrong,))
    refusal = Refusal(RefusalReason.REPORT_INCONSISTENT, conflicts=(conflict,))
    assert messages(with_call(snapshot, refusal, snapshot.events[-1].seq)) == [
        "I5: the refusal misstates the record of A-0001"
    ]


def test_i6_a_note_that_changes_the_state(snapshot: Snapshot) -> None:
    events = list(snapshot.events)
    index = next(i for i, e in enumerate(events) if e.reason is Note.BASIS_UPDATED)
    events[index] = replace(events[index], to_state=S.PENDING_APPROVAL)
    found = messages(replace(snapshot, events=tuple(events)))
    assert found == [f"I6: note {events[index].seq} changes the state"]


def test_i6_timestamps_that_differ_from_the_log(snapshot: Snapshot) -> None:
    found = messages(with_action(snapshot, "A-0003", created_vt=-1))
    assert found == ["I6: created_vt or updated_vt differs from the log"]


def test_i6_seqs_out_of_order(snapshot: Snapshot) -> None:
    events = list(snapshot.events)
    events[0], events[1] = replace(events[0], seq=2), replace(events[1], seq=1)
    assert "I6: event seqs must increase" in messages(replace(snapshot, events=tuple(events)))


def test_records_an_answer_that_differs_from_its_event(snapshot: Snapshot) -> None:
    found = messages(with_attempt(snapshot, "A-0002", 0, answer_class=AnswerClass.REJECTED))
    assert found == ["records: attempt 1 answer mismatch"]


def test_records_an_open_escalation_on_a_finished_action(snapshot: Snapshot) -> None:
    escalations = dict(snapshot.escalations)
    escalations["ESC-0002"] = replace(escalations["ESC-0002"], action_id="A-0001")
    found = messages(replace(snapshot, escalations=frozen(escalations)))
    assert "records: open escalation on a confirmed action" in found


# Closing (0003, run closing; 0010, item 6) ------------------------------------------------


def test_records_an_accepted_not_done_on_an_open_intent(snapshot: Snapshot) -> None:
    found = messages(replace(snapshot, closed_intents=frozenset()))
    call_no = snapshot.tool_calls[-1].call_no
    assert found == [f"records: call {call_no} accepted not_done, but the intent is open (0010)"]


def test_records_a_closed_intent_that_is_not_the_tasks(snapshot: Snapshot) -> None:
    found = messages(replace(snapshot, closed_intents=snapshot.closed_intents | {"REQ-9"}))
    assert found == ["records: a closed intent that is not the task's"]


def test_records_a_report_that_was_never_accepted(snapshot: Snapshot) -> None:
    found = messages(replace(snapshot, reports=frozen({"W1": 1})))
    assert found == ["records: call 1 is not an accepted report"]


def test_records_every_report_in_but_the_run_open(snapshot: Snapshot) -> None:
    found = messages(replace(snapshot, run_closed=False))
    assert found == ["records: every expected report was accepted, but the run is open (0010)"]
    two_workers = replace(SETTINGS, agent_workers=2)
    assert messages(replace(snapshot, run_closed=False), two_workers) == []


def test_records_an_unsent_action_in_a_closed_intent() -> None:
    store = MemoryStore(task_id="TASK-1", intents=INTENTS, contract=Contract.C2, settings=SETTINGS)
    result = _write(store, "REQ-1", "k", 1, 0)
    assert isinstance(result, ActionResult)
    closed = replace(store.snapshot(), closed_intents=frozenset({"REQ-1"}))
    assert messages(closed) == ["records: proposed after its intent closed (0010)"]


def test_the_fixture_records_every_read_and_write_call(snapshot: Snapshot) -> None:
    assert list(snapshot.reads) == ["R-0001", "R-0002", "R-0003", "R-0004"]
    admissions = [(c.proposal_id, str(c.admission), c.action_id) for c in snapshot.proposals]
    assert admissions == [
        ("P-0001", "admitted", "A-0001"),
        ("P-0002", "admitted", "A-0002"),
        ("P-0003", "admitted", "A-0003"),
        ("P-0004", "attached", "A-0003"),
    ]


def test_admission_an_action_no_write_call_created(snapshot: Snapshot) -> None:
    found = messages(replace(snapshot, proposals=snapshot.proposals[1:]))
    assert found == [
        "records: write call 1 has the wrong id",
        "records: write call 2 has the wrong id",
        "records: write call 3 has the wrong id",
        "records: no admitted write call created it",
    ]


def test_admission_a_citation_of_a_later_read(snapshot: Snapshot) -> None:
    first = snapshot.proposals[0]
    late = replace(first, based_on=("R-0004",))
    found = messages(replace(snapshot, proposals=(late, *snapshot.proposals[1:])))
    assert found == ["records: cites R-0004, which it could not"]


def test_admission_an_attached_call_naming_another_intent(snapshot: Snapshot) -> None:
    attached = replace(snapshot.proposals[3], action_id="A-0001")
    found = messages(replace(snapshot, proposals=(*snapshot.proposals[:3], attached)))
    assert found == ["records: names A-0001 of another intent"]


def test_admission_reads_numbered_with_a_gap(snapshot: Snapshot) -> None:
    reads = {k: v for k, v in snapshot.reads.items() if k != "R-0002"}
    found = messages(replace(snapshot, reads=frozen(reads)))
    assert "records: read ids are not numbered 1 to n" in found


# G1: the checks scoped to rules G1 follows (0003, 0008) ------------------------------------


@pytest.fixture
def g1_snapshot() -> Snapshot:
    """A valid G1 run at C2. REQ-1's order is confirmed. REQ-2's send timed out, and the
    action stays OutcomeUnknown: no retry and no escalation. The agent was told both."""
    store = g1_store()
    first = _send_g1(store, "REQ-1", 0)
    store.answer(first, 1, Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001"), 10)
    second = _send_g1(store, "REQ-2", 20)
    store.answer(second, 1, Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT), 23 + 200)
    store.status(first, 300)
    store.status(second, 300)
    return store.snapshot()


def test_g1_the_fixture_is_valid(g1_snapshot: Snapshot) -> None:
    assert check_invariants(g1_snapshot, SETTINGS) == []
    actions = g1_snapshot.actions
    assert [(a.state, a.identity) for a in actions.values()] == [
        (S.CONFIRMED, False),
        (S.OUTCOME_UNKNOWN, False),
    ]
    assert not g1_snapshot.escalations
    kinds = {t.check.kind for attempts in g1_snapshot.attempts.values() for t in attempts}
    assert kinds == {CheckKind.AUTHORITY_ONLY}


def test_i3_g1_checks_the_approval_only_and_g2_still_checks_the_basis(
    snapshot: Snapshot, g1_snapshot: Snapshot
) -> None:
    (g1_attempt,) = g1_snapshot.attempts["A-0001"]
    full = replace(g1_attempt.check, kind=CheckKind.FULL, basis=FRESH)
    assert messages(with_attempt(g1_snapshot, "A-0001", 0, check=full)) == [
        "I3: attempt 1 had a full check, not authority_only"
    ]
    g2_attempt = snapshot.attempts["A-0001"][0]
    approval_only = replace(g2_attempt.check, kind=CheckKind.AUTHORITY_ONLY, basis=None)
    assert messages(with_attempt(snapshot, "A-0001", 0, check=approval_only)) == [
        "I3: attempt 1 had a authority_only check, not full"
    ]


def test_i3_g1_never_sends_after_a_failing_authority_check(g1_snapshot: Snapshot) -> None:
    (attempt,) = g1_snapshot.attempts["A-0001"]
    revoked = replace(attempt.check, approval=ApprovalStatus.REVOKED, approval_expires_at=None)
    expiring = replace(attempt.check, approval_expires_at=attempt.sent_vt + SETTINGS.settle_ms)
    for failing in (revoked, expiring):
        assert messages(with_attempt(g1_snapshot, "A-0001", 0, check=failing)) == [
            "I3: attempt 1 was sent after a failing check"
        ]


@pytest.mark.parametrize("contract", [Contract.C2, Contract.C3])
def test_i4_g1_never_retries_even_where_the_contract_has_keys(
    g1_snapshot: Snapshot, contract: Contract
) -> None:
    """A second attempt is fine for G2 at C2 and C3 (the `snapshot` fixture's REQ-1)."""
    (attempt,) = g1_snapshot.attempts["A-0001"]
    second = replace(attempt, attempt_id="A-0001/2", attempt_no=2)
    attempts = {**g1_snapshot.attempts, "A-0001": (attempt, second)}
    actions = {k: replace(a, contract=contract) for k, a in g1_snapshot.actions.items()}
    found = messages(replace(g1_snapshot, attempts=frozen(attempts), actions=frozen(actions)))
    assert "I4: 2 attempts; G1 never retries (0008)" in found
    assert "I3: attempt 2 is a retry, which G1 forbids" in found


def test_i5_a_g1_result_never_says_how_it_is_reconciled(
    snapshot: Snapshot, g1_snapshot: Snapshot
) -> None:
    """G2 must say how (`test_i5_outcome_unknown_reported_without_how_it_is_reconciled`);
    G1 reconciles nothing, so saying any way would be untrue."""
    calls = list(g1_snapshot.tool_calls)
    index = next(i for i, c in enumerate(calls) if c.action_id == "A-0002")
    result = calls[index].result
    assert isinstance(result, ActionResult)
    assert (result.state, result.reconcile) == (S.OUTCOME_UNKNOWN, None)
    g2_result = next(c.result for c in snapshot.tool_calls if c.action_id == "A-0002")
    assert isinstance(g2_result, ActionResult) and g2_result.reconcile is not None
    calls[index] = replace(calls[index], result=replace(result, reconcile=g2_result.reconcile))
    assert messages(replace(g1_snapshot, tool_calls=tuple(calls))) == [
        "I5: reconcile on a G1 result; G1 reconciles nothing"
    ]


def test_g1_actions_are_never_escalated_and_write_no_notes(g1_snapshot: Snapshot) -> None:
    """The `snapshot` fixture's G2 run escalates REQ-2 and takes a newer basis for REQ-3,
    both with notes, and is valid."""
    note = ActionEvent(
        g1_snapshot.events[-1].seq + 1,
        "A-0002",
        EventKind.NOTE,
        S.OUTCOME_UNKNOWN,
        S.OUTCOME_UNKNOWN,
        Note.ESCALATED,
        frozen({"escalation_id": "ESC-0002", "trigger": "at_once"}),
        300,
    )
    escalation = Escalation("ESC-0002", "A-0002", 300, None, None, None, None)
    escalated = replace(
        g1_snapshot,
        events=(*g1_snapshot.events, note),
        escalations=frozen({"ESC-0002": escalation}),
    )
    assert messages(escalated) == [
        f"G1: note {note.seq} (escalated); G1 writes no notes",
        "G1: escalated; G1 has no recovery",
    ]


def test_nothing_attaches_to_a_g1_action(g1_snapshot: Snapshot) -> None:
    """The `snapshot` fixture's G2 run attaches its fourth write call to REQ-3's action."""
    first = g1_snapshot.proposals[0]
    attached = replace(first, proposal_id="P-0003", admission=Admission.ATTACHED, vt=300)
    busy = replace(
        first,
        proposal_id="P-0004",
        admission=Admission.REFUSED,
        reason=RefusalReason.INTENT_DONE,
        vt=300,
    )
    found = messages(replace(g1_snapshot, proposals=(*g1_snapshot.proposals, attached, busy)))
    assert found == [
        "G1: matched A-0001; G1 has no action identity",
        "G1: matched A-0001; G1 has no action identity",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"run_closed": True},
        {"closed_intents": frozenset({"REQ-2"})},
    ],
)
def test_a_run_with_g1_actions_never_closes(
    g1_snapshot: Snapshot, change: dict[str, object]
) -> None:
    """The `snapshot` fixture's G2 run closes REQ-3 and the run, and is valid."""
    found = messages(replace(g1_snapshot, **change))  # type: ignore[arg-type]
    assert found == ["G1: closed or reported; G1 has no report check or run closing"]


def test_a_run_has_one_configuration(snapshot: Snapshot) -> None:
    assert "G1" in labels(with_action(snapshot, "A-0003", identity=False))


def test_g1_never_rejects_for_stale_input_or_a_closed_run() -> None:
    """G1 checks no fact and never closes the run (0008); G2 uses both reasons."""
    store = g1_store()
    result = _write(store, "REQ-1", "k", 1, 0)
    assert isinstance(result, ActionResult)
    proposed = store.snapshot()
    for reason in (Reason.STALE_INPUT, Reason.RUN_CLOSED):
        seq = proposed.events[-1].seq + 1
        rejected = ActionEvent(
            seq, "A-0001", EventKind.TRANSITION, S.PROPOSED, S.REJECTED, reason, frozen({}), 5
        )
        broken = with_action(
            replace(proposed, events=(*proposed.events, rejected)),
            "A-0001",
            state=S.REJECTED,
            updated_vt=5,
        )
        assert messages(broken) == [f"G1: event {seq} has reason {reason}"]
