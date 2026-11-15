"""Reads and writes of the `sim` schema inside one open transaction.

`Ledger` holds the SQL; `service.py` decides what to write. Every method runs in the
caller's transaction, which has already locked the run's row, so the simulator handles
one request at a time (0004). The schema checks much of what is written here
(`db/migrations/0004_sim.sql`); this module only maps rows to plain values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, LiteralString, cast

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from fivefold.domain.authority import ApprovalStatus, Decision, approval_status_at
from fivefold.domain.rules import BUSINESS_ARGS, FactValue, Operation

Connection = psycopg.Connection[TupleRow]
Row = tuple[Any, ...]
JsonObject = dict[str, JsonValue]
Args = dict[str, str | int]

EventKind = Literal[
    "setup",
    "read",
    "fact_changed",
    "write_received",
    "write_committed",
    "write_replayed",
    "write_in_progress",
    "write_rejected",
    "write_aborted",
    "approval_requested",
    "approval_repeated",
    "approval_approved",
    "approval_denied",
    "approval_revoked",
    "approval_status",
    "status",
]
"""The kinds of `sim.event` rows."""

WriteState = Literal["received", "committed", "replayed", "in_progress", "rejected", "aborted"]
StoredStatus = Literal["pending", "approved", "denied", "revoked"]
Counter = Literal["po", "rs", "apr"]

_NEXT_NUMBER: dict[Counter, LiteralString] = {
    "po": "UPDATE sim.run SET last_po_no = last_po_no + 1 RETURNING last_po_no",
    "rs": "UPDATE sim.run SET last_rs_no = last_rs_no + 1 RETURNING last_rs_no",
    "apr": "UPDATE sim.run SET last_apr_no = last_apr_no + 1 RETURNING last_apr_no",
}

_APPROVAL: LiteralString = (
    "SELECT approval_id, intent_id, operation, scope, client_request_key, requester,"
    " requested_seq, requested_vt, status, decided_seq, decided_vt, decided_by, expires_at,"
    " revoked_seq, revoked_vt, revoked_by, effect_id FROM sim.approval"
)
_WRITE: LiteralString = (
    "SELECT received_seq, received_vt, caller, operation, intent_id, approval_id, args, basis,"
    " idempotency_key, key_honoured, client_ref, deadline_vt, deadline_waived, state,"
    " outcome_seq, outcome_vt, effect_id FROM sim.write"
)
_EFFECT: LiteralString = (
    "SELECT effect_id, intent_id, operation, args, approval_id, basis, idempotency_key,"
    " client_ref, received_seq, commit_seq, committed_vt FROM sim.effect"
)


class LedgerError(RuntimeError):
    """A row holds something the schema should have refused."""


def json_object(value: object) -> JsonObject:
    """A jsonb object as loaded by psycopg."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise LedgerError(f"expected a JSON object, found {value!r}")
    return cast(JsonObject, value)


def args_of(value: object) -> Args:
    """Business arguments: text or integers."""
    loaded = json_object(value)
    for key, item in loaded.items():
        if isinstance(item, bool) or not isinstance(item, str | int):
            raise LedgerError(f"argument {key} must be text or an integer, found {item!r}")
    return cast(Args, loaded)


def business_args_of(operation: Operation, value: object) -> Args:
    """An operation's business arguments, in record 0001's order."""
    loaded = args_of(value)
    names = BUSINESS_ARGS[operation]
    if set(loaded) != set(names):
        raise LedgerError(f"{operation} takes the arguments {names}, not {sorted(loaded)}")
    return {name: loaded[name] for name in names}


def basis_of(value: object) -> dict[str, int]:
    """A basis: fact key to version."""
    loaded = json_object(value)
    for key, item in loaded.items():
        if isinstance(item, bool) or not isinstance(item, int):
            raise LedgerError(f"the version of {key} must be an integer, found {item!r}")
    return cast(dict[str, int], loaded)


@dataclass(frozen=True)
class ApprovalRow:
    """One row of `sim.approval`."""

    approval_id: str
    intent_id: str
    operation: Operation
    scope: Args
    client_request_key: str | None
    requester: str
    requested_seq: int
    requested_vt: int
    status: StoredStatus
    decided_seq: int | None
    decided_vt: int | None
    decided_by: str | None
    expires_at: int | None
    revoked_seq: int | None
    revoked_vt: int | None
    revoked_by: str | None
    effect_id: str | None

    def status_at(self, vt: int) -> ApprovalStatus:
        """The status the approvals service reports at `vt` (0002, rules 8 and 9)."""
        decision = None
        if self.status in ("approved", "revoked"):
            decision = Decision.APPROVE
        elif self.status == "denied":
            decision = Decision.DENY
        return approval_status_at(
            decision=decision,
            revoked=self.status == "revoked",
            expires_at=self.expires_at,
            vt=vt,
        )


@dataclass(frozen=True)
class WriteRow:
    """One row of `sim.write`."""

    received_seq: int
    received_vt: int
    caller: str
    operation: Operation
    intent_id: str
    approval_id: str
    args: Args
    basis: dict[str, int]
    idempotency_key: str | None
    key_honoured: bool
    client_ref: str | None
    deadline_vt: int
    deadline_waived: bool
    state: WriteState
    outcome_seq: int | None
    outcome_vt: int | None
    effect_id: str | None


@dataclass(frozen=True)
class NewWrite:
    """What a write request asks for, as it reaches the destination."""

    received_seq: int
    caller: str
    operation: Operation
    intent_id: str
    approval_id: str
    args: Args
    basis: dict[str, int]
    idempotency_key: str | None
    key_honoured: bool
    client_ref: str | None
    deadline_vt: int
    deadline_waived: bool


@dataclass(frozen=True)
class EffectRow:
    """One row of `sim.effect`."""

    effect_id: str
    intent_id: str
    operation: Operation
    args: Args
    approval_id: str
    basis: dict[str, int]
    idempotency_key: str | None
    client_ref: str | None
    received_seq: int
    commit_seq: int
    committed_vt: int


def _approval(row: Row) -> ApprovalRow:
    return ApprovalRow(
        approval_id=str(row[0]),
        intent_id=str(row[1]),
        operation=Operation(row[2]),
        scope=args_of(row[3]),
        client_request_key=row[4],
        requester=str(row[5]),
        requested_seq=int(row[6]),
        requested_vt=int(row[7]),
        status=cast(StoredStatus, str(row[8])),
        decided_seq=row[9],
        decided_vt=row[10],
        decided_by=row[11],
        expires_at=row[12],
        revoked_seq=row[13],
        revoked_vt=row[14],
        revoked_by=row[15],
        effect_id=row[16],
    )


def _write(row: Row) -> WriteRow:
    return WriteRow(
        received_seq=int(row[0]),
        received_vt=int(row[1]),
        caller=str(row[2]),
        operation=Operation(row[3]),
        intent_id=str(row[4]),
        approval_id=str(row[5]),
        args=business_args_of(Operation(row[3]), row[6]),
        basis=basis_of(row[7]),
        idempotency_key=row[8],
        key_honoured=bool(row[9]),
        client_ref=row[10],
        deadline_vt=int(row[11]),
        deadline_waived=bool(row[12]),
        state=cast(WriteState, str(row[13])),
        outcome_seq=row[14],
        outcome_vt=row[15],
        effect_id=row[16],
    )


def _effect(row: Row) -> EffectRow:
    return EffectRow(
        effect_id=str(row[0]),
        intent_id=str(row[1]),
        operation=Operation(row[2]),
        args=business_args_of(Operation(row[2]), row[3]),
        approval_id=str(row[4]),
        basis=basis_of(row[5]),
        idempotency_key=row[6],
        client_ref=row[7],
        received_seq=int(row[8]),
        commit_seq=int(row[9]),
        committed_vt=int(row[10]),
    )


class Ledger:
    """The `sim` tables, inside one open transaction at virtual time `vt`."""

    def __init__(self, conn: Connection, vt: int) -> None:
        self._conn = conn
        self.vt = vt
        self.aborted: list[int] = []
        """Writes this transaction aborted for missing their deadline, by received seq."""

    def _one(self, query: LiteralString, params: tuple[object, ...]) -> Row:
        row = self._conn.execute(query, params).fetchone()
        if row is None:
            raise LedgerError(f"no row for: {query}")
        return row

    # Events and counters ---------------------------------------------------------------

    def event(self, kind: EventKind, actor: str, ref: str | None) -> int:
        """Record an event at this transaction's time and return its seq."""
        row = self._one(
            "INSERT INTO sim.event (vt, kind, actor, ref) VALUES (%s, %s, %s, %s) RETURNING seq",
            (self.vt, kind, actor, ref),
        )
        return int(row[0])

    def next_number(self, counter: Counter) -> int:
        """The next number of a per-run id counter: PO-, RS- or APR-."""
        return int(self._one(_NEXT_NUMBER[counter], ())[0])

    # Facts -----------------------------------------------------------------------------

    def latest_fact(self, key: str) -> tuple[int, JsonObject] | None:
        """The latest version of a fact and its value, or None if there is no such fact."""
        row = self._conn.execute(
            "SELECT version, value FROM sim.fact_version WHERE key = %s"
            " ORDER BY version DESC LIMIT 1",
            (key,),
        ).fetchone()
        return None if row is None else (int(row[0]), json_object(row[1]))

    def fact_at(self, key: str, version: int) -> JsonObject | None:
        """A fact's value at one version, or None if that version does not exist."""
        row = self._conn.execute(
            "SELECT value FROM sim.fact_version WHERE key = %s AND version = %s", (key, version)
        ).fetchone()
        return None if row is None else json_object(row[0])

    def add_fact_version(
        self,
        key: str,
        version: int,
        value: Mapping[str, object],
        seq: int,
        *,
        cause: Literal["initial", "effect", "control"],
        effect_id: str | None = None,
        actor: str | None = None,
    ) -> None:
        """Append a version of a fact, valid from event `seq`."""
        self._conn.execute(
            "INSERT INTO sim.fact_version (key, version, value, valid_from_seq, valid_from_vt,"
            " cause, cause_effect_id, cause_actor) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (key, version, Jsonb(dict(value)), seq, self.vt, cause, effect_id, actor),
        )

    def log_read(self, seq: int, key: str, version: int, caller: str) -> None:
        """Record a read served at event `seq`."""
        self._conn.execute(
            "INSERT INTO sim.read_log (seq, key, version, caller, vt) VALUES (%s, %s, %s, %s, %s)",
            (seq, key, version, caller, self.vt),
        )

    def served_before(self, key: str, version: int, seq: int) -> bool:
        """Whether this version of the fact was served to the run before event `seq`."""
        row = self._one(
            "SELECT EXISTS (SELECT 1 FROM sim.read_log WHERE key = %s AND version = %s"
            " AND seq < %s)",
            (key, version, seq),
        )
        return bool(row[0])

    # Approvals -------------------------------------------------------------------------

    def approval(self, approval_id: str) -> ApprovalRow | None:
        """One approval, or None."""
        row = self._conn.execute(_APPROVAL + " WHERE approval_id = %s", (approval_id,)).fetchone()
        return None if row is None else _approval(row)

    def approval_by_request_key(self, client_request_key: str) -> ApprovalRow | None:
        """The approval filed with this client request key, or None."""
        row = self._conn.execute(
            _APPROVAL + " WHERE client_request_key = %s", (client_request_key,)
        ).fetchone()
        return None if row is None else _approval(row)

    def pending_approvals(self) -> list[ApprovalRow]:
        """Approvals waiting for a decision, in the order they were requested."""
        rows = self._conn.execute(
            _APPROVAL + " WHERE status = 'pending' ORDER BY requested_seq"
        ).fetchall()
        return [_approval(row) for row in rows]

    def insert_approval(
        self,
        approval_id: str,
        *,
        intent_id: str,
        operation: Operation,
        scope: Mapping[str, object],
        client_request_key: str | None,
        requester: str,
        requested_seq: int,
    ) -> None:
        """File a pending approval request."""
        self._conn.execute(
            "INSERT INTO sim.approval (approval_id, intent_id, operation, scope,"
            " client_request_key, requester, requested_seq, requested_vt, status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')",
            (
                approval_id,
                intent_id,
                str(operation),
                Jsonb(dict(scope)),
                client_request_key,
                requester,
                requested_seq,
                self.vt,
            ),
        )

    def add_approval_event(
        self,
        seq: int,
        approval_id: str,
        kind: Literal["requested", "approved", "denied", "revoked"],
        actor: str,
    ) -> None:
        """Append an approval event at event `seq`."""
        self._conn.execute(
            "INSERT INTO sim.approval_event (seq, approval_id, kind, actor, vt)"
            " VALUES (%s, %s, %s, %s, %s)",
            (seq, approval_id, kind, actor, self.vt),
        )

    def set_decision(
        self,
        approval_id: str,
        status: Literal["approved", "denied"],
        *,
        seq: int,
        actor: str,
        expires_at: int | None,
    ) -> None:
        """Record a decision on a pending approval."""
        self._conn.execute(
            "UPDATE sim.approval SET status = %s, decided_seq = %s, decided_vt = %s,"
            " decided_by = %s, expires_at = %s WHERE approval_id = %s",
            (status, seq, self.vt, actor, expires_at, approval_id),
        )

    def set_revocation(self, approval_id: str, *, seq: int, actor: str) -> None:
        """Record the revocation of an approved approval."""
        self._conn.execute(
            "UPDATE sim.approval SET status = 'revoked', revoked_seq = %s, revoked_vt = %s,"
            " revoked_by = %s WHERE approval_id = %s",
            (seq, self.vt, actor, approval_id),
        )

    def set_backed_effect(self, approval_id: str, effect_id: str) -> None:
        """Record the effect an approval backs: the first that cites it (0002, rule 10)."""
        self._conn.execute(
            "UPDATE sim.approval SET effect_id = %s WHERE approval_id = %s",
            (effect_id, approval_id),
        )

    # Writes and effects ----------------------------------------------------------------

    def write(self, received_seq: int) -> WriteRow | None:
        """One write request, or None."""
        row = self._conn.execute(_WRITE + " WHERE received_seq = %s", (received_seq,)).fetchone()
        return None if row is None else _write(row)

    def insert_write(
        self,
        write: NewWrite,
        *,
        state: WriteState = "received",
        outcome_seq: int | None = None,
        effect_id: str | None = None,
    ) -> None:
        """Record a write request that reached the destination, with its outcome if decided.

        Only a replay or `in_progress` is decided on arrival; anything else is in flight.
        """
        self._conn.execute(
            "INSERT INTO sim.write (received_seq, received_vt, caller, operation, intent_id,"
            " approval_id, args, basis, idempotency_key, key_honoured, client_ref, deadline_vt,"
            " deadline_waived, state, outcome_seq, outcome_vt, effect_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                write.received_seq,
                self.vt,
                write.caller,
                str(write.operation),
                write.intent_id,
                write.approval_id,
                Jsonb(dict(write.args)),
                Jsonb(dict(write.basis)),
                write.idempotency_key,
                write.key_honoured,
                write.client_ref,
                write.deadline_vt,
                write.deadline_waived,
                state,
                outcome_seq,
                None if outcome_seq is None else self.vt,
                effect_id,
            ),
        )

    def set_write_outcome(
        self,
        received_seq: int,
        state: WriteState,
        *,
        outcome_seq: int,
        effect_id: str | None = None,
        rejection: Mapping[str, object] | None = None,
    ) -> None:
        """Record the outcome of a write that was in flight."""
        self._conn.execute(
            "UPDATE sim.write SET state = %s, outcome_seq = %s, outcome_vt = %s, effect_id = %s,"
            " rejection = %s WHERE received_seq = %s",
            (
                state,
                outcome_seq,
                self.vt,
                effect_id,
                None if rejection is None else Jsonb(dict(rejection)),
                received_seq,
            ),
        )

    def key_in_flight(self, idempotency_key: str) -> bool:
        """Whether a write with this honoured key is in flight."""
        row = self._one(
            "SELECT EXISTS (SELECT 1 FROM sim.write WHERE idempotency_key = %s"
            " AND key_honoured AND state = 'received')",
            (idempotency_key,),
        )
        return bool(row[0])

    def overdue_writes(self) -> list[WriteRow]:
        """Writes in flight past their deadline, unless a fault waived it, oldest first."""
        rows = self._conn.execute(
            _WRITE + " WHERE state = 'received' AND NOT deadline_waived AND deadline_vt < %s"
            " ORDER BY received_seq",
            (self.vt,),
        ).fetchall()
        return [_write(row) for row in rows]

    def effect_by_key(self, idempotency_key: str) -> EffectRow | None:
        """The effect that stored this idempotency key, or None."""
        row = self._conn.execute(
            _EFFECT + " WHERE idempotency_key = %s", (idempotency_key,)
        ).fetchone()
        return None if row is None else _effect(row)

    def effects_for(self, intent_id: str, operation: Operation, up_to_vt: int) -> list[EffectRow]:
        """Effects of one intent and operation committed at or before `up_to_vt`."""
        rows = self._conn.execute(
            _EFFECT + " WHERE intent_id = %s AND operation = %s AND committed_vt <= %s"
            " ORDER BY commit_seq",
            (intent_id, str(operation), up_to_vt),
        ).fetchall()
        return [_effect(row) for row in rows]

    def insert_effect(self, effect_id: str, write: WriteRow, *, commit_seq: int) -> None:
        """Record an effect that commits `write` at event `commit_seq`."""
        self._conn.execute(
            "INSERT INTO sim.effect (effect_id, intent_id, operation, args, approval_id, basis,"
            " idempotency_key, client_ref, received_seq, commit_seq, committed_vt)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                effect_id,
                write.intent_id,
                str(write.operation),
                Jsonb(dict(write.args)),
                write.approval_id,
                Jsonb(dict(write.basis)),
                write.idempotency_key if write.key_honoured else None,
                write.client_ref,
                write.received_seq,
                commit_seq,
                self.vt,
            ),
        )


class History:
    """The facts as a basis check at commit reads them (`rules.FactHistory`).

    Reads count only if served before the write reached the simulator (0004, property 3).
    """

    def __init__(self, ledger: Ledger, received_seq: int) -> None:
        self._ledger = ledger
        self._received_seq = received_seq

    def value_at(self, key: str, version: int) -> FactValue | None:
        return self._ledger.fact_at(key, version)

    def current(self, key: str) -> tuple[int, FactValue] | None:
        return self._ledger.latest_fact(key)

    def was_served(self, key: str, version: int) -> bool:
        return self._ledger.served_before(key, version, self._received_seq)
