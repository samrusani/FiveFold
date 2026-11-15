"""The harness's records of a run: messages, faults, scripted humans, reports and model
calls (0004).

The evaluator scores from these records and the simulator's, never from the gateway's
(0004). One counter numbers every harness event in order: a message's send and its
delivery, a fault firing, an action of a scripted human and a submitted report. So "this
message was delivered before that report" is a comparison of two numbers. Each fault
names the task intent it touched, which recovery latency starts from (0014).

The database stamps each record with the wall-clock time it was written (`wall_at`), and
a delivery with the time of its fate (`delivered_wall_at`), for metrics only (0009).

Model calls have their own numbers, in the order the model answered them, which is the
order of the run's cassette (0009). They take none of the event counter's, so logging them
leaves every other record's numbers as they were. The evaluator computes record 0004's
token metrics from them, and its report prices those tokens.

`MemoryHarnessLog` keeps the records in memory, for unit tests. `PgHarnessLog` writes them
to the `harness` schema of the run's database (`db/migrations/0004_harness.sql`) as role
`ff_harness`. Both number events the same way.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

JsonValue = object
"""A value `json.dumps` accepts: dicts, lists, text, numbers, booleans and None."""

Kind = Literal["request", "answer", "notification"]
Fate = Literal["delivered", "dropped"]
DropReason = Literal[
    "fault", "receiver_down", "caller_timed_out", "caller_crashed", "answer_to_copy"
]
"""Why a message was dropped. `answer_to_copy` is the answer to a request the transport
delivered a second time (F4): the transport never delivers it to the sender (0006)."""


@dataclass(frozen=True)
class MessageRecord:
    """One message on one hop, as sent. Its fate comes later (0004, 0006)."""

    seq: int
    hop: str
    kind: Kind
    sender: str
    receiver: str
    api: str | None
    correlation: int | None
    """For an answer, the seq of the request it answers."""
    intent_id: str | None
    idempotency_key: str | None
    answer_class: str | None
    body: JsonValue
    sent_vt: int
    fate: Fate | None = None
    drop_reason: DropReason | None = None
    delivered_seq: int | None = None
    delivered_vt: int | None = None
    copy_of: int | None = None
    """For a copy that the transport made of a request (F4), the seq of that request. Its
    sender sent the request once; the copy is the second delivery (0006)."""


@dataclass(frozen=True)
class FaultRecord:
    """A fault that fired, and what it did (0006)."""

    seq: int
    fault: str
    variant: str | None
    point: str
    occurrence: int
    action: str
    vt: int
    target_seq: int | None = None
    """The message the fault acted on, if any."""
    component: str | None = None
    """The component the fault acted on, if any: the one a crash stopped, the worker F9
    started, or the gateway where F6 or F7 fired at a dispatch point."""
    voided: tuple[int, ...] = ()
    """The seqs of messages whose delivery the fault voided (0006, crashes)."""
    params: Mapping[str, object] = field(default_factory=dict)
    intent_id: str | None = None
    """The task intent the fault touched: the intent of the message, action or tool call
    it acted on. None for F9, which touches every task intent, and for a message without
    an intent, such as a read (0014). F10 gets one more record for each status answer it
    changes, naming that answer and its intent."""


@dataclass(frozen=True)
class RunRecord:
    """What a run is: its scenario, configuration, contract, seed and model (0009)."""

    run_id: str
    scenario_id: str
    scenario_digest: str
    config: Literal["B0", "G1", "G2"]
    contract: Literal["C0", "C1", "C2", "C3"]
    seed: int
    model_adapter: Literal["scripted", "live", "replay"]
    model: str
    params: Mapping[str, object]
    topology: Literal["inproc", "split"] = "inproc"
    cassette_digest: str | None = None
    """The cassette a replay serves, or the one a recording made (`cassette_recorded`)."""
    parent_run_id: str | None = None
    """For an experimental branch, the run id of the run it branched from (0009)."""
    branch_point: int | None = None
    """For an experimental branch, K: how many of its parent's model calls it replayed
    before it switched model (0009). Set exactly when `parent_run_id` is."""


@dataclass(frozen=True)
class ActorRecord:
    """An action of a scripted human: the approver or the resolver (0002, 0004)."""

    seq: int
    actor: Literal["approver", "resolver"]
    action: str
    subject: str
    details: Mapping[str, object]
    vt: int


@dataclass(frozen=True)
class ReportRecord:
    """A final report an agent worker submitted, as the report sink received it (0004).

    `valid` says whether it met the report schema; `errors` says how it did not.
    `accepted_by` is `gateway` when G2 accepted it, or G1 forwarded it, and `sink` when
    a baseline agent submitted it to the sink directly (0008).
    """

    seq: int
    worker: str
    report: JsonValue
    valid: bool
    errors: tuple[str, ...]
    accepted_by: Literal["gateway", "sink"]
    vt: int
    message_seq: int | None = None


@dataclass(frozen=True)
class ModelCallRecord:
    """One model call of an agent worker, as its model answered it (0004, 0009)."""

    call_no: int
    """The call's number in the run, from 1, in the order the model answered."""
    worker: str
    asked_vt: int
    answered_vt: int
    model: str
    """The run's model, as `harness.run.model` names it."""
    input_tokens: int | None
    """None when the reply reports no usage, as scripted replies do."""
    output_tokens: int | None
    refused: str | None
    """Why the harness answered instead of the model, such as `spend_cap`; None when the
    model answered."""
    wall_ms: float
    """Wall-clock milliseconds from the call to its answer, for metrics only (0009)."""


class HarnessLog(ABC):
    """Numbers harness events and records messages, faults, reports and model calls."""

    @abstractmethod
    def run_recorded(self, record: RunRecord) -> None:
        """Record the run itself, once, before anything happens in it."""

    @abstractmethod
    def run_stopped(self, vt: int) -> None:
        """Record when the run stopped, once, at the end of the drain window (0009)."""

    @abstractmethod
    def cassette_recorded(self, digest: str) -> None:
        """Record, once, the digest of the cassette the run recorded (0009). It is known
        only when the run stops, after the run itself was recorded."""

    @abstractmethod
    def next_seq(self) -> int:
        """Reserve the next event number."""

    @abstractmethod
    def message_sent(self, record: MessageRecord) -> None:
        """Record a message as it is sent, with no fate yet."""

    @abstractmethod
    def message_fate(
        self,
        seq: int,
        fate: Fate,
        *,
        vt: int,
        reason: DropReason | None = None,
        delivered_seq: int | None = None,
    ) -> None:
        """Record what became of message `seq`: delivered, with the number of its
        delivery, or dropped, with why."""

    @abstractmethod
    def fault_fired(self, record: FaultRecord) -> None:
        """Record a fault that fired."""

    @abstractmethod
    def report_submitted(self, record: ReportRecord) -> None:
        """Record a final report as the sink received it."""

    @abstractmethod
    def actor_acted(self, record: ActorRecord) -> None:
        """Record what a scripted human did."""

    @abstractmethod
    def model_called(self, record: ModelCallRecord) -> None:
        """Record a model call once its model answered."""


def _check_fate(fate: Fate, reason: DropReason | None, delivered_seq: int | None) -> None:
    if fate == "delivered" and (delivered_seq is None or reason is not None):
        raise ValueError("a delivered message has a delivery number and no drop reason")
    if fate == "dropped" and (delivered_seq is not None or reason is None):
        raise ValueError("a dropped message has a drop reason and no delivery number")


class MemoryHarnessLog(HarnessLog):
    """Harness records kept in memory."""

    def __init__(self) -> None:
        self._last_seq = 0
        self._messages: dict[int, MessageRecord] = {}
        self.faults: list[FaultRecord] = []
        self.reports: list[ReportRecord] = []
        self.actors: list[ActorRecord] = []
        self.model_calls: list[ModelCallRecord] = []
        self.run: RunRecord | None = None
        self.stopped_vt: int | None = None

    def run_recorded(self, record: RunRecord) -> None:
        if self.run is not None:
            raise ValueError("the run is already recorded")
        self.run = record

    def run_stopped(self, vt: int) -> None:
        if self.stopped_vt is not None:
            raise ValueError("the run has already stopped")
        self.stopped_vt = vt

    def cassette_recorded(self, digest: str) -> None:
        if self.run is None or self.run.cassette_digest is not None:
            raise ValueError("the run was never recorded, or already names its cassette")
        self.run = replace(self.run, cassette_digest=digest)

    @property
    def messages(self) -> list[MessageRecord]:
        """Every message in send order, with its fate so far."""
        return [self._messages[seq] for seq in sorted(self._messages)]

    def next_seq(self) -> int:
        self._last_seq += 1
        return self._last_seq

    def message_sent(self, record: MessageRecord) -> None:
        if record.seq in self._messages:
            raise ValueError(f"message {record.seq} was already sent")
        if record.fate is not None:
            raise ValueError("a message has no fate when it is sent")
        self._messages[record.seq] = record

    def message_fate(
        self,
        seq: int,
        fate: Fate,
        *,
        vt: int,
        reason: DropReason | None = None,
        delivered_seq: int | None = None,
    ) -> None:
        _check_fate(fate, reason, delivered_seq)
        record = self._messages[seq]
        if record.fate is not None:
            raise ValueError(f"message {seq} already has a fate")
        self._messages[seq] = replace(
            record,
            fate=fate,
            drop_reason=reason,
            delivered_seq=delivered_seq,
            delivered_vt=vt if fate == "delivered" else None,
        )

    def fault_fired(self, record: FaultRecord) -> None:
        self.faults.append(record)

    def report_submitted(self, record: ReportRecord) -> None:
        self.reports.append(record)

    def actor_acted(self, record: ActorRecord) -> None:
        self.actors.append(record)

    def model_called(self, record: ModelCallRecord) -> None:
        if record.call_no != len(self.model_calls) + 1:
            raise ValueError(f"model call {record.call_no} is not the next call")
        self.model_calls.append(record)


Connection = psycopg.Connection[TupleRow]


class PgHarnessLog(HarnessLog):
    """Harness records in the run database's `harness` schema, written as `ff_harness`.

    Each record is written at once, outside any other transaction. The harness never
    crashes, so the event counter lives in memory, starting after the last stored event.
    """

    def __init__(self, conn: Connection) -> None:
        if not conn.autocommit:
            raise ValueError("the harness log writes each record at once: use autocommit")
        self._conn = conn
        row = conn.execute(
            "SELECT greatest((SELECT max(seq) FROM harness.message_log),"
            " (SELECT max(delivered_seq) FROM harness.message_log),"
            " (SELECT max(seq) FROM harness.fault_log),"
            " (SELECT max(seq) FROM harness.final_report),"
            " (SELECT max(seq) FROM harness.actor_log))"
        ).fetchone()
        self._last_seq = 0 if row is None or row[0] is None else int(row[0])
        self.run: RunRecord | None = None
        """The run as this log recorded it."""

    def run_recorded(self, record: RunRecord) -> None:
        self._conn.execute(
            "INSERT INTO harness.run (run_id, scenario_id, scenario_digest, config, contract,"
            " seed, topology, model_adapter, model, cassette_digest, parent_run_id,"
            " branch_point, params) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.run_id,
                record.scenario_id,
                record.scenario_digest,
                record.config,
                record.contract,
                record.seed,
                record.topology,
                record.model_adapter,
                record.model,
                record.cassette_digest,
                record.parent_run_id,
                record.branch_point,
                Jsonb(dict(record.params)),
            ),
        )
        self.run = record

    def run_stopped(self, vt: int) -> None:
        self._conn.execute(
            "INSERT INTO harness.run_stop (run_id, stopped_vt) SELECT run_id, %s FROM harness.run",
            (vt,),
        )

    def cassette_recorded(self, digest: str) -> None:
        updated = self._conn.execute(
            "UPDATE harness.run SET cassette_digest = %s WHERE cassette_digest IS NULL",
            (digest,),
        )
        if updated.rowcount != 1 or self.run is None:
            raise ValueError("the run was never recorded, or already names its cassette")
        self.run = replace(self.run, cassette_digest=digest)

    def next_seq(self) -> int:
        self._last_seq += 1
        return self._last_seq

    def message_sent(self, record: MessageRecord) -> None:
        if record.fate is not None:
            raise ValueError("a message has no fate when it is sent")
        self._conn.execute(
            "INSERT INTO harness.message_log (seq, hop, kind, sender, receiver, api,"
            " correlation, intent_id, idempotency_key, answer_class, body, sent_vt, copy_of)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.seq,
                record.hop,
                record.kind,
                record.sender,
                record.receiver,
                record.api,
                record.correlation,
                record.intent_id,
                record.idempotency_key,
                record.answer_class,
                Jsonb(record.body),
                record.sent_vt,
                record.copy_of,
            ),
        )

    def message_fate(
        self,
        seq: int,
        fate: Fate,
        *,
        vt: int,
        reason: DropReason | None = None,
        delivered_seq: int | None = None,
    ) -> None:
        _check_fate(fate, reason, delivered_seq)
        delivered = fate == "delivered"
        updated = self._conn.execute(
            "UPDATE harness.message_log SET fate = %s, drop_reason = %s, delivered_seq = %s,"
            " delivered_vt = %s, delivered_wall_at = CASE WHEN %s THEN now() END"
            " WHERE seq = %s AND fate IS NULL",
            (fate, reason, delivered_seq, vt if delivered else None, delivered, seq),
        )
        if updated.rowcount != 1:
            raise ValueError(f"message {seq} was never sent or already has a fate")

    def fault_fired(self, record: FaultRecord) -> None:
        self._conn.execute(
            "INSERT INTO harness.fault_log (seq, fault, variant, point, occurrence, action,"
            " target_seq, component, voided, params, vt, intent_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.seq,
                record.fault,
                record.variant,
                record.point,
                record.occurrence,
                record.action,
                record.target_seq,
                record.component,
                list(record.voided),
                Jsonb(dict(record.params)),
                record.vt,
                record.intent_id,
            ),
        )

    def report_submitted(self, record: ReportRecord) -> None:
        self._conn.execute(
            "INSERT INTO harness.final_report (seq, worker, report, valid, errors,"
            " accepted_by, message_seq, vt) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.seq,
                record.worker,
                Jsonb(record.report),
                record.valid,
                Jsonb(list(record.errors)),
                record.accepted_by,
                record.message_seq,
                record.vt,
            ),
        )

    def actor_acted(self, record: ActorRecord) -> None:
        self._conn.execute(
            "INSERT INTO harness.actor_log (seq, actor, action, subject, details, vt)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                record.seq,
                record.actor,
                record.action,
                record.subject,
                Jsonb(dict(record.details)),
                record.vt,
            ),
        )

    def model_called(self, record: ModelCallRecord) -> None:
        self._conn.execute(
            "INSERT INTO harness.model_call (call_no, worker, asked_vt, answered_vt, model,"
            " input_tokens, output_tokens, refused, wall_ms)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.call_no,
                record.worker,
                record.asked_vt,
                record.answered_vt,
                record.model,
                record.input_tokens,
                record.output_tokens,
                record.refused,
                record.wall_ms,
            ),
        )
