"""What the stack could know when its report was accepted (0004, property 5).

`t_r` is the harness seq of the counted report's delivery to the component that accepted
it. A message is evidence only if it was delivered to a stack component before `t_r` and
no crash voided it. The stack is the gateway in G modes and the agent workers in
baselines (0008). Evidence comes from two places, and gateway tables are never read:

- Answers from the simulator on the stack's hop: write answers (committed or a
  definitive rejection) and C1 status answers. Other answers, such as `in_progress`, a
  server error or a refusal, establish nothing.
- Attestations by the scripted resolver in `harness.actor_log`, made before `t_r`.

Let `S = max_hop_latency + commit_deadline`, from the destination's published promises
(`sim.run`). For a write request `q` for intent `i`, sent by the stack at virtual time `s`,
each of these establishes that effect `e` committed:

- a write answer naming `e`, new or replayed;
- a C1 status answer listing `e`;
- an attestation naming `e`.

Each of these establishes that `q` never commits (a `Settlement`):

- a definitive rejection of `q` itself;
- a status answer for `i` and `q`'s operation listing nothing, with `as_of_vt >= s + S`;
- at C2 or C3, a definitive rejection of a request with `q`'s idempotency key, sent at
  least `max_hop_latency` after `q`;
- an attestation that `i` is not committed, made at or after `s + S`.

`known_i` is the set of `i`'s effects with commit evidence. `K_i` is `committed` if
`known_i` is not empty, `not_committed` if every write request for `i` sent before `t_r`
never commits by the evidence (true when none was sent), and `unknowable` otherwise. A
request that a crash left possibly sent counts as sent, without an answer (0006).

Recovery latency reads the same evidence at other moments than `t_r` (0014): with `t_r`
past the run's last event, `stack_knowledge` gives `K_i` at the stop and, for each request,
the first evidence that it never commits; `commit_evidence` gives each effect's first
commit evidence.

Readings of the harness's records that the decision records leave open:

- A possibly-sent request is a `harness.fault_log` row at `gw.dispatch.after_record`. Its
  `params.request` names the request: `intent_id` (required), `api` (optional; else the
  intent's operation) and `idempotency_key` (optional). The fault's `vt` is its send time
  and the fault's `seq` places it before or after `t_r`.
- An attestation is a `harness.actor_log` row by `resolver` with action `attest`. Its
  `details.resolution` is `committed`, with `details.effect_id`, or `not_committed`, with
  `details.intent_id`. Its `vt` is when it was made and its `seq` when the stack got it.
- A crash's `voided` list names message seqs; a voided message is never evidence.
- A request's intent and idempotency key are read from its body, the request as sent;
  the log's columns must agree when both are set.
- A copy the transport made of a request it delivered twice (F4, `copy_of` set) is not a
  request the stack sent, and the answer to a copy is never evidence, even if a log
  showed it delivered: the transport never delivers it (0006, transport rules).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from fivefold.domain.api import StatusAnswer, WriteCommitted, WriteRejected
from fivefold.domain.contracts import Capability
from fivefold.evaluator.records import (
    ATTEST,
    POSSIBLY_SENT_POINT,
    RESOLVER,
    SIMULATOR,
    STATUS_APIS,
    WRITE_APIS,
    Message,
    RecordsError,
    RunRecords,
)


class Knowledge(StrEnum):
    """`K_i`: what the stack could know about intent `i` at `t_r` (0004)."""

    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    UNKNOWABLE = "unknowable"


class CommitSource(StrEnum):
    """What showed the stack that an effect committed (0004, evidence table)."""

    WRITE_ANSWER = "write_answer"
    """A write answer naming the effect, new or replayed."""
    STATUS_ANSWER = "status_answer"
    """A C1 status answer listing the effect."""
    ATTESTATION = "attestation"
    """A human attestation naming the effect."""


@dataclass(frozen=True)
class CommitEvidence:
    """The first evidence that an effect committed: the harness seq at which the stack got
    it, and what it was."""

    seq: int
    source: CommitSource


class Settlement(StrEnum):
    """How the evidence shows that a request never commits (0004, evidence table)."""

    REJECTED = "rejected"
    """A definitive rejection of the request itself."""
    STATUS_EMPTY = "status_empty"
    """A status answer listing nothing, accurate to at least the send plus S."""
    SAME_KEY_REJECTED = "same_key_rejected"
    """C2 or C3: a rejection of a request with the same key, sent at least a hop later."""
    ATTESTED = "attested_not_committed"
    """A human attested that nothing committed, at or after the send plus S."""


@dataclass(frozen=True)
class SentWrite:
    """A write request the stack sent before `t_r`, or may have sent.

    `seq` is the harness seq of the send, or of the crash that left it possibly sent.
    """

    seq: int
    intent_id: str
    operation: str | None
    idempotency_key: str | None
    sent_vt: int
    possibly_sent: bool = False


@dataclass(frozen=True)
class RequestFinding:
    """Whether the evidence shows that one request never commits, and how."""

    request: SentWrite
    settled_by: Settlement | None
    evidence_seq: int | None
    """The harness seq at which the stack got the evidence that settled it."""


@dataclass(frozen=True)
class IntentKnowledge:
    """`K_i`, `known_i` and the findings for each request of one intent."""

    intent_id: str
    knowledge: Knowledge
    known_effects: tuple[str, ...]
    """`known_i`, in commit order."""
    requests: tuple[RequestFinding, ...]


@dataclass(frozen=True)
class _Rejection:
    request: Message
    seq: int


@dataclass(frozen=True)
class _EmptyStatus:
    intent_id: str
    operation: str
    as_of_vt: int
    seq: int


@dataclass(frozen=True)
class _NotCommitted:
    intent_id: str
    vt: int
    seq: int


@dataclass
class _Evidence:
    committed: dict[str, CommitEvidence]
    """Effect id to its first commit evidence."""
    rejections: list[_Rejection]
    empty_statuses: list[_EmptyStatus]
    not_committed: list[_NotCommitted]


def stack_knowledge(records: RunRecords, t_r: int) -> dict[str, IntentKnowledge]:
    """`K_i` for every task intent, from the evidence the stack had before `t_r`.

    The result is keyed by intent id, in the scenario's intent order.
    """
    evidence = _gather(records, t_r)
    sent = sent_writes(records, t_r)
    order = sorted(records.effects, key=lambda effect: effect.commit_seq)
    found: dict[str, IntentKnowledge] = {}
    for intent in records.scenario.intents:
        known = tuple(
            effect.effect_id
            for effect in order
            if effect.intent_id == intent.id and effect.effect_id in evidence.committed
        )
        findings = tuple(
            _finding(request, evidence, records)
            for request in sent
            if request.intent_id == intent.id
        )
        if known:
            knowledge = Knowledge.COMMITTED
        elif all(finding.settled_by is not None for finding in findings):
            knowledge = Knowledge.NOT_COMMITTED
        else:
            knowledge = Knowledge.UNKNOWABLE
        found[intent.id] = IntentKnowledge(intent.id, knowledge, known, findings)
    return found


def commit_evidence(records: RunRecords, t_r: int) -> dict[str, CommitEvidence]:
    """The first commit evidence for each effect that has any before `t_r`, by effect id."""
    return dict(_gather(records, t_r).committed)


def evidence_messages(records: RunRecords, t_r: int) -> list[Message]:
    """Answers from the simulator that reached the stack before `t_r`, unvoided, in
    delivery order."""
    config = records.run.config
    voided = {seq for fault in records.faults for seq in fault.voided}
    copies = {message.seq for message in records.messages if message.copy_of is not None}
    found = [
        message
        for message in records.messages
        if message.kind == "answer"
        and message.hop == config.stack_hop
        and message.sender == SIMULATOR
        and config.is_stack(message.receiver)
        and message.delivered_before(t_r)
        and message.seq not in voided
        and message.correlation not in copies
    ]
    return sorted(found, key=lambda message: (message.delivered_seq or 0, message.seq))


def sent_writes(records: RunRecords, t_r: int) -> list[SentWrite]:
    """The stack's write requests sent before `t_r`, then its possibly-sent ones."""
    config = records.run.config
    found: list[SentWrite] = []
    for message in sorted(records.messages, key=lambda message: message.seq):
        if (
            message.kind != "request"
            or message.copy_of is not None
            or message.hop != config.stack_hop
            or message.receiver != SIMULATOR
            or not config.is_stack(message.sender)
            or message.api not in WRITE_APIS
            or message.seq >= t_r
        ):
            continue
        intent_id = _field(message, "intent_id", message.intent_id)
        if intent_id is None:
            raise RecordsError(f"write request {message.seq} names no intent")
        key = _field(message, "idempotency_key", message.idempotency_key)
        found.append(SentWrite(message.seq, intent_id, message.api, key, message.sent_vt))
    operations = {intent.id: str(intent.operation) for intent in records.scenario.intents}
    for fault in sorted(records.faults, key=lambda fault: fault.seq):
        if fault.point != POSSIBLY_SENT_POINT or fault.seq >= t_r:
            continue
        request = (fault.params or {}).get("request")
        if not isinstance(request, Mapping):
            raise RecordsError(f"fault {fault.seq} at {fault.point} names no request")
        intent_id = request.get("intent_id")
        api = request.get("api")
        key = request.get("idempotency_key")
        if not isinstance(intent_id, str):
            raise RecordsError(f"fault {fault.seq}: the request names no intent")
        if api is not None and (not isinstance(api, str) or api not in WRITE_APIS):
            raise RecordsError(f"fault {fault.seq}: the request's api is not a write: {api!r}")
        if not isinstance(key, str | None):
            raise RecordsError(f"fault {fault.seq}: the request's idempotency_key must be text")
        operation = api if isinstance(api, str) else operations.get(intent_id)
        found.append(SentWrite(fault.seq, intent_id, operation, key, fault.vt, possibly_sent=True))
    return found


def _field(message: Message, name: str, column: str | None) -> str | None:
    """A request field from its body, which must agree with the log's column if both are
    set."""
    body = message.body
    value = body.get(name) if isinstance(body, Mapping) else None
    if value is not None and not isinstance(value, str):
        raise RecordsError(f"message {message.seq}: {name} in the body must be text")
    if value is not None and column is not None and value != column:
        raise RecordsError(
            f"message {message.seq}: the log says {name} {column!r}, the body {value!r}"
        )
    return value if value is not None else column


def _answer_kind(message: Message) -> object:
    body = message.body
    return body.get("answer") if isinstance(body, Mapping) else None


def _gather(records: RunRecords, t_r: int) -> _Evidence:
    """Collect every piece of evidence the stack had before `t_r`."""
    ledger = {effect.effect_id for effect in records.effects}
    requests = {message.seq: message for message in records.messages if message.kind == "request"}
    evidence = _Evidence(committed={}, rejections=[], empty_statuses=[], not_committed=[])

    def commit(effect_id: str, seq: int, source: CommitSource, what: str) -> None:
        if effect_id not in ledger:
            raise RecordsError(f"{what} names effect {effect_id}, which the ledger lacks")
        first = evidence.committed.get(effect_id)
        if first is None or seq < first.seq:
            evidence.committed[effect_id] = CommitEvidence(seq, source)

    for message in evidence_messages(records, t_r):
        seq = message.delivered_seq or 0
        kind = _answer_kind(message)
        try:
            if message.api in WRITE_APIS and kind == "committed":
                committed = WriteCommitted.model_validate(message.body)
                commit(committed.effect_id, seq, CommitSource.WRITE_ANSWER, f"answer {message.seq}")
            elif message.api in WRITE_APIS and kind == "rejected":
                WriteRejected.model_validate(message.body)
                request = (
                    requests.get(message.correlation) if message.correlation is not None else None
                )
                if request is None:
                    raise RecordsError(f"answer {message.seq} answers no request in the log")
                evidence.rejections.append(_Rejection(request, seq))
            elif message.api in STATUS_APIS and kind == "status":
                status = StatusAnswer.model_validate(message.body)
                for listed in status.effects:
                    what = f"status answer {message.seq}"
                    commit(listed.effect_id, seq, CommitSource.STATUS_ANSWER, what)
                if not status.effects:
                    evidence.empty_statuses.append(
                        _EmptyStatus(status.intent_id, str(status.operation), status.as_of_vt, seq)
                    )
        except ValidationError as error:
            raise RecordsError(f"answer {message.seq} does not parse as a {kind} answer") from error

    for action in sorted(records.actor_actions, key=lambda action: action.seq):
        if action.actor != RESOLVER or action.action != ATTEST or action.seq >= t_r:
            continue
        resolution = action.details.get("resolution")
        if resolution == "committed":
            effect_id = action.details.get("effect_id")
            if not isinstance(effect_id, str):
                raise RecordsError(f"attestation {action.seq} of a commit names no effect")
            what = f"attestation {action.seq}"
            commit(effect_id, action.seq, CommitSource.ATTESTATION, what)
        elif resolution == "not_committed":
            intent_id = action.details.get("intent_id")
            if not isinstance(intent_id, str):
                raise RecordsError(f"attestation {action.seq} names no intent")
            evidence.not_committed.append(_NotCommitted(intent_id, action.vt, action.seq))
        else:
            raise RecordsError(f"attestation {action.seq} has no resolution: {resolution!r}")
    return evidence


def _finding(request: SentWrite, evidence: _Evidence, records: RunRecords) -> RequestFinding:
    """The earliest evidence that `request` never commits, if any."""
    settle_ms = records.destination.settle_ms
    hop_ms = records.destination.params.max_hop_latency_ms
    keyed = records.run.contract.has(Capability.IDEMPOTENCY_KEY)
    found: list[tuple[int, Settlement]] = []
    for rejection in evidence.rejections:
        rejected = rejection.request
        if not request.possibly_sent and rejected.seq == request.seq:
            found.append((rejection.seq, Settlement.REJECTED))
        elif (
            keyed
            and request.idempotency_key is not None
            and _field(rejected, "idempotency_key", rejected.idempotency_key)
            == request.idempotency_key
            and rejected.sent_vt >= request.sent_vt + hop_ms
        ):
            found.append((rejection.seq, Settlement.SAME_KEY_REJECTED))
    for status in evidence.empty_statuses:
        if (
            status.intent_id == request.intent_id
            and status.operation == request.operation
            and status.as_of_vt >= request.sent_vt + settle_ms
        ):
            found.append((status.seq, Settlement.STATUS_EMPTY))
    for attested in evidence.not_committed:
        if attested.intent_id == request.intent_id and attested.vt >= request.sent_vt + settle_ms:
            found.append((attested.seq, Settlement.ATTESTED))
    if not found:
        return RequestFinding(request, None, None)
    seq, how = min(found)
    return RequestFinding(request, how, seq)
