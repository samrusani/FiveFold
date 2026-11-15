"""The harness's records and faults of the split services' traffic (0004, 0006, 0009).

In the inproc topology the harness's transport carries every message, logs it with its
fate and fires the plan's faults on it. In the split topology messages travel over real
HTTP connections, so each service logs the messages it sees and fires the faults at the
points it owns (`fivefold.harness.split.faults`). The log is the same harness log, with
these readings of when a message is sent and delivered:

- A message from the user's agent, which the harness cannot watch, is logged when it
  arrives: sent and delivered at once, at the same virtual time.
- An answer a service sends to the agent is logged as sent, then as delivered when the
  service hands it to its HTTP server, before the server writes it: the harness cannot
  see whether the agent read it. The simulator first asks whether the agent's connection
  is still open, and logs the answer as dropped (`caller_timed_out`) if it is not. The
  gateway's answers go through the MCP SDK, which writes them itself, so the gateway logs
  each one as delivered when its handler returns, even if the agent has gone. In G1 and
  G2 the evaluator reads no answer on the agent's hop as evidence (0004), so this changes
  no verdict there.
- On the `gw_sim` hop the gateway logs each request as it sends it, and each answer as it
  arrives; the simulator logs each request as it arrives, and each answer as it sends it.
  A request carries the seq the gateway logged it under, and an answer the seq the
  simulator logged it under, in the header `fivefold-seq`.
- A write request is sent at the time its attempt records, `gw.attempt.sent_vt` (0003),
  the time the gateway's settle bounds start from (0005). The gateway records the
  attempt in a transaction, then logs the request, and on the host's clock milliseconds
  pass in between; the log takes the attempt's time, so the evaluator's bounds start from
  the same number (0004). A message the simulator logs meanwhile can then have a lower
  seq and a later `sent_vt`. Nothing reads the log as if virtual time grew with seq: the
  evaluator compares times within one request's evidence, recovery latency compares a
  fault's time with its evidence's deliveries and attestations, the viewer orders events
  by time, and the log's own checks ask only that a message is delivered no earlier than
  it is sent, and that a copy repeats its request's time.
- A message whose fate nobody logged, because its receiver never took it, is dropped when
  the run ends (`fivefold.harness.split.lifecycle`).
- A message from the agent that holds a NUL character is refused before it is logged
  (`holds_nul`): PostgreSQL's JSONB cannot store one. The simulator answers 422, as for a
  malformed body, and the gateway answers invalid params.

What a fault does where it fires (0006):

- DROP: the message never reaches its receiver. A service that drops a request it
  received holds the connection open until the caller gives up, so the caller sees no
  answer, as when a request is lost. The gateway, dropping an answer, waits until its
  call's timeout ends, as when an answer is lost.
- DUPLICATE (F4): the service handles the request a second time, `copy_delay_ms` after
  the first delivery, as a copy that names the request in `copy_of`. The copy's answer is
  dropped with reason `answer_to_copy`.
- REVOKE and CHANGE_QUOTE (F6, F7): applied through the simulator's control interface
  before the message goes on, by the simulator's service, which owns that interface.

Each fault's record names the message it acted on and that message's intent, which
recovery latency starts from (0014). The log stamps each delivery with the database's
wall-clock time (`delivered_wall_at`), as it stamps each message it writes (`wall_at`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

import psycopg
from psycopg.rows import TupleRow

from fivefold.domain.api import ApiName
from fivefold.faults.catalog import Action
from fivefold.faults.plan import FaultInjector, Fired
from fivefold.harness.log import (
    DropReason,
    FaultRecord,
    HarnessLog,
    JsonValue,
    Kind,
    MessageRecord,
)
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Envelope, Hop

SEQ_HEADER = "fivefold-seq"
"""The header that carries a message's seq in the harness log between the services."""

WRITES = frozenset({str(ApiName.SUBMIT_PURCHASE_ORDER), str(ApiName.RESERVE_INVENTORY)})

Effect = Callable[[Fired, Envelope], None]
"""Applies a fault that changes the simulator's state (F6, F7) for the message it hit."""


NUL_REFUSED = "it holds a NUL character (\\u0000), which the harness's log cannot store"
"""Why a service refuses a message holding a NUL, before logging it."""


def holds_nul(value: object) -> bool:
    """Whether a JSON value holds a NUL character, in a string or a key. PostgreSQL's JSONB
    cannot store one, so neither can the harness's log: a service refuses such a message
    before it logs it, as it refuses a malformed one."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return True
        elif isinstance(item, Mapping):
            for key, inner in item.items():
                if isinstance(key, str) and "\x00" in key:
                    return True
                pending.append(inner)
        elif isinstance(item, list | tuple):
            pending.extend(item)
    return False


def answer_class(api: str | None, body: object) -> str | None:
    """The answer class the log records for a simulator answer: a write's `answer`
    (`committed`, `rejected` or `in_progress`), and None for every other call, as the
    inproc endpoint records it (`fivefold.sim.endpoint`)."""
    if api in WRITES and isinstance(body, Mapping):
        found = body.get("answer")
        return None if found is None else str(found)
    return None


@dataclass(frozen=True)
class Outcome:
    """What the faults that fired at a message do to it."""

    dropped: bool
    copies: tuple[Fired, ...] = ()
    """F4 entries that fired: each delivers the message once more."""


class Hops:
    """One service's records of its traffic, and the faults at the points it owns."""

    def __init__(
        self,
        scheduler: Scheduler,
        log: HarnessLog,
        faults: FaultInjector,
        *,
        effect: Effect | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._log = log
        self._faults = faults
        self._effect = effect

    def sent(
        self,
        *,
        hop: Hop,
        kind: Kind,
        sender: str,
        receiver: str,
        api: str | None,
        body: JsonValue,
        intent_id: str | None = None,
        idempotency_key: str | None = None,
        correlation: int | None = None,
        answer_class: str | None = None,
        sent_vt: int | None = None,
    ) -> Envelope:
        """Log a message as sent, with no fate yet; return it as its receiver gets it.

        It is sent now, unless `sent_vt` gives the earlier time at which its sender
        recorded the send: the gateway's write attempt (`SimulatorLink`).
        """
        kept = json.loads(json.dumps(body, sort_keys=True))
        seq = self._log.next_seq()
        now = self._scheduler.now
        if sent_vt is None:
            sent_vt = now
        elif sent_vt > now:
            raise ValueError(f"a message logged at {now} cannot be sent later, at {sent_vt}")
        envelope = Envelope(
            seq=seq,
            hop=hop,
            kind=kind,
            sender=sender,
            receiver=receiver,
            api=api,
            correlation=correlation,
            intent_id=intent_id,
            idempotency_key=idempotency_key,
            answer_class=answer_class,
            body=kept,
            sent_vt=sent_vt,
        )
        self._log.message_sent(_record(envelope))
        return envelope

    def delivered(self, envelope: Envelope) -> None:
        """Log that `envelope` reached its receiver now."""
        self._log.message_fate(
            envelope.seq, "delivered", vt=self._scheduler.now, delivered_seq=self._log.next_seq()
        )

    def dropped(self, envelope: Envelope, reason: DropReason) -> None:
        """Log that `envelope` never reached its receiver, and why."""
        self._log.message_fate(envelope.seq, "dropped", vt=self._scheduler.now, reason=reason)

    def fire(self, point: str, envelope: Envelope) -> Outcome:
        """Reach `point` with `envelope`; record and apply the faults that fire there.

        A plan entry matches on the call (as `api` or `tool`), the answer class and the
        intent, as the inproc transport's points do. Faults that change the simulator
        apply here, before the message goes on.
        """
        fields = {
            "api": envelope.api,
            "tool": envelope.api,
            "answer": envelope.answer_class,
            "intent_id": envelope.intent_id,
        }
        dropped = False
        copies: list[Fired] = []
        for fired in self._faults.at(point, fields):
            action = fired.planned.kind.action
            if action is Action.DUPLICATE:
                copies.append(fired)
                continue
            self._record(fired, point, envelope)
            if action is Action.DROP:
                dropped = True
            elif self._effect is not None:
                self._effect(fired, envelope)
            else:
                raise RuntimeError(f"{fired.fault} fired at {point} with nothing to apply it")
        return Outcome(dropped, tuple(copies))

    def copy(self, fired: Fired, point: str, original: Envelope) -> Envelope:
        """Log F4's copy of the request `original`, and the fault that made it (0006).

        The copy repeats the request field for field, with its own seq and `copy_of`
        naming the request. It is due `copy_delay_ms` after the request's delivery.
        """
        fault_seq = self._log.next_seq()
        copy = replace(original, seq=self._log.next_seq(), copy_of=original.seq)
        self._log.fault_fired(
            FaultRecord(
                seq=fault_seq,
                fault=str(fired.fault),
                variant=fired.planned.variant,
                point=point,
                occurrence=fired.occurrence,
                action=str(Action.DUPLICATE),
                vt=self._scheduler.now,
                target_seq=original.seq,
                params={**fired.planned.params, "copy_seq": copy.seq},
                intent_id=original.intent_id,
            )
        )
        self._log.message_sent(_record(copy))
        return copy

    def _record(self, fired: Fired, point: str, envelope: Envelope) -> None:
        self._log.fault_fired(
            FaultRecord(
                seq=self._log.next_seq(),
                fault=str(fired.fault),
                variant=fired.planned.variant,
                point=point,
                occurrence=fired.occurrence,
                action=str(fired.planned.kind.action),
                vt=self._scheduler.now,
                target_seq=envelope.seq,
                params=dict(fired.planned.params),
                intent_id=envelope.intent_id,
            )
        )


def _record(envelope: Envelope) -> MessageRecord:
    return MessageRecord(
        seq=envelope.seq,
        hop=envelope.hop,
        kind=envelope.kind,
        sender=envelope.sender,
        receiver=envelope.receiver,
        api=envelope.api,
        correlation=envelope.correlation,
        intent_id=envelope.intent_id,
        idempotency_key=envelope.idempotency_key,
        answer_class=envelope.answer_class,
        body=envelope.body,
        sent_vt=envelope.sent_vt,
        copy_of=envelope.copy_of,
    )


def logged_message(conn: psycopg.Connection[TupleRow], seq: int) -> tuple[Envelope, bool] | None:
    """The message logged under `seq`, as an envelope, and whether it has a fate yet; None
    if there is none. Reads the harness log as `ff_harness`."""
    row = conn.execute(
        "SELECT hop, kind, sender, receiver, api, correlation, intent_id, idempotency_key,"
        " answer_class, body, sent_vt, copy_of, fate FROM harness.message_log WHERE seq = %s",
        (seq,),
    ).fetchone()
    if row is None:
        return None
    envelope = Envelope(
        seq=seq,
        hop=row[0],
        kind=row[1],
        sender=str(row[2]),
        receiver=str(row[3]),
        api=None if row[4] is None else str(row[4]),
        correlation=None if row[5] is None else int(row[5]),
        intent_id=None if row[6] is None else str(row[6]),
        idempotency_key=None if row[7] is None else str(row[7]),
        answer_class=None if row[8] is None else str(row[8]),
        body=row[9],
        sent_vt=int(row[10]),
        copy_of=None if row[11] is None else int(row[11]),
    )
    return envelope, row[12] is not None
