"""The harness's records of a run: messages, faults, scripted humans and reports (0004).

The evaluator scores from these records and the simulator's, never from the gateway's
(0004). One counter numbers every harness event in order: a message's send and its
delivery, a fault firing, an action of a scripted human and a submitted report. So "this
message was delivered before that report" is a comparison of two numbers.

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
DropReason = Literal["fault", "receiver_down", "caller_timed_out", "caller_crashed"]


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


class HarnessLog(ABC):
    """Numbers harness events and records messages, faults and reports."""

    @abstractmethod
    def run_recorded(self, record: RunRecord) -> None:
        """Record the run itself, once, before anything happens in it."""

    @abstractmethod
    def run_stopped(self, vt: int) -> None:
        """Record when the run stopped, once, at the end of the drain window (0009)."""

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

    def run_recorded(self, record: RunRecord) -> None:
        self._conn.execute(
            "INSERT INTO harness.run (run_id, scenario_id, scenario_digest, config, contract,"
            " seed, topology, model_adapter, model, params)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                Jsonb(dict(record.params)),
            ),
        )

    def run_stopped(self, vt: int) -> None:
        self._conn.execute(
            "INSERT INTO harness.run_stop (run_id, stopped_vt) SELECT run_id, %s FROM harness.run",
            (vt,),
        )

    def next_seq(self) -> int:
        self._last_seq += 1
        return self._last_seq

    def message_sent(self, record: MessageRecord) -> None:
        if record.fate is not None:
            raise ValueError("a message has no fate when it is sent")
        self._conn.execute(
            "INSERT INTO harness.message_log (seq, hop, kind, sender, receiver, api,"
            " correlation, intent_id, idempotency_key, answer_class, body, sent_vt)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
        updated = self._conn.execute(
            "UPDATE harness.message_log SET fate = %s, drop_reason = %s, delivered_seq = %s,"
            " delivered_vt = %s WHERE seq = %s AND fate IS NULL",
            (fate, reason, delivered_seq, vt if fate == "delivered" else None, seq),
        )
        if updated.rowcount != 1:
            raise ValueError(f"message {seq} was never sent or already has a fate")

    def fault_fired(self, record: FaultRecord) -> None:
        self._conn.execute(
            "INSERT INTO harness.fault_log (seq, fault, variant, point, occurrence, action,"
            " target_seq, component, voided, params, vt)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
