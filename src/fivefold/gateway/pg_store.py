"""The gateway's records for one run, in the `gw` schema of the run's database (0003, 0008).

`PgStore` runs the operations of `ActionStore` (`store.py`) in PostgreSQL transactions.
It connects as the gateway's role, `ff_gw`, which can write only the `gw` schema and has
no UPDATE or DELETE privilege on `action_event`. Every operation that writes is one
transaction: it locks the rows it changes in the order `store.py` describes, writes the
step's change and its events, checks every invariant on what the transaction sees, and
commits. Reads take no locks and see one consistent state. Nothing in a transaction calls
the network (0003): the store only reads and writes rows.

One gateway serves a run. Its store holds a session advisory lock on the run database,
so a second gateway cannot start recovery under a live one (0003, startup recovery).
The gateway can open further stores on the same run with `exclusive` off, for example
one per dispatcher worker. A store is one connection: use it from one thread at a time,
as the deterministic scheduler does (0009).

The schema itself enforces part of I1, I2, I4, I6 and I7 (`db/migrations/0002_gateway.sql`),
so even a store with a bug cannot write some broken records.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from types import TracebackType
from typing import Any, LiteralString

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from fivefold.domain.contracts import Contract
from fivefold.gateway.codec import (
    args_from_json,
    basis_to_json,
    check_from_columns,
    mapping_from_json,
    result_from_json,
    result_to_json,
    to_json,
    versions_from_json,
)
from fivefold.gateway.records import (
    Action,
    ActionEvent,
    ActionResult,
    Attempt,
    Escalation,
    Grant,
    Refusal,
    ReportAccepted,
    Resolution,
    Snapshot,
    ToolCall,
    frozen,
)
from fivefold.gateway.state_machine import (
    HOLDING_STATES,
    ActionState,
    AnswerClass,
    EventKind,
    GatewaySettings,
    GrantState,
    Note,
    Reason,
)
from fivefold.gateway.steps import ActionView, Change
from fivefold.gateway.store import ActionStore, Transaction, UnknownAction

Connection = psycopg.Connection[TupleRow]
Row = tuple[Any, ...]

_ACTION: LiteralString = (
    "SELECT action_id, task_id, intent_id, operation, target, args, action_key, identity,"
    " contract, basis, state, grant_id, idempotency_key, effect_id, created_vt, updated_vt"
    " FROM gw.action"
)
_GRANT: LiteralString = (
    'SELECT grant_id, action_id, approval_id, state, requested_vt, consumed_vt FROM gw."grant"'
)
_ATTEMPT: LiteralString = (
    "SELECT attempt_id, action_id, attempt_no, idempotency_key, grant_id, check_kind,"
    " check_approval, check_basis, check_seen_seq, check_vt, sent_vt, answer_class,"
    " answer_ref, answered_vt FROM gw.attempt"
)
_EVENT: LiteralString = (
    "SELECT seq, action_id, kind, from_state, to_state, reason, details, vt FROM gw.action_event"
)
_ESCALATION: LiteralString = (
    "SELECT escalation_id, action_id, opened_vt, resolved_vt, resolution, effect_id, actor"
    " FROM gw.escalation"
)
_TOOL_CALL: LiteralString = (
    "SELECT call_no, tool, action_id, result, as_of_seq, vt FROM gw.tool_call"
)
_IN_ACTION_ORDER: LiteralString = " ORDER BY substring(action_id FROM 3)::bigint"

GATEWAY_LOCK_KEY = 5_000_006
"""Session advisory lock a gateway holds on its run database while it runs."""


class IntentMismatch(ValueError):
    """The database already holds different intents for this run."""


class GatewayBusy(RuntimeError):
    """Another gateway already holds this run database."""


class PgStore(ActionStore):
    """One run's gateway records in PostgreSQL. Close it, or use it as a context manager."""

    def __init__(
        self,
        conninfo: str,
        *,
        task_id: str,
        intents: Mapping[str, str],
        contract: Contract,
        settings: GatewaySettings,
        identity: bool = True,
        exclusive: bool = True,
    ) -> None:
        """Connect, claim the run, and record or check the run's intents.

        With `exclusive` off, the store is a further connection of a gateway that already
        holds the run, for example a second worker of its dispatcher. It checks the run's
        intents but never records them, so only the store that holds the run writes them
        and two stores cannot both find the run empty and record different intents.
        """
        super().__init__(task_id=task_id, contract=contract, settings=settings, identity=identity)
        self._holds_run = False
        self._conn: Connection = psycopg.connect(conninfo, autocommit=True)
        try:
            self._start(intents, exclusive=exclusive)
        except BaseException:
            self.close()
            raise

    def _start(self, intents: Mapping[str, str], *, exclusive: bool) -> None:
        if exclusive:
            row = self._conn.execute(
                "SELECT pg_try_advisory_lock(%s)", (GATEWAY_LOCK_KEY,)
            ).fetchone()
            if row != (True,):
                raise GatewayBusy("another gateway holds this run database")
            self._holds_run = True
        with self._conn.transaction():
            rows = self._conn.execute("SELECT intent_id, operation FROM gw.intent").fetchall()
            stored = {str(row[0]): str(row[1]) for row in rows}
            if not stored and exclusive:
                for intent_id, operation in sorted(intents.items()):
                    self._conn.execute(
                        "INSERT INTO gw.intent (intent_id, operation) VALUES (%s, %s)",
                        (intent_id, operation),
                    )
            elif not stored:
                raise IntentMismatch("the run has no intents yet; its gateway records them")
            elif stored != dict(intents):
                raise IntentMismatch(f"the run's intents are {stored}, not {dict(intents)}")
            row = self._conn.execute(
                "SELECT greatest((SELECT max(vt) FROM gw.action_event),"
                " (SELECT max(vt) FROM gw.tool_call))"
            ).fetchone()
        self._vt = 0 if row is None or row[0] is None else int(row[0])

    def close(self) -> None:
        """Release the run, if this store holds it, and close the connection.

        The run is released before this returns, so another gateway can start at once.
        Closing twice does nothing.
        """
        if self._holds_run:
            self._holds_run = False
            # If the unlock fails, the connection is unusable; closing it still releases
            # the lock, only not before this returns.
            with suppress(psycopg.Error):
                self._conn.execute("SELECT pg_advisory_unlock(%s)", (GATEWAY_LOCK_KEY,))
        self._conn.close()

    def counters(self) -> tuple[int, int, int]:
        """The run's counters: the last action number, event seq and response number."""
        row = self._conn.execute(
            "SELECT last_action_no, last_seq, last_call_no FROM gw.run_state"
        ).fetchone()
        if row is None:
            raise LookupError("gw.run_state has no row; is the gateway migration applied?")
        return int(row[0]), int(row[1]), int(row[2])

    def __enter__(self) -> PgStore:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[Transaction]:
        with self._conn.transaction():
            yield _PgTransaction(self._conn, lock=True)

    @contextmanager
    def _reading(self) -> Iterator[Transaction]:
        with self._conn.transaction():
            self._conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield _PgTransaction(self._conn, lock=False)


class _PgTransaction(Transaction):
    """Reads and writes inside one open transaction. With `lock`, reads lock their rows."""

    def __init__(self, conn: Connection, *, lock: bool) -> None:
        self._conn = conn
        self._lock = lock

    def intents(self) -> Mapping[str, str]:
        rows = self._conn.execute("SELECT intent_id, operation FROM gw.intent").fetchall()
        return frozen({str(intent_id): str(operation) for intent_id, operation in rows})

    def run_closed(self) -> bool:
        return bool(self._one("SELECT run_closed FROM gw.run_state", ())[0])

    def close_run(self) -> None:
        self._conn.execute("UPDATE gw.run_state SET run_closed = true")

    def lock_intent(self, intent_id: str) -> None:
        if self._lock:
            self._conn.execute(
                "SELECT 1 FROM gw.intent WHERE intent_id = %s FOR UPDATE", (intent_id,)
            )

    def lock_intents(self) -> None:
        if self._lock:
            self._conn.execute("SELECT 1 FROM gw.intent ORDER BY intent_id FOR UPDATE")

    def view(self, action_id: str) -> ActionView:
        lock: LiteralString = " FOR UPDATE" if self._lock else ""
        row = self._conn.execute(_ACTION + " WHERE action_id = %s" + lock, (action_id,)).fetchone()
        if row is None:
            raise UnknownAction(action_id)
        action = _action(row)
        grant_row = self._conn.execute(_GRANT + " WHERE action_id = %s", (action_id,)).fetchone()
        attempts = self._conn.execute(
            _ATTEMPT + " WHERE action_id = %s ORDER BY attempt_no", (action_id,)
        ).fetchall()
        escalation_row = self._conn.execute(
            _ESCALATION + " WHERE action_id = %s", (action_id,)
        ).fetchone()
        events = self._conn.execute(_EVENT + " WHERE action_id = %s ORDER BY seq", (action_id,))
        return ActionView(
            action=action,
            grant=None if grant_row is None else _grant(grant_row),
            attempts=tuple(_attempt(r) for r in attempts),
            escalation=None if escalation_row is None else _escalation(escalation_row),
            events=tuple(_event(r) for r in events.fetchall()),
        )

    def action_ids(self) -> list[str]:
        lock: LiteralString = " FOR UPDATE" if self._lock else ""
        rows = self._conn.execute("SELECT action_id FROM gw.action" + _IN_ACTION_ORDER + lock)
        return [str(row[0]) for row in rows.fetchall()]

    def holders(self, intent_id: str) -> list[Action]:
        lock: LiteralString = " FOR UPDATE" if self._lock else ""
        rows = self._conn.execute(
            _ACTION
            + " WHERE intent_id = %s AND identity AND state = ANY(%s)"
            + _IN_ACTION_ORDER
            + lock,
            (intent_id, [str(state) for state in HOLDING_STATES]),
        ).fetchall()
        return [_action(row) for row in rows]

    def next_action_number(self) -> int:
        row = self._one(
            "UPDATE gw.run_state SET last_action_no = last_action_no + 1 RETURNING last_action_no",
            (),
        )
        return int(row[0])

    def apply(self, change: Change, vt: int) -> None:
        a = change.action
        self._conn.execute(
            "INSERT INTO gw.action (action_id, task_id, intent_id, operation, target, args,"
            " action_key, identity, contract, basis, state, grant_id, idempotency_key, effect_id,"
            " created_vt, updated_vt)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (action_id) DO UPDATE SET basis = EXCLUDED.basis,"
            " state = EXCLUDED.state, grant_id = EXCLUDED.grant_id,"
            " effect_id = EXCLUDED.effect_id, updated_vt = EXCLUDED.updated_vt",
            (
                a.action_id,
                a.task_id,
                a.intent_id,
                a.operation,
                a.target,
                Jsonb(to_json(a.args)),
                a.action_key,
                a.identity,
                str(a.contract),
                Jsonb(to_json(a.basis)),
                str(a.state),
                a.grant_id,
                a.idempotency_key,
                a.effect_id,
                a.created_vt,
                a.updated_vt,
            ),
        )
        if change.grant is not None:
            g = change.grant
            self._conn.execute(
                'INSERT INTO gw."grant" (grant_id, action_id, approval_id, state, requested_vt,'
                " consumed_vt) VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (grant_id) DO UPDATE SET state = EXCLUDED.state,"
                " consumed_vt = EXCLUDED.consumed_vt",
                (
                    g.grant_id,
                    g.action_id,
                    g.approval_id,
                    str(g.state),
                    g.requested_vt,
                    g.consumed_vt,
                ),
            )
        if change.attempt is not None:
            t = change.attempt
            check = t.check
            self._conn.execute(
                "INSERT INTO gw.attempt (attempt_id, action_id, attempt_no, idempotency_key,"
                " grant_id, check_kind, check_approval, check_basis, check_seen_seq, check_vt,"
                " sent_vt, answer_class, answer_ref, answered_vt)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (attempt_id) DO UPDATE SET answer_class = EXCLUDED.answer_class,"
                " answer_ref = EXCLUDED.answer_ref, answered_vt = EXCLUDED.answered_vt",
                (
                    t.attempt_id,
                    t.action_id,
                    t.attempt_no,
                    t.idempotency_key,
                    t.grant_id,
                    str(check.kind),
                    None if check.approval is None else str(check.approval),
                    None if check.basis is None else Jsonb(basis_to_json(check.basis)),
                    check.seen_seq,
                    check.check_vt,
                    t.sent_vt,
                    None if t.answer_class is None else str(t.answer_class),
                    t.answer_ref,
                    t.answered_vt,
                ),
            )
        if change.escalation is not None:
            e = change.escalation
            self._conn.execute(
                "INSERT INTO gw.escalation (escalation_id, action_id, opened_vt, resolved_vt,"
                " resolution, effect_id, actor) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (escalation_id) DO UPDATE SET resolved_vt = EXCLUDED.resolved_vt,"
                " resolution = EXCLUDED.resolution, effect_id = EXCLUDED.effect_id,"
                " actor = EXCLUDED.actor",
                (
                    e.escalation_id,
                    e.action_id,
                    e.opened_vt,
                    e.resolved_vt,
                    None if e.resolution is None else str(e.resolution),
                    e.effect_id,
                    e.actor,
                ),
            )
        if not change.events:
            return
        count = len(change.events)
        last = int(
            self._one(
                "UPDATE gw.run_state SET last_seq = last_seq + %s RETURNING last_seq", (count,)
            )[0]
        )
        for offset, new in enumerate(change.events):
            self._conn.execute(
                "INSERT INTO gw.action_event (seq, action_id, kind, from_state, to_state, reason,"
                " details, vt) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    last - count + 1 + offset,
                    a.action_id,
                    str(new.kind),
                    None if new.from_state is None else str(new.from_state),
                    str(new.to_state),
                    str(new.reason),
                    Jsonb(to_json(new.details)),
                    vt,
                ),
            )

    def log(
        self,
        tool: str,
        action_id: str | None,
        result: ActionResult | Refusal | ReportAccepted,
        vt: int,
    ) -> None:
        row = self._one(
            "UPDATE gw.run_state SET last_call_no = last_call_no + 1"
            " RETURNING last_call_no, last_seq",
            (),
        )
        self._conn.execute(
            "INSERT INTO gw.tool_call (call_no, tool, action_id, result, as_of_seq, vt)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (int(row[0]), tool, action_id, Jsonb(result_to_json(result)), int(row[1]), vt),
        )

    def snapshot(self) -> Snapshot:
        if self._lock:
            # Every writer takes the run's state row before it commits and holds it until
            # then, so with this lock no other transaction commits while these reads run.
            self._conn.execute("SELECT 1 FROM gw.run_state FOR UPDATE")
        actions = [_action(r) for r in self._conn.execute(_ACTION).fetchall()]
        attempts: dict[str, list[Attempt]] = {}
        for row in self._conn.execute(_ATTEMPT + " ORDER BY action_id, attempt_no").fetchall():
            attempt = _attempt(row)
            attempts.setdefault(attempt.action_id, []).append(attempt)
        grants = [_grant(r) for r in self._conn.execute(_GRANT).fetchall()]
        escalations = [_escalation(r) for r in self._conn.execute(_ESCALATION).fetchall()]
        return Snapshot(
            intents=self.intents(),
            actions=frozen({a.action_id: a for a in actions}),
            grants=frozen({g.grant_id: g for g in grants}),
            attempts=frozen({key: tuple(rows) for key, rows in attempts.items()}),
            events=tuple(
                _event(r) for r in self._conn.execute(_EVENT + " ORDER BY seq").fetchall()
            ),
            escalations=frozen({e.escalation_id: e for e in escalations}),
            tool_calls=tuple(
                _tool_call(r)
                for r in self._conn.execute(_TOOL_CALL + " ORDER BY call_no").fetchall()
            ),
            run_closed=self.run_closed(),
        )

    def _one(self, query: LiteralString, params: tuple[object, ...]) -> Row:
        row = self._conn.execute(query, params).fetchone()
        if row is None:
            raise LookupError("gw.run_state has no row; is the gateway migration applied?")
        return row


# Rows to records -----------------------------------------------------------------------------


def _action(row: Row) -> Action:
    return Action(
        action_id=row[0],
        task_id=row[1],
        intent_id=row[2],
        operation=row[3],
        target=row[4],
        args=args_from_json(row[5]),
        action_key=row[6],
        identity=row[7],
        contract=Contract(row[8]),
        basis=versions_from_json(row[9]),
        state=ActionState(row[10]),
        grant_id=row[11],
        idempotency_key=row[12],
        effect_id=row[13],
        created_vt=row[14],
        updated_vt=row[15],
    )


def _grant(row: Row) -> Grant:
    return Grant(row[0], row[1], row[2], GrantState(row[3]), row[4], row[5])


def _attempt(row: Row) -> Attempt:
    return Attempt(
        attempt_id=row[0],
        action_id=row[1],
        attempt_no=row[2],
        idempotency_key=row[3],
        grant_id=row[4],
        check=check_from_columns(row[5], row[6], row[7], row[8], row[9]),
        sent_vt=row[10],
        answer_class=None if row[11] is None else AnswerClass(row[11]),
        answer_ref=row[12],
        answered_vt=row[13],
    )


def _event(row: Row) -> ActionEvent:
    kind = EventKind(row[2])
    reason: Reason | Note = Reason(row[5]) if kind is EventKind.TRANSITION else Note(row[5])
    return ActionEvent(
        seq=row[0],
        action_id=row[1],
        kind=kind,
        from_state=None if row[3] is None else ActionState(row[3]),
        to_state=ActionState(row[4]),
        reason=reason,
        details=mapping_from_json(row[6]),
        vt=row[7],
    )


def _escalation(row: Row) -> Escalation:
    resolution = None if row[4] is None else Resolution(row[4])
    return Escalation(row[0], row[1], row[2], row[3], resolution, row[5], row[6])


def _tool_call(row: Row) -> ToolCall:
    return ToolCall(row[0], row[1], row[2], result_from_json(row[3]), row[4], row[5])
