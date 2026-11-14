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
    """The component a crash stopped, if any."""
    voided: tuple[int, ...] = ()
    """The seqs of messages whose delivery the fault voided (0006, crashes)."""
    params: Mapping[str, object] = field(default_factory=dict)


class HarnessLog(ABC):
    """Numbers harness events and records messages and faults."""

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
            " (SELECT max(seq) FROM harness.fault_log))"
        ).fetchone()
        self._last_seq = 0 if row is None or row[0] is None else int(row[0])

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
