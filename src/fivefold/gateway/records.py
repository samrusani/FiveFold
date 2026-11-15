"""Gateway records, step inputs and agent-facing results (0001, 0002, 0003).

The record types mirror the `gw` tables in record 0003's data model. Stores build them,
step functions read and replace them, and the invariant checks read nothing else. All
records are immutable; mappings inside them are read-only views.

Two fields go beyond the data model, as implementation detail:

- `CheckRecord.seen_seq`: the seq of the action's latest event when the dispatch check
  began. A send commits only if no event was added since (0003, dispatch; I3).
- `ToolCall.as_of_seq`: the event seq a response reflects, which I5 compares against.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from fivefold.domain.contracts import Capability, Contract
from fivefold.gateway.state_machine import (
    ANSWER_REASONS,
    ActionState,
    AnswerClass,
    CheckKind,
    EventKind,
    GrantState,
    Note,
    Reason,
    ReconcileHow,
)


def frozen[K, V](mapping: Mapping[K, V]) -> Mapping[K, V]:
    """Return a read-only copy of `mapping`."""
    return MappingProxyType(dict(mapping))


NO_VERSIONS: Mapping[str, int] = MappingProxyType({})
NO_DETAILS: Mapping[str, object] = MappingProxyType({})
NO_ARGS: Mapping[str, object] = MappingProxyType({})
"""The arguments of a call that has none."""

# Records -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One logical effect (0001, 0003). `identity` is on in G2 and off in G1 (0008).

    `idempotency_key` is the action's own key, its id, in both configurations. A request
    carries it only where a contract takes keys (C2, C3) and only in G2 (0005, 0008).
    """

    action_id: str
    task_id: str
    intent_id: str
    operation: str
    target: str
    args: Mapping[str, str | int]
    action_key: str
    identity: bool
    contract: Contract
    basis: Mapping[str, int]
    state: ActionState
    grant_id: str | None
    idempotency_key: str
    effect_id: str | None
    created_vt: int
    updated_vt: int
    """The virtual time of the latest state change; notes do not change it."""

    @property
    def authorization_only(self) -> bool:
        """Whether this is a G1 action: one with identity off (0003, 0008).

        G1 is authorization only. It files one approval request per write call, checks the
        approval and no fact before its one send, and has no idempotency keys, status
        probes, retries, escalation, report checks or run closing.
        """
        return not self.identity

    @property
    def request_key(self) -> str | None:
        """The idempotency key a request for this action carries, if any (0005, 0008)."""
        if self.authorization_only or not self.contract.has(Capability.IDEMPOTENCY_KEY):
            return None
        return self.idempotency_key


@dataclass(frozen=True)
class Grant:
    """The binding of one approval to one action (0002, rules 12 to 16)."""

    grant_id: str
    action_id: str
    approval_id: str
    state: GrantState
    requested_vt: int
    consumed_vt: int | None


class ApprovalStatus(StrEnum):
    """An approval's status as the approvals service reports it (0002, rules 6 to 9)."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True)
class StaleFact:
    """A cited fact that broke a dependency rule, as the agent is told (0003)."""

    key: str
    cited_version: int
    current_version: int
    changed_fields: tuple[str, ...]


@dataclass(frozen=True)
class BasisCheck:
    """The result of checking an action's basis against current facts (0004, property 3).

    `sufficient_ok` is None when only the exact rules were checked (a C2 retry, I3).
    """

    exact_ok: bool
    sufficient_ok: bool | None
    versions_seen: Mapping[str, int] = NO_VERSIONS
    stale: tuple[StaleFact, ...] = ()

    @property
    def fresh(self) -> bool:
        """Whether every dependency rule holds."""
        return self.exact_ok and self.sufficient_ok is True


@dataclass(frozen=True)
class CheckRecord:
    """The check made before a send, stored with the attempt (I3).

    `approval_expires_at` is the approval's expiry as the approvals service reported it,
    recorded exactly when the approval is approved: an approval that expires before a
    send could commit counts as expired (0010, change 4).

    An `authority_only` check, G1's (0008), records the approval and no basis, because G1
    reads no fact before it sends.
    """

    kind: CheckKind
    approval: ApprovalStatus | None
    basis: BasisCheck | None
    seen_seq: int
    check_vt: int
    approval_expires_at: int | None = None

    def __post_init__(self) -> None:
        if self.kind is CheckKind.EXEMPT:
            if self.approval is not None or self.basis is not None:
                raise ValueError("an exempt check records no approval and no basis")
            if self.approval_expires_at is not None:
                raise ValueError("an exempt check records no approval expiry")
            return
        if self.kind is CheckKind.AUTHORITY_ONLY:
            if self.approval is None or self.basis is not None:
                raise ValueError("an authority_only check records the approval and no basis")
        elif self.approval is None or self.basis is None:
            raise ValueError(f"a {self.kind} check records the approval and the basis")
        elif (self.kind is CheckKind.FULL) != (self.basis.sufficient_ok is not None):
            raise ValueError(
                "a full check covers the sufficient rules; an exact_only check does not"
            )
        if (self.approval is ApprovalStatus.APPROVED) != (self.approval_expires_at is not None):
            raise ValueError("an approved approval's expiry is recorded, and only that")

    def approval_at_send(self, sent_vt: int, settle_ms: int) -> ApprovalStatus | None:
        """The approval's status for a send at `sent_vt` (0002 rule 17, 0010 change 4).

        A request sent at `sent_vt` commits by `sent_vt + S` or never, so an approved
        approval that expires by then counts as expired. The send is at or after the
        check, so this also keeps the approval alive past the check's time plus S.
        """
        expires_at = self.approval_expires_at
        approved = self.approval is ApprovalStatus.APPROVED
        if approved and expires_at is not None and expires_at <= sent_vt + settle_ms:
            return ApprovalStatus.EXPIRED
        return self.approval

    def passes(self, sent_vt: int, settle_ms: int) -> bool:
        """Whether this check allows a send at `sent_vt` (0002 rule 17, I3)."""
        if self.kind is CheckKind.EXEMPT:
            return True
        approved = self.approval_at_send(sent_vt, settle_ms) is ApprovalStatus.APPROVED
        if self.kind is CheckKind.AUTHORITY_ONLY:
            return approved
        if not approved or self.basis is None:
            return False
        return self.basis.exact_ok and self.basis.sufficient_ok is not False


@dataclass(frozen=True)
class Attempt:
    """One send of an action (0003). `answer_class` is None while the attempt is open."""

    attempt_id: str
    action_id: str
    attempt_no: int
    idempotency_key: str
    grant_id: str
    check: CheckRecord
    sent_vt: int
    answer_class: AnswerClass | None
    answer_ref: str | None
    answered_vt: int | None


@dataclass(frozen=True)
class ActionEvent:
    """One append-only row of the action's log (I6). Notes keep the state unchanged."""

    seq: int
    action_id: str
    kind: EventKind
    from_state: ActionState | None
    to_state: ActionState
    reason: Reason | Note
    details: Mapping[str, object]
    vt: int


class Resolution(StrEnum):
    """What a human attests about an escalated action (0003, escalation)."""

    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"


@dataclass(frozen=True)
class Escalation:
    """A request for a human to resolve an unknown outcome (0003, escalation)."""

    escalation_id: str
    action_id: str
    opened_vt: int
    resolved_vt: int | None
    resolution: Resolution | None
    effect_id: str | None
    actor: str | None


# Step inputs ---------------------------------------------------------------------------


def check_text(value: str, what: str) -> None:
    """Refuse text that no store can keep: PostgreSQL text cannot hold a NUL character."""
    if "\x00" in value:
        raise ValueError(f"{what} contains a NUL character")


@dataclass(frozen=True)
class Proposal:
    """A write proposal that passed the schema and read checks (0001, item 17).

    The store checks the intent and applies the matching rule. Arguments are text or
    integers and versions are integers (0001, item 6); this is checked here, so every
    store refuses the same proposals.
    """

    intent_id: str
    operation: str
    target: str
    args: Mapping[str, str | int]
    action_key: str
    basis: Mapping[str, int]

    def __post_init__(self) -> None:
        for what in ("intent_id", "operation", "target", "action_key"):
            check_text(getattr(self, what), what)
        for key, value in self.args.items():
            check_text(key, "an argument name")
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise ValueError(f"argument {key} must be text or an integer")
            if isinstance(value, str):
                check_text(value, f"argument {key}")
        for key, version in self.basis.items():
            check_text(key, "a fact key")
            if isinstance(version, bool) or not isinstance(version, int):
                raise ValueError(f"the version of {key} must be an integer")


@dataclass(frozen=True)
class WriteCall:
    """A write tool call that passed the schema check, as the store admits it (0001).

    `based_on` holds the read ids the agent cited. `dependencies` holds the fact keys the
    operation depends on (0004, dependency rules), which the citations must cover. The
    store resolves the citations against its read records in the transaction that checks
    the intent and applies the matching rule, so every check runs in the order of 0001,
    item 17. The action's basis is the cited version of each dependency; a dependency
    cited more than once takes its newest cited version. Extra citations are recorded on
    the proposal but are not part of the basis (item 15).
    """

    intent_id: str
    operation: str
    target: str
    args: Mapping[str, str | int]
    action_key: str
    based_on: tuple[str, ...]
    dependencies: tuple[str, ...]

    def __post_init__(self) -> None:
        for what in ("intent_id", "operation", "target", "action_key"):
            check_text(getattr(self, what), what)
        for text in (*self.based_on, *self.dependencies):
            check_text(text, "a citation or a dependency")
        for key, value in self.args.items():
            check_text(key, "an argument name")
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise ValueError(f"argument {key} must be text or an integer")
            if isinstance(value, str):
                check_text(value, f"argument {key}")


@dataclass(frozen=True)
class ReadRecord:
    """A read the gateway served, which a proposal can cite by its id (0001, item 13).

    `data` is the fact's value at that version, decoded from JSON, which the dependency
    rules compare with the value now (0004).
    """

    read_id: str
    key: str
    version: int
    data: Mapping[str, object]
    vt: int


class Admission(StrEnum):
    """What admission did with a write call (0001, items 10 and 17)."""

    ADMITTED = "admitted"
    """It created a new action."""

    ATTACHED = "attached"
    """It returned an existing action with the same key."""

    REFUSED = "refused"


@dataclass(frozen=True)
class ProposalRecord:
    """One write call and its outcome; every call has one, admitted or refused (0001).

    `args` holds the business arguments in normal form, or, for a call refused with
    `invalid_args`, the arguments as the agent sent them. `intent_id` is None only when
    such a call named no intent as text. `action_id` names the action the call created
    or attached to, or the one an `intent_busy` or `intent_done` refusal names.
    """

    proposal_id: str
    operation: str
    intent_id: str | None
    args: Mapping[str, object]
    based_on: tuple[str, ...]
    admission: Admission
    reason: RefusalReason | None
    action_id: str | None
    vt: int


@dataclass(frozen=True)
class CheckTicket:
    """What a dispatcher needs to check an action before a send (0003, dispatch)."""

    action_id: str
    attempt_no: int
    kind: CheckKind
    seen_seq: int
    not_before_vt: int

    def record(
        self,
        *,
        approval: ApprovalStatus | None,
        basis: BasisCheck | None,
        check_vt: int,
        expires_at: int | None = None,
    ) -> CheckRecord:
        """Return the check record for what the dispatcher found.

        `expires_at` is the approval's expiry, given exactly when it is approved.
        """
        return CheckRecord(self.kind, approval, basis, self.seen_seq, check_vt, expires_at)


@dataclass(frozen=True)
class Answer:
    """A destination's answer to an attempt, or the gateway's own timeout (0003)."""

    answer_class: AnswerClass
    reason: Reason
    effect_id: str | None = None

    def __post_init__(self) -> None:
        if self.reason not in ANSWER_REASONS[self.answer_class]:
            raise ValueError(f"a {self.answer_class} answer cannot have reason {self.reason}")
        if (self.answer_class is AnswerClass.COMMITTED) != (self.effect_id is not None):
            raise ValueError("an effect id comes with committed answers and only with them")


@dataclass(frozen=True)
class ProbeAnswer:
    """A C1 status query's result (0005). Unanswered when no answer came within T."""

    answered: bool
    effect_ids: tuple[str, ...] = ()
    as_of_vt: int | None = None

    def __post_init__(self) -> None:
        if self.answered != (self.as_of_vt is not None):
            raise ValueError("an answered probe has as_of_vt; an unanswered one does not")
        if not self.answered and self.effect_ids:
            raise ValueError("an unanswered probe lists no effects")


class ClaimStatus(StrEnum):
    """A final report's claim about one intent (0004, property 5)."""

    DONE = "done"
    NOT_DONE = "not_done"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Claim:
    """One claim of a final report. `effect_ids` is non-empty exactly for `done`."""

    intent_id: str
    status: ClaimStatus
    effect_ids: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if (self.status is ClaimStatus.DONE) != bool(self.effect_ids):
            raise ValueError("effect_ids must be non-empty for done and empty otherwise")
        for text in (self.intent_id, self.note, *self.effect_ids):
            check_text(text, "a claim")


# Agent-facing results ------------------------------------------------------------------


@dataclass(frozen=True)
class Reconcile:
    """How an unknown outcome is being resolved (0003). Only with `outcome_unknown`.

    `what` says in words which effect may or may not exist.
    """

    contract: Contract
    how: ReconcileHow
    escalation_id: str | None
    what: str


@dataclass(frozen=True)
class ActionResult:
    """The `{"result": "action", ...}` shape of 0003. `reason` is the latest transition's."""

    action_id: str
    intent_id: str
    state: ActionState
    effect_id: str | None
    reason: Reason
    stale: tuple[StaleFact, ...]
    reconcile: Reconcile | None


class RefusalReason(StrEnum):
    """Refusal reasons (0001, 0003)."""

    INVALID_ARGS = "invalid_args"
    UNKNOWN_INTENT = "unknown_intent"
    WRONG_OPERATION = "wrong_operation"
    UNKNOWN_READ = "unknown_read"
    MISSING_BASIS = "missing_basis"
    INTENT_BUSY = "intent_busy"
    INTENT_DONE = "intent_done"
    RUN_CLOSED = "run_closed"
    REPORT_INCONSISTENT = "report_inconsistent"


@dataclass(frozen=True)
class RecordedAction:
    """The gateway's record of one action, as a report refusal names it (0003).

    `effect_ids` holds every effect id the gateway learned for the action.
    """

    action_id: str
    state: ActionState
    effect_ids: tuple[str, ...]


@dataclass(frozen=True)
class ClaimConflict:
    """A report claim that contradicts the gateway's records (0003, final report)."""

    intent_id: str
    claimed: ClaimStatus
    claimed_effect_ids: tuple[str, ...]
    recorded: tuple[RecordedAction, ...]


@dataclass(frozen=True)
class Refusal:
    """The `{"result": "refused", ...}` shape of 0003, with its details.

    `unknown_reads` lists the cited read ids the gateway never issued (`unknown_read`),
    `missing_keys` the dependencies no citation covers (`missing_basis`), and `message`
    says what broke the schema (`invalid_args`).
    """

    reason: RefusalReason
    action_id: str | None = None
    state: ActionState | None = None
    effect_id: str | None = None
    args: Mapping[str, str | int] | None = None
    conflicts: tuple[ClaimConflict, ...] = ()
    unknown_reads: tuple[str, ...] = ()
    missing_keys: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class ReportAccepted:
    """A final report the gateway accepted, with the claims it accepted."""

    claims: tuple[Claim, ...]


@dataclass(frozen=True)
class ToolCall:
    """One agent-facing response, logged for I5. `as_of_seq` is the event seq it reflects.

    `args` are the arguments of the call it answers, as the gateway received them, kept as
    `fivefold.gateway.codec.storable` makes them: arrays as tuples, objects as read-only
    mappings.
    """

    call_no: int
    tool: str
    args: Mapping[str, object]
    action_id: str | None
    result: ActionResult | Refusal | ReportAccepted
    as_of_seq: int
    vt: int


# Snapshots -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """Every gateway record of one run, for the invariant checks.

    `attempts` maps each action id to its attempts in attempt order. `events` and
    `tool_calls` are in seq and call order. `closed_intents` are the intents an accepted
    `not_done` claim closed, and `reports` maps each agent worker whose report was
    accepted to the call number of its first accepted report (0010, item 6). `reads`
    maps each read id to the read it names, and `proposals` lists every write call in
    the order the calls came (0001, items 13 and 17).
    """

    intents: Mapping[str, str]
    actions: Mapping[str, Action]
    grants: Mapping[str, Grant]
    attempts: Mapping[str, tuple[Attempt, ...]]
    events: tuple[ActionEvent, ...]
    escalations: Mapping[str, Escalation]
    tool_calls: tuple[ToolCall, ...]
    run_closed: bool
    closed_intents: frozenset[str] = frozenset()
    reports: Mapping[str, int] = field(default_factory=lambda: frozen({}))
    reads: Mapping[str, ReadRecord] = field(default_factory=lambda: frozen({}))
    proposals: tuple[ProposalRecord, ...] = ()
