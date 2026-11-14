"""Invariants I1 to I7 as checks over a run's gateway records (0003).

`check_invariants` reads only a `Snapshot`. Both stores run it in every writing
transaction before it commits (`store.py`), and the Hypothesis machines run it after
every rule, in memory and on rows loaded from PostgreSQL. Each check restates its rule
from the decision records and never calls the step functions, so a wrong step cannot
hide its own mistake. The table of transitions is the one exception: it is the rule
itself.

What each check covers:

- I1: the effect id is set exactly when the action is Confirmed, matches the event that
  confirmed it, and matches the committed attempt's answer.
- I2: one grant per action and per approval; every attempt uses the action's grant; the
  grant was consumed exactly at the first send; the grant's state fits the action's
  state (0002, rule 14).
- I3: every attempt has a passing check of the right kind, made before the send, with no
  other event on the action between the check and the send.
- I4: attempts are numbered 1 to n and share the action's key, which is its id; at most
  one attempt without idempotency keys; at most `max_attempts`.
- I5: every logged response reports the state derived from the event log at its seq,
  responses reflect the log in the order they were given, every refusal states the
  records truthfully, and every accepted report agrees with the records at its seq.
- I6: event seqs increase (a database sequence may leave gaps) and time never runs
  backwards; every action's transitions form a legal chain from T1 that ends in its
  current state, with matching timestamps; notes keep the state.
- I7: at most one live or Confirmed action per intent, among actions with identity on.
- Records: attempts' answers, escalations and notes agree with each other, and a closed
  run holds no live action that was never sent (0003, run closing).

Two parts are checked elsewhere. That the log is append-only and that an effect id
never changes compare two snapshots, so the Hypothesis suites check them between steps.
Row locks and privileges are database properties, checked in the Postgres suites.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from fivefold.domain.contracts import Capability, Contract
from fivefold.gateway.records import (
    Action,
    ActionEvent,
    ActionResult,
    Attempt,
    Claim,
    ClaimStatus,
    Grant,
    Refusal,
    RefusalReason,
    ReportAccepted,
    Snapshot,
    ToolCall,
)
from fivefold.gateway.state_machine import (
    ANSWER_REASONS,
    HOLDING_STATES,
    LIVE_STATES,
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

_S = ActionState
_G = GrantState


def _expected_check(contract: Contract, attempt_no: int) -> CheckKind | None:
    """The check a send needs (I3), or None for a retry the contract forbids (I4)."""
    if attempt_no == 1:
        return CheckKind.FULL
    if not contract.has(Capability.IDEMPOTENCY_KEY):
        return None
    if contract.has(Capability.CONDITIONAL_WRITE):
        return CheckKind.EXEMPT
    return CheckKind.EXACT_ONLY


def _expected_how(contract: Contract, *, escalated: bool) -> ReconcileHow:
    """What the agent is told about an unknown outcome (0003, 0005)."""
    if escalated or not contract.capabilities:
        return ReconcileHow.ESCALATED_TO_HUMAN
    if contract.has(Capability.STATUS):
        return ReconcileHow.QUERYING_STATUS
    return ReconcileHow.RETRYING_WITH_SAME_KEY


@dataclass(frozen=True)
class Violation:
    """One broken rule: the invariant, the record it concerns, and what is wrong."""

    invariant: str
    subject: str
    message: str

    def __str__(self) -> str:
        return f"{self.invariant} {self.subject}: {self.message}"


class InvariantViolation(AssertionError):
    """Raised when records break an invariant. Holds every violation found."""

    def __init__(self, violations: Sequence[Violation]) -> None:
        self.violations = tuple(violations)
        super().__init__("; ".join(str(v) for v in self.violations))


def check_invariants(snapshot: Snapshot, settings: GatewaySettings) -> list[Violation]:
    """Return every violation of I1 to I7 in `snapshot`; an empty list means none."""
    events_by_action: dict[str, list[ActionEvent]] = {}
    for event in snapshot.events:
        events_by_action.setdefault(event.action_id, []).append(event)
    found: list[Violation] = []
    found += _records(snapshot, events_by_action)
    found += _i6_log(snapshot)
    for action in snapshot.actions.values():
        events = events_by_action.get(action.action_id, [])
        attempts = snapshot.attempts.get(action.action_id, ())
        grant = snapshot.grants.get(action.grant_id) if action.grant_id else None
        found += _i1(action, events, attempts)
        found += _i2(action, grant, attempts, snapshot)
        found += _i3(action, events, attempts)
        found += _i4(action, attempts, settings)
        found += _i6_action(action, events)
    found += _i2_global(snapshot)
    found += _i5(snapshot, events_by_action)
    found += _i7(snapshot)
    return found


# I1 -----------------------------------------------------------------------------------


def _i1(
    action: Action, events: list[ActionEvent], attempts: Sequence[Attempt]
) -> Iterator[Violation]:
    a = action.action_id
    confirms = [e for e in events if e.kind is EventKind.TRANSITION and e.to_state is _S.CONFIRMED]
    if action.state is _S.CONFIRMED:
        if action.effect_id is None:
            yield Violation("I1", a, "Confirmed without an effect id")
        if len(confirms) != 1:
            yield Violation("I1", a, f"{len(confirms)} transitions into Confirmed")
        elif confirms[0].details.get("effect_id") != action.effect_id:
            yield Violation("I1", a, "the effect id differs from the one its event recorded")
    else:
        if action.effect_id is not None:
            yield Violation("I1", a, f"effect id {action.effect_id} on a {action.state} action")
        if confirms:
            yield Violation("I1", a, "a transition into Confirmed, but the action is not Confirmed")
    committed = [t for t in attempts if t.answer_class is AnswerClass.COMMITTED]
    if len(committed) > 1:
        yield Violation("I1", a, f"{len(committed)} attempts answered committed")
    for attempt in committed:
        if attempt.answer_ref != action.effect_id:
            yield Violation("I1", a, f"attempt {attempt.attempt_no} committed another effect")


# I2 -----------------------------------------------------------------------------------

_GRANT_FOR_STATE: Mapping[ActionState, frozenset[GrantState]] = {
    _S.PENDING_APPROVAL: frozenset({_G.PENDING}),
    _S.AUTHORIZED: frozenset({_G.ACTIVE}),
    _S.DISPATCHED: frozenset({_G.CONSUMED, _G.REVOKED, _G.EXPIRED}),
    _S.OUTCOME_UNKNOWN: frozenset({_G.CONSUMED, _G.REVOKED, _G.EXPIRED}),
    _S.CONFIRMED: frozenset({_G.CONSUMED, _G.REVOKED, _G.EXPIRED}),
    _S.FAILED: frozenset({_G.CONSUMED, _G.REVOKED, _G.EXPIRED}),
    _S.REJECTED: frozenset({_G.DENIED, _G.TIMED_OUT, _G.RELEASED, _G.REVOKED, _G.EXPIRED}),
}
"""Grant states that fit each action state (0002 rule 14, 0003). Proposed has no grant."""


def _i2(
    action: Action, grant: Grant | None, attempts: Sequence[Attempt], snapshot: Snapshot
) -> Iterator[Violation]:
    a = action.action_id
    owned = [g for g in snapshot.grants.values() if g.action_id == a]
    if len(owned) > 1:
        yield Violation("I2", a, f"{len(owned)} grants")
    if action.grant_id is None:
        if owned:
            yield Violation("I2", a, f"grant {owned[0].grant_id} is not recorded on the action")
    elif grant is None:
        yield Violation("I2", a, f"grant {action.grant_id} does not exist")
    elif grant.action_id != a:
        yield Violation("I2", a, f"grant {grant.grant_id} belongs to {grant.action_id}")
    for attempt in attempts:
        if attempt.grant_id != action.grant_id:
            yield Violation("I2", a, f"attempt {attempt.attempt_no} uses another grant")
    if grant is None:
        if action.state is not _S.PROPOSED and action.state is not _S.REJECTED:
            yield Violation("I2", a, f"no grant in state {action.state}")
        return
    allowed = _GRANT_FOR_STATE.get(action.state, frozenset())
    if grant.state not in allowed:
        yield Violation("I2", a, f"grant {grant.state} does not fit state {action.state}")
    if attempts:
        if grant.consumed_vt != attempts[0].sent_vt:
            yield Violation("I2", a, "the grant was not consumed at the first send")
        if grant.state not in (_G.CONSUMED, _G.REVOKED, _G.EXPIRED):
            yield Violation("I2", a, f"attempts sent under a {grant.state} grant")
    elif grant.consumed_vt is not None or grant.state is _G.CONSUMED:
        yield Violation("I2", a, "the grant is consumed but nothing was sent")


def _i2_global(snapshot: Snapshot) -> Iterator[Violation]:
    approvals: dict[str, str] = {}
    for grant in snapshot.grants.values():
        if grant.action_id not in snapshot.actions:
            yield Violation("I2", grant.grant_id, f"no action {grant.action_id}")
        other = approvals.setdefault(grant.approval_id, grant.grant_id)
        if other != grant.grant_id:
            yield Violation(
                "I2", grant.grant_id, f"approval {grant.approval_id} also backs {other}"
            )


# I3 -----------------------------------------------------------------------------------


def _i3(
    action: Action, events: list[ActionEvent], attempts: Sequence[Attempt]
) -> Iterator[Violation]:
    a = action.action_id
    for attempt in attempts:
        n = attempt.attempt_no
        check = attempt.check
        expected = _expected_check(action.contract, n)
        if expected is None:
            yield Violation("I3", a, f"attempt {n} is a retry, which {action.contract} forbids")
            continue
        if check.kind is not expected:
            yield Violation("I3", a, f"attempt {n} had a {check.kind} check, not {expected}")
        if not check.passed:
            yield Violation("I3", a, f"attempt {n} was sent after a failing check")
        if check.check_vt > attempt.sent_vt:
            yield Violation("I3", a, f"attempt {n} was checked after it was sent")
        sends = [
            i
            for i, e in enumerate(events)
            if e.kind is EventKind.TRANSITION
            and e.to_state is _S.DISPATCHED
            and e.details.get("attempt_no") == n
        ]
        if len(sends) != 1:
            yield Violation("I3", a, f"attempt {n} has {len(sends)} dispatch events")
            continue
        index = sends[0]
        if events[index].vt != attempt.sent_vt:
            yield Violation("I3", a, f"attempt {n} was recorded at another time than its send")
        if index == 0 or events[index - 1].seq != check.seen_seq:
            yield Violation("I3", a, f"attempt {n}: another event came between check and send")


# I4 -----------------------------------------------------------------------------------


def _i4(
    action: Action, attempts: Sequence[Attempt], settings: GatewaySettings
) -> Iterator[Violation]:
    a = action.action_id
    if [t.attempt_no for t in attempts] != list(range(1, len(attempts) + 1)):
        yield Violation("I4", a, "attempts are not numbered 1 to n")
    if action.idempotency_key != a:
        yield Violation("I4", a, "the idempotency key is not the action id (0005)")
    for attempt in attempts:
        if attempt.idempotency_key != action.idempotency_key:
            yield Violation("I4", a, f"attempt {attempt.attempt_no} used another key")
        if attempt.action_id != a:
            yield Violation("I4", a, f"attempt {attempt.attempt_id} belongs to {attempt.action_id}")
    if not action.contract.has(Capability.IDEMPOTENCY_KEY) and len(attempts) > 1:
        yield Violation("I4", a, f"{len(attempts)} attempts at {action.contract}")
    if len(attempts) > settings.max_attempts:
        yield Violation("I4", a, f"{len(attempts)} attempts, above max_attempts")


# I5 -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Derived:
    state: ActionState
    reason: Reason
    effect_id: str | None
    escalation_id: str | None
    effects: tuple[str, ...]
    """Every effect id learned so far: the recorded one and any a status answer listed."""


def _derive(events: Sequence[ActionEvent], as_of_seq: int) -> _Derived | None:
    """Replay an action's events up to `as_of_seq` (I6 makes this the action's state)."""
    derived: _Derived | None = None
    for event in events:
        if event.seq > as_of_seq:
            break
        if event.kind is EventKind.TRANSITION and isinstance(event.reason, Reason):
            effect = event.details.get("effect_id") if event.to_state is _S.CONFIRMED else None
            effect_id = effect if isinstance(effect, str) else None
            effects = list(derived.effects) if derived is not None else []
            listed = event.details.get("listed", ())
            for found in (effect_id, *(listed if isinstance(listed, tuple) else ())):
                if isinstance(found, str) and found not in effects:
                    effects.append(found)
            escalation = derived.escalation_id if derived is not None else None
            derived = _Derived(event.to_state, event.reason, effect_id, escalation, tuple(effects))
        elif event.reason is Note.ESCALATED and derived is not None:
            escalation_id = event.details.get("escalation_id")
            if isinstance(escalation_id, str):
                derived = _Derived(
                    derived.state, derived.reason, derived.effect_id, escalation_id, derived.effects
                )
    return derived


def _i5(
    snapshot: Snapshot, events_by_action: Mapping[str, list[ActionEvent]]
) -> Iterator[Violation]:
    previous = 0
    for call in snapshot.tool_calls:
        subject = f"call {call.call_no}"
        if call.as_of_seq < previous:
            yield Violation("I5", subject, "reflects an older log than the response before it")
        previous = max(previous, call.as_of_seq)
        result = call.result
        if isinstance(result, ActionResult):
            yield from _i5_result(snapshot, events_by_action, call, result, subject)
        elif isinstance(result, Refusal):
            yield from _i5_refusal(events_by_action, call, result, subject)
        else:
            yield from _i5_report(snapshot, events_by_action, call, result, subject)


def _i5_result(
    snapshot: Snapshot,
    events_by_action: Mapping[str, list[ActionEvent]],
    call: ToolCall,
    result: ActionResult,
    subject: str,
) -> Iterator[Violation]:
    derived = _derive(events_by_action.get(result.action_id, []), call.as_of_seq)
    if derived is None:
        yield Violation("I5", subject, f"{result.action_id} did not exist at seq {call.as_of_seq}")
        return
    if result.state is not derived.state:
        yield Violation("I5", subject, f"reported {result.state}, records say {derived.state}")
    if result.reason is not derived.reason:
        yield Violation(
            "I5", subject, f"reported reason {result.reason}, records say {derived.reason}"
        )
    if result.effect_id != derived.effect_id:
        yield Violation("I5", subject, "the effect id differs from the records")
    if (result.reconcile is not None) != (derived.state is _S.OUTCOME_UNKNOWN):
        yield Violation("I5", subject, "reconcile must appear exactly with outcome_unknown")
    action = snapshot.actions.get(result.action_id)
    if result.reconcile is not None and action is not None:
        escalated = derived.escalation_id is not None
        if result.reconcile.how is not _expected_how(action.contract, escalated=escalated):
            yield Violation("I5", subject, f"reconcile says {result.reconcile.how}")
        if result.reconcile.escalation_id != derived.escalation_id:
            yield Violation("I5", subject, "the escalation id differs from the records")


def _i5_refusal(
    events_by_action: Mapping[str, list[ActionEvent]],
    call: ToolCall,
    refusal: Refusal,
    subject: str,
) -> Iterator[Violation]:
    if refusal.reason is RefusalReason.REPORT_INCONSISTENT:
        for conflict in refusal.conflicts:
            for recorded in conflict.recorded:
                derived = _derive(events_by_action.get(recorded.action_id, []), call.as_of_seq)
                if derived is None or (recorded.state, recorded.effect_ids) != (
                    derived.state,
                    derived.effects,
                ):
                    yield Violation(
                        "I5", subject, f"the refusal misstates the record of {recorded.action_id}"
                    )
        return
    if refusal.reason not in (RefusalReason.INTENT_BUSY, RefusalReason.INTENT_DONE):
        return
    if refusal.action_id is None:
        yield Violation("I5", subject, f"{refusal.reason} names no action")
        return
    derived = _derive(events_by_action.get(refusal.action_id, []), call.as_of_seq)
    if derived is None or refusal.state is not derived.state:
        yield Violation("I5", subject, f"{refusal.reason} reports a state the records lack")
        return
    if refusal.reason is RefusalReason.INTENT_BUSY and derived.state not in LIVE_STATES:
        yield Violation("I5", subject, f"intent_busy names a {derived.state} action")
    if refusal.reason is RefusalReason.INTENT_DONE and (
        derived.state is not _S.CONFIRMED or refusal.effect_id != derived.effect_id
    ):
        yield Violation("I5", subject, "intent_done does not name the confirmed effect")


def _i5_report(
    snapshot: Snapshot,
    events_by_action: Mapping[str, list[ActionEvent]],
    call: ToolCall,
    report: ReportAccepted,
    subject: str,
) -> Iterator[Violation]:
    if sorted(c.intent_id for c in report.claims) != sorted(snapshot.intents):
        yield Violation("I5", subject, "the accepted report does not cover each intent once")
    for claim in report.claims:
        states: list[ActionState] = []
        effects: list[str] = []
        for action in snapshot.actions.values():
            if action.intent_id != claim.intent_id:
                continue
            derived = _derive(events_by_action.get(action.action_id, []), call.as_of_seq)
            if derived is None:
                continue
            states.append(derived.state)
            effects.extend(e for e in derived.effects if e not in effects)
        if not _claim_holds(claim, states, effects):
            yield Violation("I5", subject, f"accepted {claim.status} for {claim.intent_id}")


def _claim_holds(claim: Claim, states: list[ActionState], effects: list[str]) -> bool:
    if claim.status is ClaimStatus.DONE:
        return _S.CONFIRMED in states and sorted(claim.effect_ids) == sorted(effects)
    if claim.status is ClaimStatus.NOT_DONE:
        return all(
            s in (_S.REJECTED, _S.FAILED, _S.PROPOSED, _S.PENDING_APPROVAL, _S.AUTHORIZED)
            for s in states
        )
    return any(s in (_S.DISPATCHED, _S.OUTCOME_UNKNOWN) for s in states)


# I6 -----------------------------------------------------------------------------------


def _i6_log(snapshot: Snapshot) -> Iterator[Violation]:
    if snapshot.events and snapshot.events[0].seq < 1:
        yield Violation("I6", "log", "event seqs start at 1")
    for before, after in itertools.pairwise(snapshot.events):
        if after.seq <= before.seq:
            yield Violation("I6", f"seq {after.seq}", "event seqs must increase")
        if after.vt < before.vt:
            yield Violation("I6", f"seq {after.seq}", "virtual time went backwards")
    last = snapshot.events[-1].seq if snapshot.events else 0
    for call in snapshot.tool_calls:
        if call.as_of_seq > last:
            yield Violation("I6", f"call {call.call_no}", "reflects an event that does not exist")


def _i6_action(action: Action, events: list[ActionEvent]) -> Iterator[Violation]:
    a = action.action_id
    if not events:
        yield Violation("I6", a, "no events")
        return
    first = events[0]
    if first.kind is not EventKind.TRANSITION or first.from_state is not None:
        yield Violation("I6", a, "the first event is not T1")
        return
    current: ActionState | None = None
    last_transition_vt = first.vt
    for event in events:
        if event.kind is EventKind.NOTE:
            if not isinstance(event.reason, Note):
                yield Violation("I6", a, f"note {event.seq} has a transition reason")
            if event.from_state is not current or event.to_state is not current:
                yield Violation("I6", a, f"note {event.seq} changes the state")
            continue
        if not isinstance(event.reason, Reason):
            yield Violation("I6", a, f"transition {event.seq} has a note reason")
            continue
        if event.from_state is not current:
            yield Violation(
                "I6", a, f"transition {event.seq} starts from {event.from_state}, not {current}"
            )
        try:
            check_transition(event.from_state, event.to_state, event.reason, action.contract)
        except IllegalTransition as error:
            yield Violation("I6", a, f"transition {event.seq}: {error}")
        current = event.to_state
        last_transition_vt = event.vt
    if current is not action.state:
        yield Violation(
            "I6", a, f"replaying the log gives {current}, the action says {action.state}"
        )
    if action.created_vt != first.vt or action.updated_vt != last_transition_vt:
        yield Violation("I6", a, "created_vt or updated_vt differs from the log")


# I7 -----------------------------------------------------------------------------------


def _i7(snapshot: Snapshot) -> Iterator[Violation]:
    holders: dict[str, list[str]] = {}
    for action in snapshot.actions.values():
        if action.identity and action.state in HOLDING_STATES:
            holders.setdefault(action.intent_id, []).append(action.action_id)
    for intent_id, ids in holders.items():
        if len(ids) > 1:
            yield Violation("I7", intent_id, f"live or Confirmed actions {', '.join(sorted(ids))}")


# Records ------------------------------------------------------------------------------


def _records(
    snapshot: Snapshot, events_by_action: Mapping[str, list[ActionEvent]]
) -> Iterator[Violation]:
    """Cross-record consistency that the invariants above rely on."""
    for action_id in events_by_action:
        if action_id not in snapshot.actions:
            yield Violation("records", action_id, "events for an unknown action")
    for action_id, attempts in snapshot.attempts.items():
        action = snapshot.actions.get(action_id)
        if action is None:
            yield Violation("records", action_id, "attempts for an unknown action")
            continue
        events = events_by_action.get(action_id, [])
        answers = [
            e
            for e in events
            if e.kind is EventKind.TRANSITION
            and e.from_state is _S.DISPATCHED
            and e.reason is not Reason.RECOVERED_OPEN_ATTEMPT
        ]
        answered = [t for t in attempts if t.answer_class is not None]
        if len(answers) != len(answered):
            yield Violation("records", action_id, "answered attempts and answer events differ")
        for attempt, event in zip(answered, answers, strict=False):
            reasons = ANSWER_REASONS[attempt.answer_class] if attempt.answer_class else frozenset()
            if event.reason not in reasons or event.vt != attempt.answered_vt:
                yield Violation(
                    "records", action_id, f"attempt {attempt.attempt_no} answer mismatch"
                )
        if action.state is _S.DISPATCHED and (
            not attempts or attempts[-1].answer_class is not None
        ):
            yield Violation("records", action_id, "Dispatched without an open attempt")
    escalated_actions: set[str] = set()
    for escalation in snapshot.escalations.values():
        a = escalation.action_id
        action = snapshot.actions.get(a)
        if action is None:
            yield Violation("records", escalation.escalation_id, f"no action {a}")
            continue
        if a in escalated_actions:
            yield Violation("records", a, "more than one escalation")
        escalated_actions.add(a)
        notes = [
            e
            for e in events_by_action.get(a, [])
            if e.reason is Note.ESCALATED
            and e.details.get("escalation_id") == escalation.escalation_id
        ]
        if len(notes) != 1:
            yield Violation(
                "records", a, f"{len(notes)} escalated notes for {escalation.escalation_id}"
            )
        if escalation.resolved_vt is None:
            if action.state is not _S.OUTCOME_UNKNOWN:
                yield Violation("records", a, f"open escalation on a {action.state} action")
        else:
            events = events_by_action.get(a, [])
            last = events[-1] if events else None
            if (
                last is None
                or last.reason is not Reason.HUMAN_ATTESTED
                or last.vt != escalation.resolved_vt
            ):
                yield Violation("records", a, "a resolved escalation without its attestation")
    for event in snapshot.events:
        if event.reason is Note.ESCALATED and event.action_id not in escalated_actions:
            yield Violation("records", event.action_id, "an escalated note without an escalation")
    if snapshot.run_closed:
        for action in snapshot.actions.values():
            if action.state in (_S.PROPOSED, _S.PENDING_APPROVAL, _S.AUTHORIZED):
                yield Violation(
                    "records", action.action_id, f"{action.state} after the run closed (0003)"
                )
