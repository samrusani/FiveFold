"""The gateway's decisions, one pure function per step (0001, 0002, 0003, 0005).

Each step reads an `ActionView` of one action's records plus its own inputs, and returns
the `Change` to apply, or None when there is nothing to do. Steps never mutate anything
and never do I/O, and every state change goes through `check_transition`. A store
applies a change in one transaction, with its events, while holding the action's row
lock (I6). Both stores, in memory and in PostgreSQL, call these functions through the
operations in `store.py`, so each rule exists once.

Steps raise `StepRefused` when a step does not apply (wrong state, too early, already
escalated) and `UnspecifiedCase` when the accepted records define no outcome. Malformed
inputs raise `ValueError`. In all three cases nothing changes.

Ids that depend on one action are derived from its id: grant `G-0007` and escalation
`ESC-0007` belong to action `A-0007`, and its attempts are `A-0007/1`, `A-0007/2` and so
on. One grant and at most one escalation per action follow from the rules (I2; an
escalated action leaves OutcomeUnknown only by attestation).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from fivefold.domain.contracts import Capability, Contract
from fivefold.gateway.records import (
    NO_DETAILS,
    Action,
    ActionEvent,
    ActionResult,
    Answer,
    ApprovalStatus,
    Attempt,
    BasisCheck,
    CheckRecord,
    CheckTicket,
    Claim,
    ClaimConflict,
    ClaimStatus,
    Escalation,
    Grant,
    ProbeAnswer,
    Proposal,
    Reconcile,
    RecordedAction,
    Resolution,
    StaleFact,
    frozen,
)
from fivefold.gateway.state_machine import (
    HOLDING_STATES,
    UNDISPATCHED_STATES,
    UNSETTLED_STATES,
    ActionState,
    AnswerClass,
    CheckKind,
    EscalationTrigger,
    EventKind,
    GatewayRuleError,
    GatewaySettings,
    GrantState,
    Note,
    Reason,
    check_grant_transition,
    check_transition,
    reconcile_how,
    required_check,
)

_S = ActionState


class StepRefused(GatewayRuleError):
    """The step does not apply to the action in its current state, or not yet."""


class UnspecifiedCase(GatewayRuleError):
    """The accepted decision records define no outcome for this case (see record 0010)."""


@dataclass(frozen=True)
class ActionView:
    """One action's records, as a step reads them. `events` are in seq order."""

    action: Action
    grant: Grant | None
    attempts: tuple[Attempt, ...]
    escalation: Escalation | None
    events: tuple[ActionEvent, ...]

    @property
    def last_seq(self) -> int:
        """The seq of the action's latest event."""
        return self.events[-1].seq


@dataclass(frozen=True)
class NewEvent:
    """An event a step asks the store to append. The store assigns its seq and vt."""

    kind: EventKind
    from_state: ActionState | None
    to_state: ActionState
    reason: Reason | Note
    details: Mapping[str, object] = NO_DETAILS


@dataclass(frozen=True)
class Change:
    """Records to write in one transaction: the action, its events, and any other record."""

    action: Action
    events: tuple[NewEvent, ...]
    grant: Grant | None = None
    attempt: Attempt | None = None
    escalation: Escalation | None = None


class Match(StrEnum):
    """The outcome of the matching rule (0001, item 10)."""

    CREATE = "create"
    ATTACH = "attach"
    BUSY = "busy"
    DONE = "done"


# Ids -----------------------------------------------------------------------------------


def action_number(action_id: str) -> int:
    """Return the counter of an action id such as `A-0007`."""
    prefix, _, number = action_id.partition("-")
    if prefix != "A" or not number.isdigit():
        raise ValueError(f"not an action id: {action_id!r}")
    return int(number)


def action_id_for(number: int) -> str:
    """Return the action id for the run's `number`-th action."""
    return f"A-{number:04d}"


def grant_id_for(action_id: str) -> str:
    """Return the id of the action's grant."""
    return f"G-{action_number(action_id):04d}"


def escalation_id_for(action_id: str) -> str:
    """Return the id of the action's escalation."""
    return f"ESC-{action_number(action_id):04d}"


def attempt_id_for(action_id: str, attempt_no: int) -> str:
    """Return the id of the action's attempt `attempt_no`."""
    return f"{action_id}/{attempt_no}"


# Helpers -------------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StepRefused(message)


def _move(
    view: ActionView,
    target: ActionState,
    reason: Reason,
    vt: int,
    details: Mapping[str, object] = NO_DETAILS,
) -> tuple[Action, NewEvent]:
    action = view.action
    check_transition(action.state, target, reason, action.contract)
    event = NewEvent(EventKind.TRANSITION, action.state, target, reason, frozen(details))
    return replace(action, state=target, updated_vt=vt), event


def _note(action: Action, reason: Note, details: Mapping[str, object]) -> NewEvent:
    return NewEvent(EventKind.NOTE, action.state, action.state, reason, frozen(details))


def _grant_to(grant: Grant, target: GrantState, *, consumed_vt: int | None = None) -> Grant:
    check_grant_transition(grant.state, target)
    if consumed_vt is None:
        return replace(grant, state=target)
    return replace(grant, state=target, consumed_vt=consumed_vt)


def _grant_in(view: ActionView, *states: GrantState) -> Grant:
    grant = view.grant
    if grant is None or grant.state not in states:
        found = None if grant is None else grant.state
        raise StepRefused(f"{view.action.action_id} needs a grant in {states}, found {found}")
    return grant


def _full_basis(basis: BasisCheck) -> BasisCheck:
    if basis.sufficient_ok is None:
        raise ValueError("this step checks every dependency rule, the sufficient ones too")
    return basis


def _escalate(
    action: Action, trigger: EscalationTrigger, vt: int, details: Mapping[str, object]
) -> tuple[Escalation, NewEvent]:
    escalation_id = escalation_id_for(action.action_id)
    escalation = Escalation(escalation_id, action.action_id, vt, None, None, None, None)
    note = _note(
        action, Note.ESCALATED, {"escalation_id": escalation_id, "trigger": trigger, **details}
    )
    return escalation, note


def _trigger_on_unknown(
    contract: Contract, attempts_sent: int, settings: GatewaySettings
) -> EscalationTrigger | None:
    """When entering OutcomeUnknown escalates at once (0003, escalation; 0005)."""
    if not contract.capabilities:
        return EscalationTrigger.AT_ONCE
    if contract.has(Capability.IDEMPOTENCY_KEY) and attempts_sent >= settings.max_attempts:
        return EscalationTrigger.MAX_ATTEMPTS
    return None


def _enter_unknown(
    view: ActionView,
    reason: Reason,
    vt: int,
    settings: GatewaySettings,
    attempt: Attempt | None,
) -> Change:
    attempt_no = len(view.attempts)
    action, event = _move(view, _S.OUTCOME_UNKNOWN, reason, vt, {"attempt_no": attempt_no})
    trigger = _trigger_on_unknown(action.contract, attempt_no, settings)
    if trigger is None:
        return Change(action, (event,), attempt=attempt)
    escalation, note = _escalate(action, trigger, vt, NO_DETAILS)
    return Change(action, (event, note), attempt=attempt, escalation=escalation)


def _latest_sent_vt(view: ActionView) -> int:
    if not view.attempts:
        raise StepRefused(f"{view.action.action_id} has no attempt")
    return max(attempt.sent_vt for attempt in view.attempts)


def _retry_allowed(view: ActionView, settings: GatewaySettings) -> int:
    """Check the conditions for any retry (I4, 0005) and return the next attempt number."""
    action = view.action
    _require(action.state is _S.OUTCOME_UNKNOWN, f"{action.action_id} is {action.state}")
    _require(
        action.contract.has(Capability.IDEMPOTENCY_KEY),
        f"{action.contract} allows no retries (I4)",
    )
    _require(view.escalation is None, f"{action.action_id} is escalated; a human resolves it")
    attempt_no = len(view.attempts) + 1
    _require(attempt_no <= settings.max_attempts, f"{action.action_id} used max_attempts")
    _grant_in(view, GrantState.CONSUMED)
    return attempt_no


# Admission and matching (0001) -------------------------------------------------------


def match_proposal(holders: Sequence[Action], proposal: Proposal) -> tuple[Match, Action | None]:
    """Apply the matching rule to the intent's live or Confirmed actions (0001, item 10)."""
    if len(holders) > 1:
        ids = ", ".join(holder.action_id for holder in holders)
        raise AssertionError(f"I7 broken: {proposal.intent_id} has live or Confirmed {ids}")
    if not holders:
        return Match.CREATE, None
    holder = holders[0]
    if holder.state not in HOLDING_STATES:
        raise ValueError(f"{holder.action_id} is {holder.state}, so it holds no intent")
    same = holder.action_key == proposal.action_key
    if holder.state is _S.CONFIRMED:
        return (Match.ATTACH if same else Match.DONE), holder
    return (Match.ATTACH if same else Match.BUSY), holder


def admit(
    action_id: str,
    task_id: str,
    proposal: Proposal,
    contract: Contract,
    *,
    identity: bool,
    vt: int,
) -> Change:
    """T1: create an action from an admitted proposal (0001, 0003)."""
    check_transition(None, _S.PROPOSED, Reason.ADMITTED, contract)
    action = Action(
        action_id=action_id,
        task_id=task_id,
        intent_id=proposal.intent_id,
        operation=proposal.operation,
        target=proposal.target,
        args=frozen(proposal.args),
        action_key=proposal.action_key,
        identity=identity,
        contract=contract,
        basis=frozen(proposal.basis),
        state=_S.PROPOSED,
        grant_id=None,
        idempotency_key=action_id,
        effect_id=None,
        created_vt=vt,
        updated_vt=vt,
    )
    event = NewEvent(EventKind.TRANSITION, None, _S.PROPOSED, Reason.ADMITTED)
    return Change(action, (event,))


def attach(view: ActionView, proposal: Proposal) -> Change | None:
    """Take a newer basis from an attaching proposal, before the first send (0001, item 16).

    The basis is replaced only if every cited version is at least as new as the one held.
    After the first dispatch the basis is frozen. The note makes any dispatch check that
    began against the old basis stale (I3).
    """
    action = view.action
    if action.action_key != proposal.action_key:
        raise ValueError("only a proposal with the same action key attaches")
    if action.state not in UNDISPATCHED_STATES:
        return None
    held, cited = dict(action.basis), dict(proposal.basis)
    if cited == held or cited.keys() != held.keys():
        return None
    if any(cited[key] < held[key] for key in cited):
        return None
    note = _note(action, Note.BASIS_UPDATED, {"from": frozen(held), "to": frozen(cited)})
    return Change(replace(action, basis=frozen(cited)), (note,))


# Approval (0002) ---------------------------------------------------------------------


def file_approval(
    view: ActionView, basis: BasisCheck, approval_id: str | None, vt: int, *, seen_seq: int
) -> Change | None:
    """T2 or T3: check the basis; if fresh, record the filed approval request as a grant.

    The gateway files the request (keyed by the action id) only after the basis passed,
    so a stale basis comes with no approval id. `seen_seq` is the seq of the action's
    latest event when the gateway began its check, which reads facts over the network
    and so runs without a lock. If the action has a newer event, such as a newer basis
    from an attaching proposal, nothing is written (0003, dispatch).
    """
    if seen_seq != view.last_seq:
        return None
    action = view.action
    _require(action.state is _S.PROPOSED, f"{action.action_id} is {action.state}")
    _require(view.grant is None, f"{action.action_id} already has a grant")
    if not _full_basis(basis).fresh:
        if approval_id is not None:
            raise ValueError("no approval request is filed for a stale basis")
        rejected, event = _move(view, _S.REJECTED, Reason.STALE_INPUT, vt, {"stale": basis.stale})
        return Change(rejected, (event,))
    if approval_id is None:
        raise ValueError("a fresh basis comes with the filed approval request's id")
    check_grant_transition(None, GrantState.PENDING)
    grant = Grant(
        grant_id_for(action.action_id), action.action_id, approval_id, GrantState.PENDING, vt, None
    )
    pending, event = _move(
        view, _S.PENDING_APPROVAL, Reason.APPROVAL_REQUESTED, vt, {"approval_id": approval_id}
    )
    return Change(replace(pending, grant_id=grant.grant_id), (event,), grant=grant)


def poll_approval(
    view: ActionView,
    approval: ApprovalStatus,
    basis: BasisCheck | None,
    vt: int,
    *,
    seen_seq: int,
) -> Change | None:
    """T4 or T5 from what the approvals service reports while the action waits (0002).

    `basis` is the basis check made with this poll. It is optional while the approval is
    pending and required once it is approved (T4 needs a fresh basis). A denial is named
    before a stale basis. Nothing is written if the action has an event newer than
    `seen_seq`, as in `file_approval`.
    """
    if seen_seq != view.last_seq:
        return None
    action = view.action
    _require(action.state is _S.PENDING_APPROVAL, f"{action.action_id} is {action.state}")
    grant = _grant_in(view, GrantState.PENDING)
    if basis is not None:
        _full_basis(basis)
    if approval is ApprovalStatus.DENIED:
        rejected, event = _move(view, _S.REJECTED, Reason.APPROVAL_DENIED, vt)
        return Change(rejected, (event,), grant=_grant_to(grant, GrantState.DENIED))
    if approval in (ApprovalStatus.REVOKED, ApprovalStatus.EXPIRED):
        raise UnspecifiedCase(
            f"the first poll for {action.action_id} found the approval {approval}: record "
            "0002 moves the grant to revoked or expired, but T5 in record 0003 has no reason "
            "for it (proposed record 0010)"
        )
    stale = {"stale": basis.stale} if basis is not None else NO_DETAILS
    if approval is ApprovalStatus.PENDING:
        if basis is None or basis.fresh:
            return None
        rejected, event = _move(view, _S.REJECTED, Reason.STALE_INPUT, vt, stale)
        return Change(rejected, (event,), grant=_grant_to(grant, GrantState.RELEASED))
    if basis is None:
        raise ValueError("an approved request is acted on only with a basis check (T4)")
    active = _grant_to(grant, GrantState.ACTIVE)
    if basis.fresh:
        authorized, event = _move(view, _S.AUTHORIZED, Reason.APPROVED, vt)
        return Change(authorized, (event,), grant=active)
    rejected, event = _move(view, _S.REJECTED, Reason.STALE_INPUT, vt, stale)
    return Change(rejected, (event,), grant=_grant_to(active, GrantState.RELEASED))


def time_out_approval(
    view: ActionView,
    approval: ApprovalStatus,
    vt: int,
    settings: GatewaySettings,
    *,
    seen_seq: int,
) -> Change | None:
    """T5 when no decision came within the scenario's `approval_wait_ms` (0002, rule 14).

    "No decision" is checked, not assumed: `approval` is what a final poll found, and it
    must still be pending. A decision that arrived first is acted on by `poll_approval`.
    Nothing is written if the action has an event newer than `seen_seq`.
    """
    if seen_seq != view.last_seq:
        return None
    action = view.action
    _require(action.state is _S.PENDING_APPROVAL, f"{action.action_id} is {action.state}")
    grant = _grant_in(view, GrantState.PENDING)
    _require(
        approval is ApprovalStatus.PENDING,
        f"{action.action_id}: the approval is {approval}; act on the decision instead",
    )
    _require(
        vt >= grant.requested_vt + settings.approval_wait_ms,
        f"{action.action_id}: approval_wait_ms has not passed",
    )
    rejected, event = _move(view, _S.REJECTED, Reason.APPROVAL_TIMEOUT, vt)
    return Change(rejected, (event,), grant=_grant_to(grant, GrantState.TIMED_OUT))


# Dispatch and retries (0002, 0003, 0005) ------------------------------------------------


def begin_check(view: ActionView, settings: GatewaySettings) -> CheckTicket:
    """Start a dispatch check: say which check is due and from which event (0003, dispatch).

    The dispatcher runs the check without a lock, then calls `dispatch` or `retry`, which
    commit only if the action has no event newer than `seen_seq`.
    """
    action = view.action
    if action.state is _S.AUTHORIZED:
        _grant_in(view, GrantState.ACTIVE)
        _require(not view.attempts, f"{action.action_id} is Authorized but has attempts")
        return CheckTicket(action.action_id, 1, CheckKind.FULL, view.last_seq, action.updated_vt)
    attempt_no = _retry_allowed(view, settings)
    due = _latest_sent_vt(view) + settings.backoff_ms(len(view.attempts))
    kind = required_check(action.contract, attempt_no)
    return CheckTicket(action.action_id, attempt_no, kind, view.last_seq, due)


def _new_attempt(view: ActionView, check: CheckRecord, vt: int) -> Attempt:
    action = view.action
    attempt_no = len(view.attempts) + 1
    grant_id = action.grant_id
    if grant_id is None:
        raise StepRefused(f"{action.action_id} has no grant")
    return Attempt(
        attempt_id=attempt_id_for(action.action_id, attempt_no),
        action_id=action.action_id,
        attempt_no=attempt_no,
        idempotency_key=action.idempotency_key,
        grant_id=grant_id,
        check=check,
        sent_vt=vt,
        answer_class=None,
        answer_ref=None,
        answered_vt=None,
    )


def dispatch(view: ActionView, check: CheckRecord, vt: int) -> Change | None:
    """T6 or T7: commit the first send, or reject, after a full check (I3; 0002, rule 17-18).

    Returns None, committing nothing, if the action has a newer event than the check saw.
    """
    if check.seen_seq != view.last_seq:
        return None
    action = view.action
    _require(action.state is _S.AUTHORIZED, f"{action.action_id} is {action.state}")
    _require(check.kind is CheckKind.FULL, "the first send needs a full check (I3)")
    _require(check.check_vt <= vt, "the check must come before the send")
    grant = _grant_in(view, GrantState.ACTIVE)
    if check.approval is ApprovalStatus.REVOKED:
        rejected, event = _move(view, _S.REJECTED, Reason.APPROVAL_REVOKED, vt)
        return Change(rejected, (event,), grant=_grant_to(grant, GrantState.REVOKED))
    if check.approval is ApprovalStatus.EXPIRED:
        rejected, event = _move(view, _S.REJECTED, Reason.APPROVAL_EXPIRED, vt)
        return Change(rejected, (event,), grant=_grant_to(grant, GrantState.EXPIRED))
    if check.approval is not ApprovalStatus.APPROVED or check.basis is None:
        raise ValueError(f"an active grant's approval cannot be {check.approval}")
    if not check.passed:
        stale = {"stale": check.basis.stale}
        rejected, event = _move(view, _S.REJECTED, Reason.STALE_INPUT, vt, stale)
        return Change(rejected, (event,), grant=_grant_to(grant, GrantState.RELEASED))
    sent, event = _move(view, _S.DISPATCHED, Reason.DISPATCHED, vt, {"attempt_no": 1})
    consumed = _grant_to(grant, GrantState.CONSUMED, consumed_vt=vt)
    return Change(sent, (event,), grant=consumed, attempt=_new_attempt(view, check, vt))


def retry(
    view: ActionView, check: CheckRecord, vt: int, settings: GatewaySettings
) -> Change | None:
    """T13 with the same idempotency key, or escalation if the retry check fails (0005).

    At C2 the retry check covers authority and the exact rules; if it fails, nothing is
    sent and the action is escalated (0002, rule 19). C3 retries are exempt (rule 20).
    Returns None, committing nothing, if the action has a newer event than the check saw.
    """
    if check.seen_seq != view.last_seq:
        return None
    action = view.action
    attempt_no = _retry_allowed(view, settings)
    _require(
        check.kind is required_check(action.contract, attempt_no),
        f"attempt {attempt_no} at {action.contract} needs a different check (I3)",
    )
    due = _latest_sent_vt(view) + settings.backoff_ms(len(view.attempts))
    _require(vt >= due, f"{action.action_id}: the retry backoff has not passed")
    _require(check.check_vt <= vt, "the check must come before the send")
    grant = _grant_in(view, GrantState.CONSUMED)
    if check.kind is CheckKind.EXACT_ONLY and not check.passed:
        observed: Grant | None = None
        if check.approval is ApprovalStatus.REVOKED:
            observed = _grant_to(grant, GrantState.REVOKED)
        elif check.approval is ApprovalStatus.EXPIRED:
            observed = _grant_to(grant, GrantState.EXPIRED)
        elif check.approval is not ApprovalStatus.APPROVED:
            raise ValueError(f"a consumed grant's approval cannot be {check.approval}")
        stale = check.basis.stale if check.basis is not None else ()
        escalation, note = _escalate(
            action,
            EscalationTrigger.RETRY_CHECK_FAILED,
            vt,
            {"approval": check.approval, "stale": stale},
        )
        return Change(action, (note,), grant=observed, escalation=escalation)
    sent, event = _move(view, _S.DISPATCHED, Reason.RETRY, vt, {"attempt_no": attempt_no})
    return Change(sent, (event,), attempt=_new_attempt(view, check, vt))


def answer(
    view: ActionView, attempt_no: int, result: Answer, vt: int, settings: GatewaySettings
) -> Change:
    """T8, T9 or T10 from the destination's answer to the open attempt (0003).

    The transport drops answers to timed-out requests (0006), so answers arrive only for
    the latest attempt while the action is Dispatched. A timeout is the gateway's own
    answer, due `dispatch_timeout_ms` after the send.
    """
    action = view.action
    _require(action.state is _S.DISPATCHED, f"{action.action_id} is {action.state}")
    attempt = view.attempts[-1]
    _require(attempt_no == attempt.attempt_no, "answers arrive only for the latest attempt")
    _require(attempt.answer_class is None, f"attempt {attempt_no} already has an answer")
    if result.reason is Reason.TIMEOUT:
        _require(
            vt >= attempt.sent_vt + settings.dispatch_timeout_ms,
            "a timeout comes dispatch_timeout_ms after the send",
        )
    answered = replace(
        attempt, answer_class=result.answer_class, answer_ref=result.effect_id, answered_vt=vt
    )
    if result.answer_class is AnswerClass.COMMITTED:
        effect_id = result.effect_id
        details = {"attempt_no": attempt_no, "effect_id": effect_id}
        confirmed, event = _move(view, _S.CONFIRMED, Reason.COMMITTED, vt, details)
        return Change(replace(confirmed, effect_id=effect_id), (event,), attempt=answered)
    if result.answer_class is AnswerClass.REJECTED:
        failed, event = _move(view, _S.FAILED, result.reason, vt, {"attempt_no": attempt_no})
        return Change(failed, (event,), attempt=answered)
    return _enter_unknown(view, result.reason, vt, settings, answered)


def recover(view: ActionView, vt: int, settings: GatewaySettings) -> Change | None:
    """Startup recovery, step 1: an open attempt moves to OutcomeUnknown (0003).

    Other states need no change: OutcomeUnknown resumes its recovery, Authorized
    actions return to the outbox, PendingApproval resumes polling, Proposed resumes its
    basis check. The open attempt keeps no answer, because none arrived.
    """
    if view.action.state is not _S.DISPATCHED:
        return None
    if view.attempts[-1].answer_class is not None:
        raise AssertionError(f"{view.action.action_id} is Dispatched with an answered attempt")
    return _enter_unknown(view, Reason.RECOVERED_OPEN_ATTEMPT, vt, settings, None)


# Settling an unknown outcome (0003, 0004, 0005) --------------------------------------


def probe(view: ActionView, result: ProbeAnswer, vt: int, settings: GatewaySettings) -> Change:
    """T11, T12 or a probe note from a C1 status query (0005).

    A listed effect confirms the action. An empty answer fails it only when the answer is
    accurate to at least S after the send (`as_of_vt >= sent_vt + S`), the same rule the
    evaluator uses (0004). Otherwise the probe is noted, and the `max_probes`-th note
    escalates.
    """
    action = view.action
    _require(action.state is _S.OUTCOME_UNKNOWN, f"{action.action_id} is {action.state}")
    _require(action.contract.has(Capability.STATUS), f"{action.contract} has no status lookup")
    _require(view.escalation is None, f"{action.action_id} is escalated; a human resolves it")
    if result.as_of_vt is not None and result.as_of_vt > vt:
        raise ValueError("a status answer cannot be accurate to a later time than now")
    sent_vt = _latest_sent_vt(view)
    if result.effect_ids:
        effect_id = result.effect_ids[0]
        details = {"effect_id": effect_id, "listed": result.effect_ids, "as_of_vt": result.as_of_vt}
        confirmed, event = _move(view, _S.CONFIRMED, Reason.STATUS_FOUND, vt, details)
        return Change(replace(confirmed, effect_id=effect_id), (event,))
    if result.as_of_vt is not None and result.as_of_vt >= sent_vt + settings.settle_ms:
        details = {"as_of_vt": result.as_of_vt}
        failed, event = _move(view, _S.FAILED, Reason.STATUS_SETTLED_EMPTY, vt, details)
        return Change(failed, (event,))
    probe_no = 1 + sum(1 for event in view.events if event.reason is Note.PROBE)
    note = _note(
        action,
        Note.PROBE,
        {"probe_no": probe_no, "answered": result.answered, "as_of_vt": result.as_of_vt},
    )
    if probe_no < settings.max_probes:
        return Change(action, (note,))
    escalation, escalated = _escalate(action, EscalationTrigger.MAX_PROBES, vt, NO_DETAILS)
    return Change(action, (note, escalated), escalation=escalation)


def attest(
    view: ActionView,
    resolution: Resolution,
    effect_id: str | None,
    actor: str,
    vt: int,
    settings: GatewaySettings,
) -> Change:
    """T11 or T12 with reason human_attested, resolving the open escalation (0003).

    An attestation that nothing committed is accepted only at or after S past the latest
    send, because only then does it settle every attempt in the evaluator's evidence
    rules (0004), which the gateway applies too.
    """
    action = view.action
    _require(action.state is _S.OUTCOME_UNKNOWN, f"{action.action_id} is {action.state}")
    escalation = view.escalation
    if escalation is None or escalation.resolved_vt is not None:
        raise StepRefused(f"{action.action_id} has no open escalation")
    if resolution is Resolution.COMMITTED:
        if effect_id is None:
            raise ValueError("an attestation of a commit names the effect")
        details = {"effect_id": effect_id, "actor": actor}
        moved, event = _move(view, _S.CONFIRMED, Reason.HUMAN_ATTESTED, vt, details)
        moved = replace(moved, effect_id=effect_id)
    else:
        if effect_id is not None:
            raise ValueError("an attestation that nothing committed names no effect")
        _require(
            vt >= _latest_sent_vt(view) + settings.settle_ms,
            "nothing can be attested as not committed before S has passed since the last send",
        )
        moved, event = _move(view, _S.FAILED, Reason.HUMAN_ATTESTED, vt, {"actor": actor})
    resolved = replace(
        escalation, resolved_vt=vt, resolution=resolution, effect_id=effect_id, actor=actor
    )
    return Change(moved, (event,), escalation=resolved)


# Closing the run and reporting (0003) ------------------------------------------------


def close(view: ActionView, vt: int) -> Change | None:
    """Reject an action that was never dispatched when the run closes (0003)."""
    action = view.action
    if action.state is _S.PROPOSED:
        rejected, event = _move(view, _S.REJECTED, Reason.RUN_CLOSED, vt)
        return Change(rejected, (event,))
    if action.state is _S.PENDING_APPROVAL:
        grant = _grant_in(view, GrantState.PENDING)
        rejected, event = _move(view, _S.REJECTED, Reason.RUN_CLOSED, vt)
        return Change(rejected, (event,), grant=_grant_to(grant, GrantState.RELEASED))
    if action.state is _S.AUTHORIZED:
        grant = _grant_in(view, GrantState.ACTIVE)
        rejected, event = _move(view, _S.REJECTED, Reason.RUN_CLOSED, vt)
        return Change(rejected, (event,), grant=_grant_to(grant, GrantState.RELEASED))
    return None


def result_for(view: ActionView) -> ActionResult:
    """The agent-facing result for the action as it is now (0003, what the agent is told)."""
    action = view.action
    last = next(e for e in reversed(view.events) if e.kind is EventKind.TRANSITION)
    if not isinstance(last.reason, Reason):
        raise AssertionError(f"transition {last.seq} has a note reason")
    stale: tuple[StaleFact, ...] = ()
    if action.state is _S.REJECTED and last.reason is Reason.STALE_INPUT:
        found = last.details.get("stale", ())
        if isinstance(found, tuple):
            stale = tuple(item for item in found if isinstance(item, StaleFact))
    reconcile = None
    if action.state is _S.OUTCOME_UNKNOWN:
        escalation = view.escalation
        escalated = escalation is not None and escalation.resolved_vt is None
        reconcile = Reconcile(
            action.contract,
            reconcile_how(action.contract, escalated=escalated),
            escalation.escalation_id if escalation is not None else None,
            f"The {action.operation} for {action.intent_id} at {action.target} "
            "may or may not have taken effect.",
        )
    effect_id = action.effect_id if action.state is _S.CONFIRMED else None
    return ActionResult(
        action.action_id, action.intent_id, action.state, effect_id, last.reason, stale, reconcile
    )


def known_effects(view: ActionView) -> tuple[str, ...]:
    """Every effect id the gateway learned for the action, in the order it learned them.

    That is the effect id the action records (I1) and every other id a status answer
    listed. A C1 status answer can list two when the transport delivered the request
    twice (fault F4), and the evaluator counts both as known (0004, property 5).
    """
    found: list[str] = []
    if view.action.effect_id is not None:
        found.append(view.action.effect_id)
    for event in view.events:
        listed = event.details.get("listed", ())
        if not isinstance(listed, tuple):
            continue
        for effect_id in listed:
            if isinstance(effect_id, str) and effect_id not in found:
                found.append(effect_id)
    return tuple(found)


def report_conflicts(
    claims: Sequence[Claim],
    intents: Collection[str],
    views_by_intent: Mapping[str, Sequence[ActionView]],
) -> tuple[ClaimConflict, ...]:
    """Return every claim that contradicts the gateway's records (0003, final report).

    `done` needs a Confirmed action and exactly the effect ids the gateway recorded for
    the intent, which include every effect a status answer listed (`known_effects`).
    `not_done` needs every action to be Rejected, Failed or not yet dispatched.
    `unknown` needs an attempt without a settled answer: an action in Dispatched or
    OutcomeUnknown.
    """
    claimed = [claim.intent_id for claim in claims]
    if sorted(claimed) != sorted(intents):
        raise ValueError("a report has exactly one claim per task intent")
    conflicts: list[ClaimConflict] = []
    for claim in claims:
        views = views_by_intent.get(claim.intent_id, ())
        states = {view.action.state for view in views}
        recorded = sorted({effect for view in views for effect in known_effects(view)})
        if claim.status is ClaimStatus.DONE:
            ok = _S.CONFIRMED in states and sorted(claim.effect_ids) == recorded
        elif claim.status is ClaimStatus.NOT_DONE:
            ok = states <= (UNDISPATCHED_STATES | {_S.REJECTED, _S.FAILED})
        else:
            ok = bool(states & UNSETTLED_STATES)
        if not ok:
            record = tuple(
                RecordedAction(view.action.action_id, view.action.state, known_effects(view))
                for view in views
            )
            conflicts.append(ClaimConflict(claim.intent_id, claim.status, claim.effect_ids, record))
    return tuple(conflicts)
