"""The gateway's records for one run, held in memory (0003, testing plan).

`MemoryStore` applies the step functions in `steps.py` the way the Postgres store will:
one step per operation, all or nothing. It builds the next snapshot from copies of the
touched records, checks every invariant on it, and only then makes it current. A step
that raises, or a result that breaks an invariant, leaves the store unchanged.

It serves two purposes. The in-memory Hypothesis suite tests the steps through it, and
the Postgres suite will use it as the oracle: the same operations against both stores
must produce the same records.

It also logs every agent-facing response, as the gateway's `tool_call` table does, so
I5 can be checked. It does not wait: `tool_wait_ms` and `get_action_status(wait_ms)`
belong to the tool layer, which calls `status` when its wait ends, with the name of the
tool that answers. A refused proposal is answered at once, so `propose` logs refusals;
an admitted one is answered later, by that `status` call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from fivefold.domain.contracts import Contract
from fivefold.gateway import steps
from fivefold.gateway.invariants import InvariantViolation, check_invariants
from fivefold.gateway.records import (
    ActionEvent,
    ActionResult,
    Answer,
    ApprovalStatus,
    Attempt,
    BasisCheck,
    CheckRecord,
    CheckTicket,
    Claim,
    ProbeAnswer,
    Proposal,
    Refusal,
    RefusalReason,
    ReportAccepted,
    Resolution,
    Snapshot,
    ToolCall,
    frozen,
)
from fivefold.gateway.state_machine import HOLDING_STATES, GatewaySettings
from fivefold.gateway.steps import ActionView, Change, Match

StepFn = Callable[[ActionView], Change | None]


class UnknownAction(LookupError):
    """No action has this id in the run."""


class MemoryStore:
    """One run's gateway records with every step applied atomically.

    `identity` is on for G2. With it off (G1), every proposal creates a new action
    (0008); the other G1 differences belong to the gateway, not to the records.
    """

    def __init__(
        self,
        *,
        task_id: str,
        intents: Mapping[str, str],
        contract: Contract,
        settings: GatewaySettings,
        identity: bool = True,
    ) -> None:
        self._task_id = task_id
        self._contract = contract
        self._settings = settings
        self._identity = identity
        self._vt = 0
        self._snapshot = Snapshot(
            intents=frozen(intents),
            actions=frozen({}),
            grants=frozen({}),
            attempts=frozen({}),
            events=(),
            escalations=frozen({}),
            tool_calls=(),
            run_closed=False,
        )

    # Reading ---------------------------------------------------------------------------

    @property
    def vt(self) -> int:
        """The virtual time of the latest operation."""
        return self._vt

    @property
    def settings(self) -> GatewaySettings:
        """The settings every step runs with."""
        return self._settings

    def snapshot(self) -> Snapshot:
        """Every record as it is now. Snapshots are immutable and never change."""
        return self._snapshot

    def view(self, action_id: str) -> ActionView:
        """One action's records, as steps read them."""
        return _view(self._snapshot, action_id)

    def begin_check(self, action_id: str) -> CheckTicket:
        """What the dispatcher must check before the next send (0003, dispatch)."""
        return steps.begin_check(self.view(action_id), self._settings)

    # Tools ------------------------------------------------------------------------------

    def propose(self, proposal: Proposal, vt: int) -> ActionResult | Refusal:
        """A write tool call: admission's intent check, then the matching rule (0001).

        Returns the action's result as admission left it, or a refusal. Only a refusal is
        logged as the tool's response; see the module docstring.
        """
        self._clock(vt)
        snapshot = self._snapshot
        outcome: ActionResult | Refusal
        action_id: str | None = None
        operation = snapshot.intents.get(proposal.intent_id)
        if snapshot.run_closed:
            outcome = Refusal(RefusalReason.RUN_CLOSED)
        elif operation is None:
            outcome = Refusal(RefusalReason.UNKNOWN_INTENT)
        elif operation != proposal.operation:
            outcome = Refusal(RefusalReason.WRONG_OPERATION)
        else:
            holders = [
                a
                for a in snapshot.actions.values()
                if self._identity
                and a.identity
                and a.intent_id == proposal.intent_id
                and a.state in HOLDING_STATES
            ]
            kind, holder = steps.match_proposal(holders, proposal)
            if holder is None:
                action_id = steps.action_id_for(len(snapshot.actions) + 1)
                change = steps.admit(
                    action_id,
                    self._task_id,
                    proposal,
                    self._contract,
                    identity=self._identity,
                    vt=vt,
                )
                snapshot = _apply(snapshot, change, vt)
                outcome = steps.result_for(_view(snapshot, action_id))
            elif kind is Match.ATTACH:
                action_id = holder.action_id
                snapshot = _apply(snapshot, steps.attach(_view(snapshot, action_id), proposal), vt)
                outcome = steps.result_for(_view(snapshot, action_id))
            elif kind is Match.BUSY:
                action_id = holder.action_id
                outcome = Refusal(
                    RefusalReason.INTENT_BUSY, action_id, holder.state, args=holder.args
                )
            else:
                action_id = holder.action_id
                outcome = Refusal(
                    RefusalReason.INTENT_DONE, action_id, holder.state, holder.effect_id
                )
        if isinstance(outcome, Refusal):
            snapshot = _log(snapshot, proposal.operation, action_id, outcome, vt)
        self._commit(snapshot, vt)
        return outcome

    def status(self, action_id: str, vt: int, *, tool: str = "get_action_status") -> ActionResult:
        """The action's result now, as `tool` answers once its wait ends (0003); logged."""
        self._clock(vt)
        result = steps.result_for(self.view(action_id))
        self._commit(_log(self._snapshot, tool, action_id, result, vt), vt)
        return result

    def submit_report(self, claims: Sequence[Claim], vt: int) -> ReportAccepted | Refusal:
        """Check a final report against the records (0003); refuse any contradiction."""
        self._clock(vt)
        snapshot = self._snapshot
        by_intent: dict[str, list[ActionView]] = {}
        for action in snapshot.actions.values():
            by_intent.setdefault(action.intent_id, []).append(_view(snapshot, action.action_id))
        conflicts = steps.report_conflicts(claims, list(snapshot.intents), by_intent)
        outcome: ReportAccepted | Refusal
        if conflicts:
            outcome = Refusal(RefusalReason.REPORT_INCONSISTENT, conflicts=conflicts)
        else:
            outcome = ReportAccepted(tuple(claims))
        self._commit(_log(snapshot, "submit_final_report", None, outcome, vt), vt)
        return outcome

    # Gateway steps ----------------------------------------------------------------------

    def file_approval(
        self,
        action_id: str,
        basis: BasisCheck,
        approval_id: str | None,
        vt: int,
        *,
        seen_seq: int,
    ) -> None:
        """T2 or T3 (0002, 0003). `seen_seq`: the action's latest event when the check began."""
        self._step(
            action_id,
            vt,
            lambda v: steps.file_approval(v, basis, approval_id, vt, seen_seq=seen_seq),
        )

    def poll_approval(
        self,
        action_id: str,
        approval: ApprovalStatus,
        basis: BasisCheck | None,
        vt: int,
        *,
        seen_seq: int,
    ) -> None:
        """T4 or T5, or nothing while the request is pending and the basis holds."""
        self._step(
            action_id,
            vt,
            lambda v: steps.poll_approval(v, approval, basis, vt, seen_seq=seen_seq),
        )

    def time_out_approval(
        self, action_id: str, approval: ApprovalStatus, vt: int, *, seen_seq: int
    ) -> None:
        """T5 with reason approval_timeout, after a final poll that still reads pending."""
        self._step(
            action_id,
            vt,
            lambda v: steps.time_out_approval(v, approval, vt, self._settings, seen_seq=seen_seq),
        )

    def dispatch(self, action_id: str, check: CheckRecord, vt: int) -> Attempt | None:
        """T6 or T7. Returns the attempt to send, or None if nothing may be sent."""
        change = self._step(action_id, vt, lambda v: steps.dispatch(v, check, vt))
        return change.attempt if change is not None else None

    def retry(self, action_id: str, check: CheckRecord, vt: int) -> Attempt | None:
        """T13 or an escalation. Returns the attempt to send, or None if nothing may be sent."""
        change = self._step(action_id, vt, lambda v: steps.retry(v, check, vt, self._settings))
        return change.attempt if change is not None else None

    def answer(self, action_id: str, attempt_no: int, answer: Answer, vt: int) -> None:
        """T8, T9 or T10."""
        self._step(action_id, vt, lambda v: steps.answer(v, attempt_no, answer, vt, self._settings))

    def probe(self, action_id: str, result: ProbeAnswer, vt: int) -> None:
        """T11, T12 or a probe note, at C1."""
        self._step(action_id, vt, lambda v: steps.probe(v, result, vt, self._settings))

    def attest(
        self, action_id: str, resolution: Resolution, effect_id: str | None, actor: str, vt: int
    ) -> None:
        """T11 or T12 with reason human_attested, through the admin API."""
        self._step(
            action_id,
            vt,
            lambda v: steps.attest(v, resolution, effect_id, actor, vt, self._settings),
        )

    def restart(self, vt: int) -> None:
        """Startup recovery in action-id order, before any tool is served (0003)."""
        self._each_action(vt, lambda v: steps.recover(v, vt, self._settings))

    def close_run(self, vt: int) -> None:
        """Refuse new proposals and reject every action not yet dispatched (0003)."""
        self._clock(vt)
        closed = replace(self._snapshot, run_closed=True)
        self._commit(_each(closed, vt, lambda v: steps.close(v, vt)), vt)

    # Internals --------------------------------------------------------------------------

    def _clock(self, vt: int) -> None:
        if vt < self._vt:
            raise ValueError(f"virtual time went backwards: {vt} after {self._vt}")

    def _step(self, action_id: str, vt: int, step: StepFn) -> Change | None:
        self._clock(vt)
        change = step(self.view(action_id))
        if change is not None:
            self._commit(_apply(self._snapshot, change, vt), vt)
        else:
            self._vt = vt
        return change

    def _each_action(self, vt: int, step: StepFn) -> None:
        self._clock(vt)
        self._commit(_each(self._snapshot, vt, step), vt)

    def _commit(self, snapshot: Snapshot, vt: int) -> None:
        violations = check_invariants(snapshot, self._settings)
        if violations:
            raise InvariantViolation(violations)
        self._snapshot = snapshot
        self._vt = vt


def _view(snapshot: Snapshot, action_id: str) -> ActionView:
    action = snapshot.actions.get(action_id)
    if action is None:
        raise UnknownAction(action_id)
    grant = snapshot.grants.get(action.grant_id) if action.grant_id is not None else None
    return ActionView(
        action=action,
        grant=grant,
        attempts=snapshot.attempts.get(action_id, ()),
        escalation=snapshot.escalations.get(steps.escalation_id_for(action_id)),
        events=tuple(e for e in snapshot.events if e.action_id == action_id),
    )


def _apply(snapshot: Snapshot, change: Change | None, vt: int) -> Snapshot:
    """Return `snapshot` with `change` written, as one transaction would write it."""
    if change is None:
        return snapshot
    action = change.action
    actions = dict(snapshot.actions)
    actions[action.action_id] = action
    grants = dict(snapshot.grants)
    if change.grant is not None:
        grants[change.grant.grant_id] = change.grant
    attempts = dict(snapshot.attempts)
    if change.attempt is not None:
        attempt = change.attempt
        kept = [t for t in attempts.get(action.action_id, ()) if t.attempt_no != attempt.attempt_no]
        attempts[action.action_id] = tuple(sorted([*kept, attempt], key=lambda t: t.attempt_no))
    escalations = dict(snapshot.escalations)
    if change.escalation is not None:
        escalations[change.escalation.escalation_id] = change.escalation
    seq = _last_seq(snapshot)
    events = list(snapshot.events)
    for new in change.events:
        seq += 1
        events.append(
            ActionEvent(
                seq,
                action.action_id,
                new.kind,
                new.from_state,
                new.to_state,
                new.reason,
                new.details,
                vt,
            )
        )
    return replace(
        snapshot,
        actions=frozen(actions),
        grants=frozen(grants),
        attempts=frozen(attempts),
        events=tuple(events),
        escalations=frozen(escalations),
    )


def _each(snapshot: Snapshot, vt: int, step: StepFn) -> Snapshot:
    """Apply `step` to every action in action-id order, each seeing the ones before."""
    for action_id in sorted(snapshot.actions, key=steps.action_number):
        snapshot = _apply(snapshot, step(_view(snapshot, action_id)), vt)
    return snapshot


def _log(
    snapshot: Snapshot,
    tool: str,
    action_id: str | None,
    result: ActionResult | Refusal | ReportAccepted,
    vt: int,
) -> Snapshot:
    call = ToolCall(len(snapshot.tool_calls) + 1, tool, action_id, result, _last_seq(snapshot), vt)
    return replace(snapshot, tool_calls=(*snapshot.tool_calls, call))


def _last_seq(snapshot: Snapshot) -> int:
    return snapshot.events[-1].seq if snapshot.events else 0
