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
    ActionResult,
    Answer,
    ApprovalStatus,
    BasisCheck,
    Claim,
    ClaimStatus,
    Proposal,
    ReportAccepted,
    Snapshot,
    ToolCall,
    frozen,
)
from fivefold.gateway.state_machine import (
    ActionState,
    AnswerClass,
    CheckKind,
    GatewaySettings,
    GrantState,
    Reason,
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
INTENTS = {"REQ-1": "submit_purchase_order", "REQ-2": "submit_purchase_order", "REQ-3": "x"}


def _send(store: MemoryStore, intent: str, vt: int) -> tuple[str, int]:
    """Propose, approve and send one action for `intent`; return its id and the time."""
    result = store.propose(
        Proposal(intent, INTENTS[intent], "t", {}, f"key-{intent}", {"f": 1}), vt
    )
    assert isinstance(result, ActionResult)
    action_id = result.action_id
    store.file_approval(action_id, FRESH, f"AP-{action_id}", vt + 1)
    store.poll_approval(action_id, ApprovalStatus.APPROVED, FRESH, vt + 2)
    ticket = store.begin_check(action_id)
    check = ticket.record(approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=vt + 3)
    assert store.dispatch(action_id, check, vt + 3) is not None
    return action_id, vt + 3


@pytest.fixture
def snapshot() -> Snapshot:
    """A valid C2 run: REQ-1 confirmed after a retry, REQ-2 unknown, REQ-3 authorized."""
    store = MemoryStore(task_id="TASK-1", intents=INTENTS, contract=Contract.C2, settings=SETTINGS)
    first, vt = _send(store, "REQ-1", 0)
    store.answer(first, 1, Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT), vt + 200)
    ticket = store.begin_check(first)
    retry = ticket.record(
        approval=ApprovalStatus.APPROVED,
        basis=BasisCheck(exact_ok=True, sufficient_ok=None),
        check_vt=ticket.not_before_vt,
    )
    assert store.retry(first, retry, ticket.not_before_vt) is not None
    committed = Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001")
    store.answer(first, 2, committed, ticket.not_before_vt + 10)
    second, vt = _send(store, "REQ-2", ticket.not_before_vt + 20)
    store.answer(second, 1, Answer(AnswerClass.UNKNOWN, Reason.SERVER_ERROR), vt + 5)
    third = store.propose(Proposal("REQ-3", "x", "t", {}, "key-REQ-3", {"f": 1}), vt + 6)
    assert isinstance(third, ActionResult)
    store.file_approval(third.action_id, FRESH, "AP-REQ-3", vt + 7)
    store.poll_approval(third.action_id, ApprovalStatus.APPROVED, FRESH, vt + 8)
    store.status(first, vt + 9)
    store.status(second, vt + 9)
    claims = [
        Claim("REQ-1", ClaimStatus.DONE, ("PO-0001",)),
        Claim("REQ-2", ClaimStatus.UNKNOWN),
        Claim("REQ-3", ClaimStatus.NOT_DONE),
    ]
    assert isinstance(store.submit_report(claims, vt + 10), ReportAccepted)
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
    assert states == [S.CONFIRMED, S.OUTCOME_UNKNOWN, S.AUTHORIZED]


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
    failing = replace(attempt.check, approval=ApprovalStatus.REVOKED)
    attempts["A-0002"] = (replace(attempt, check=failing),)
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
        Claim("REQ-1", ClaimStatus.DONE, ("PO-0001",)),
        Claim("REQ-2", ClaimStatus.NOT_DONE),
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


def test_i6_a_gap_in_the_log(snapshot: Snapshot) -> None:
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


def test_i7_two_live_actions_for_one_intent() -> None:
    store = MemoryStore(
        task_id="TASK-1", intents=INTENTS, contract=Contract.C2, settings=SETTINGS, identity=False
    )
    _send(store, "REQ-1", 0)
    _send(store, "REQ-1", 10)
    g1 = store.snapshot()
    assert check_invariants(g1, SETTINGS) == []
    identity_on = {k: replace(a, identity=True) for k, a in g1.actions.items()}
    assert labels(replace(g1, actions=frozen(identity_on))) == {"I7"}
