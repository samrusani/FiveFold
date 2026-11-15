"""The action and grant state machines, as tables (0002, 0003).

This module is the only code that decides whether a transition is legal (0003, testing
plan). It holds no records and does no I/O: the step functions in `steps.py` and every
store call it before they change anything.

Two tables live here:

- `TRANSITIONS`, the action transitions T1 to T13 with their reason codes. A reason can
  also need a capability of the contract (0003, 0005): `in_progress` and `retry` need
  idempotency keys, the `status_*` reasons need the status lookup, and
  `precondition_failed` needs conditional writes.
- `GRANT_TRANSITIONS`, the grant lifecycle from record 0002, rule 14.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from fivefold.domain.contracts import Capability, Contract, ContractParams


class ActionState(StrEnum):
    """The eight action states (0003). Values are what the agent sees (snake_case)."""

    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    AUTHORIZED = "authorized"
    DISPATCHED = "dispatched"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    FAILED = "failed"


FINAL_STATES = frozenset({ActionState.CONFIRMED, ActionState.REJECTED, ActionState.FAILED})
"""States with no outgoing transition."""

LIVE_STATES = frozenset(set(ActionState) - FINAL_STATES)
"""The five states in which an action can still change (0001, item 8)."""

HOLDING_STATES = LIVE_STATES | {ActionState.CONFIRMED}
"""At most one action per intent may be in these states (I7)."""

UNDISPATCHED_STATES = frozenset(
    {ActionState.PROPOSED, ActionState.PENDING_APPROVAL, ActionState.AUTHORIZED}
)
"""Live states before any attempt. Closing the run rejects actions in them (0003)."""

UNSETTLED_STATES = frozenset({ActionState.DISPATCHED, ActionState.OUTCOME_UNKNOWN})
"""States in which an attempt has no settled answer (0003, final report)."""


class Reason(StrEnum):
    """Reason codes of transitions (0003, transition table)."""

    ADMITTED = "admitted"
    APPROVAL_REQUESTED = "approval_requested"
    STALE_INPUT = "stale_input"
    RUN_CLOSED = "run_closed"
    APPROVED = "approved"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_TIMEOUT = "approval_timeout"
    DISPATCHED = "dispatched"
    APPROVAL_REVOKED = "approval_revoked"
    APPROVAL_EXPIRED = "approval_expired"
    COMMITTED = "committed"
    DESTINATION_REJECTED = "destination_rejected"
    PRECONDITION_FAILED = "precondition_failed"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    SERVER_ERROR = "server_error"
    IN_PROGRESS = "in_progress"
    RECOVERED_OPEN_ATTEMPT = "recovered_open_attempt"
    STATUS_FOUND = "status_found"
    HUMAN_ATTESTED = "human_attested"
    STATUS_SETTLED_EMPTY = "status_settled_empty"
    RETRY = "retry"


class Note(StrEnum):
    """Reasons of note events, which record something without changing the state.

    Record 0003 names `escalated`. The other two are this implementation's: `probe`
    records a status query that settled nothing, so the count toward `max_probes`
    survives a restart; `basis_updated` records a newer basis taken from an attaching
    proposal (0001, item 16), so a dispatch check made against the old basis cannot
    commit (I3).
    """

    ESCALATED = "escalated"
    PROBE = "probe"
    BASIS_UPDATED = "basis_updated"


class EscalationTrigger(StrEnum):
    """Why an OutcomeUnknown action was escalated (0003, escalation table)."""

    AT_ONCE = "at_once"
    MAX_PROBES = "max_probes"
    RETRY_CHECK_FAILED = "retry_check_failed"
    MAX_ATTEMPTS = "max_attempts"


class EventKind(StrEnum):
    """Kinds of `action_event` rows (0003, data model)."""

    TRANSITION = "transition"
    NOTE = "note"


@dataclass(frozen=True)
class Transition:
    """One row of the transition table. `source` is None for T1, which creates the action."""

    tid: str
    source: ActionState | None
    target: ActionState
    reasons: frozenset[Reason]


def _t(tid: str, source: ActionState | None, target: ActionState, *reasons: Reason) -> Transition:
    return Transition(tid, source, target, frozenset(reasons))


_S = ActionState
_R = Reason

TRANSITIONS: tuple[Transition, ...] = (
    _t("T1", None, _S.PROPOSED, _R.ADMITTED),
    _t("T2", _S.PROPOSED, _S.PENDING_APPROVAL, _R.APPROVAL_REQUESTED),
    _t("T3", _S.PROPOSED, _S.REJECTED, _R.STALE_INPUT, _R.RUN_CLOSED),
    _t("T4", _S.PENDING_APPROVAL, _S.AUTHORIZED, _R.APPROVED),
    _t(
        "T5",
        _S.PENDING_APPROVAL,
        _S.REJECTED,
        _R.APPROVAL_DENIED,
        _R.APPROVAL_TIMEOUT,
        _R.APPROVAL_REVOKED,
        _R.APPROVAL_EXPIRED,
        _R.STALE_INPUT,
        _R.RUN_CLOSED,
    ),
    _t("T6", _S.AUTHORIZED, _S.DISPATCHED, _R.DISPATCHED),
    _t(
        "T7",
        _S.AUTHORIZED,
        _S.REJECTED,
        _R.APPROVAL_REVOKED,
        _R.APPROVAL_EXPIRED,
        _R.STALE_INPUT,
        _R.RUN_CLOSED,
    ),
    _t("T8", _S.DISPATCHED, _S.CONFIRMED, _R.COMMITTED),
    _t("T9", _S.DISPATCHED, _S.FAILED, _R.DESTINATION_REJECTED, _R.PRECONDITION_FAILED),
    _t(
        "T10",
        _S.DISPATCHED,
        _S.OUTCOME_UNKNOWN,
        _R.TIMEOUT,
        _R.TRANSPORT_ERROR,
        _R.SERVER_ERROR,
        _R.IN_PROGRESS,
        _R.RECOVERED_OPEN_ATTEMPT,
    ),
    _t("T11", _S.OUTCOME_UNKNOWN, _S.CONFIRMED, _R.STATUS_FOUND, _R.HUMAN_ATTESTED),
    _t("T12", _S.OUTCOME_UNKNOWN, _S.FAILED, _R.STATUS_SETTLED_EMPTY, _R.HUMAN_ATTESTED),
    _t("T13", _S.OUTCOME_UNKNOWN, _S.DISPATCHED, _R.RETRY),
)
"""The transition table of record 0003, as amended by record 0010. No other transitions exist."""

REASON_NEEDS: Mapping[Reason, Capability] = {
    Reason.IN_PROGRESS: Capability.IDEMPOTENCY_KEY,
    Reason.RETRY: Capability.IDEMPOTENCY_KEY,
    Reason.STATUS_FOUND: Capability.STATUS,
    Reason.STATUS_SETTLED_EMPTY: Capability.STATUS,
    Reason.PRECONDITION_FAILED: Capability.CONDITIONAL_WRITE,
}
"""Reasons that only a contract with the named capability can produce (0003, 0005)."""

_BY_STATES: dict[tuple[ActionState | None, ActionState], Transition] = {
    (t.source, t.target): t for t in TRANSITIONS
}


class GatewayRuleError(Exception):
    """A step was asked to do something the decision records do not allow."""


class IllegalTransition(GatewayRuleError):
    """The transition is not in the table, or not with this reason or contract."""


def transition_for(source: ActionState | None, target: ActionState) -> Transition | None:
    """Return the table row from `source` to `target`, or None if there is none."""
    return _BY_STATES.get((source, target))


def check_transition(
    source: ActionState | None, target: ActionState, reason: Reason, contract: Contract
) -> Transition:
    """Return the transition for this move, or raise `IllegalTransition`."""
    row = _BY_STATES.get((source, target))
    if row is None:
        raise IllegalTransition(f"no transition from {source} to {target}")
    if reason not in row.reasons:
        raise IllegalTransition(f"{row.tid} ({source} to {target}) has no reason {reason}")
    needed = REASON_NEEDS.get(reason)
    if needed is not None and not contract.has(needed):
        raise IllegalTransition(
            f"{row.tid} with reason {reason} needs {needed}; {contract} lacks it"
        )
    return row


class GrantState(StrEnum):
    """Grant states (0002, rule 14)."""

    PENDING = "pending"
    ACTIVE = "active"
    CONSUMED = "consumed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


_G = GrantState

GRANT_TRANSITIONS: Mapping[GrantState | None, frozenset[GrantState]] = {
    None: frozenset({_G.PENDING}),
    _G.PENDING: frozenset(
        {_G.ACTIVE, _G.DENIED, _G.TIMED_OUT, _G.RELEASED, _G.REVOKED, _G.EXPIRED}
    ),
    _G.ACTIVE: frozenset({_G.CONSUMED, _G.RELEASED, _G.REVOKED, _G.EXPIRED}),
    _G.CONSUMED: frozenset({_G.REVOKED, _G.EXPIRED}),
}
"""Legal grant moves (0002, rule 14). States missing as keys have no outgoing move."""


def check_grant_transition(source: GrantState | None, target: GrantState) -> None:
    """Raise `IllegalTransition` unless the grant may move from `source` to `target`."""
    if target not in GRANT_TRANSITIONS.get(source, frozenset()):
        raise IllegalTransition(f"no grant transition from {source} to {target}")


class AnswerClass(StrEnum):
    """The four classes of destination answers (0003).

    Only committed and rejected are definitive, and only a definitive answer ends an
    attempt. A 503 is unknown: a proxy can return it after the backend committed.
    """

    COMMITTED = "committed"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"


ANSWER_REASONS: Mapping[AnswerClass, frozenset[Reason]] = {
    AnswerClass.COMMITTED: frozenset({Reason.COMMITTED}),
    AnswerClass.REJECTED: frozenset({Reason.DESTINATION_REJECTED, Reason.PRECONDITION_FAILED}),
    AnswerClass.IN_PROGRESS: frozenset({Reason.IN_PROGRESS}),
    AnswerClass.UNKNOWN: frozenset({Reason.TIMEOUT, Reason.TRANSPORT_ERROR, Reason.SERVER_ERROR}),
}
"""The reason code each answer class records (T8, T9, T10)."""


class CheckKind(StrEnum):
    """Which checks must pass before a send (I3)."""

    FULL = "full"
    """Authority and every dependency rule: before the first send."""

    EXACT_ONLY = "exact_only"
    """Authority and the exact rules only: before a retry at C2."""

    EXEMPT = "exempt"
    """No gateway check: a retry at C3, where the destination checks atomically."""

    AUTHORITY_ONLY = "authority_only"
    """Authority only: G1's one check before its one send, which reads no fact (0008)."""


def required_check(
    contract: Contract, attempt_no: int, *, authorization_only: bool = False
) -> CheckKind:
    """Return the check that must pass before attempt `attempt_no` is sent (I3, 0002 rule 17).

    `authorization_only` is G1 (0008): it checks the approval and no fact before its one
    send, and never retries.

    Raises `GatewayRuleError` for a retry in G1, or on a contract without idempotency
    keys (I4).
    """
    if attempt_no < 1:
        raise ValueError("attempt numbers start at 1")
    if authorization_only:
        if attempt_no > 1:
            raise GatewayRuleError("G1 never retries (0008)")
        return CheckKind.AUTHORITY_ONLY
    if attempt_no == 1:
        return CheckKind.FULL
    if not contract.has(Capability.IDEMPOTENCY_KEY):
        raise GatewayRuleError(f"{contract} allows no retries (I4)")
    if contract.has(Capability.CONDITIONAL_WRITE):
        return CheckKind.EXEMPT
    return CheckKind.EXACT_ONLY


class ReconcileHow(StrEnum):
    """How an unknown outcome is being resolved, as the agent is told (0003)."""

    ESCALATED_TO_HUMAN = "escalated_to_human"
    QUERYING_STATUS = "querying_status"
    RETRYING_WITH_SAME_KEY = "retrying_with_same_key"


def reconcile_how(contract: Contract, *, escalated: bool) -> ReconcileHow:
    """Return what the gateway is doing about an OutcomeUnknown action (0003, 0005)."""
    if escalated:
        return ReconcileHow.ESCALATED_TO_HUMAN
    if contract.has(Capability.STATUS):
        return ReconcileHow.QUERYING_STATUS
    if contract.has(Capability.IDEMPOTENCY_KEY):
        return ReconcileHow.RETRYING_WITH_SAME_KEY
    return ReconcileHow.ESCALATED_TO_HUMAN


@dataclass(frozen=True)
class GatewaySettings:
    """The gateway's timing and limits (0005), with the destination's promises.

    Times are virtual milliseconds. `approval_wait_ms` comes from the scenario's
    `authority` section (0002, 0009); the other defaults are 0005's. `agent_workers` is
    how many agent workers the run config expects to report: one, or two when its fault
    plan includes F9, which starts a second worker (0009). The run closes in the
    transaction that accepts the last of their reports (0010). If a planned F9 never
    fires, the run closes when the agents' phase ends instead (0003).
    """

    approval_wait_ms: int
    params: ContractParams = field(default_factory=ContractParams)
    dispatch_timeout_ms: int = 2_000
    max_attempts: int = 5
    max_probes: int = 20
    agent_workers: int = 1

    def __post_init__(self) -> None:
        if self.dispatch_timeout_ms <= 2 * self.params.max_hop_latency_ms:
            raise ValueError("dispatch_timeout_ms must exceed 2 x max_hop_latency_ms (0005)")
        if self.approval_wait_ms <= 0 or self.max_attempts < 1 or self.max_probes < 1:
            raise ValueError("approval_wait_ms, max_attempts and max_probes must be positive")
        if self.agent_workers < 1:
            raise ValueError("a run has at least one agent worker (0009)")

    @property
    def settle_ms(self) -> int:
        """S, the settle time: `max_hop_latency_ms + commit_deadline_ms` (0004, 0005)."""
        return self.params.settle_ms

    def backoff_ms(self, attempts_sent: int) -> int:
        """How long after the latest send the next retry may go (0005).

        The backoff is T, then doubling, capped at 8 x T. Record 0005 does not say what it
        is counted from; counting from the latest send keeps retries at least T apart,
        which 0005 requires so that a rejected retry settles the attempts before it.
        """
        if attempts_sent < 1:
            raise ValueError("a retry follows at least one attempt")
        t = self.dispatch_timeout_ms
        return min(t << (attempts_sent - 1), 8 * t)
