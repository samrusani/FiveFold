"""What the gw schema enforces on its own (0002, 0003, 0008).

The store writes only records the step functions decided, so these rules should never
fire in normal use. Each test writes a broken record directly and checks that the
database refuses it, so a store with a bug cannot write it either.
"""

from __future__ import annotations

from typing import LiteralString

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.pg

Connection = psycopg.Connection[TupleRow]
TABLES = ["run_state", "intent", "action", "grant", "attempt", "action_event", "escalation"]
TABLES += ["tool_call", "report", "read", "proposal"]
LIVE = ["proposed", "pending_approval", "authorized", "dispatched", "outcome_unknown"]


def _intent(conn: Connection, intent_id: str = "REQ-1") -> None:
    conn.execute(
        "INSERT INTO gw.intent (intent_id, operation) VALUES (%s, 'submit_purchase_order')"
        " ON CONFLICT DO NOTHING",
        (intent_id,),
    )


def _action(
    conn: Connection,
    action_id: str,
    *,
    intent_id: str = "REQ-1",
    state: str = "proposed",
    identity: bool = True,
    effect_id: str | None = None,
) -> None:
    _intent(conn, intent_id)
    conn.execute(
        "INSERT INTO gw.action (action_id, task_id, intent_id, operation, target, args,"
        " action_key, identity, contract, basis, state, idempotency_key, effect_id,"
        " created_vt, updated_vt) VALUES (%s, 'TASK-1', %s, 'submit_purchase_order', 't',"
        " '{}', 'k', %s, 'C2', '{}', %s, %s, %s, 0, 0)",
        (action_id, intent_id, identity, state, action_id, effect_id),
    )


def _grant(conn: Connection, grant_id: str, action_id: str, approval_id: str) -> None:
    conn.execute(
        'INSERT INTO gw."grant" (grant_id, action_id, approval_id, state, requested_vt)'
        " VALUES (%s, %s, %s, 'pending', 0)",
        (grant_id, action_id, approval_id),
    )


def _attempt(conn: Connection, action_id: str, grant_id: str, *, key: str | None = None) -> None:
    conn.execute(
        "INSERT INTO gw.attempt (attempt_id, action_id, attempt_no, idempotency_key, grant_id,"
        " check_kind, check_seen_seq, check_vt, sent_vt)"
        " VALUES (%s, %s, 1, %s, %s, 'exempt', 1, 0, 0)",
        (f"{action_id}/1", action_id, key or action_id, grant_id),
    )


def _event(conn: Connection, seq: int, action_id: str) -> None:
    conn.execute(
        "INSERT INTO gw.action_event (seq, action_id, kind, from_state, to_state, reason,"
        " details, vt) VALUES (%s, %s, 'transition', NULL, 'proposed', 'admitted', '{}', 0)",
        (seq, action_id),
    )


def _refused(conn: Connection, error: type[Exception], query: LiteralString) -> None:
    with pytest.raises(error), conn.transaction():
        conn.execute(query)


def _set(conn: Connection, table: LiteralString, column: str, value: object) -> None:
    """Update one column of every row of a gw table."""
    query = sql.SQL("UPDATE {} SET {} = %s").format(
        sql.Identifier("gw", table), sql.Identifier(column)
    )
    conn.execute(query, (value,))


def _with_grant(conn: Connection, action_id: str, grant_id: str) -> None:
    """Give an action its grant, as T3 does."""
    _grant(conn, grant_id, action_id, f"AP-{action_id}")
    conn.execute("UPDATE gw.action SET grant_id = %s WHERE action_id = %s", (grant_id, action_id))


def test_an_effect_id_exists_exactly_when_the_action_is_confirmed(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            _action(conn, "A-0001", effect_id="PO-0001")
        with pytest.raises(psycopg.errors.CheckViolation):
            _action(conn, "A-0002", state="confirmed")
        _action(conn, "A-0003", state="confirmed", effect_id="PO-0001")


def test_an_effect_id_never_changes_once_set(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001", state="confirmed", effect_id="PO-0001")
        conn.execute("UPDATE gw.action SET effect_id = 'PO-0001' WHERE action_id = 'A-0001'")
        with pytest.raises(psycopg.errors.RaiseException, match="I1"):
            conn.execute("UPDATE gw.action SET effect_id = 'PO-0002' WHERE action_id = 'A-0001'")


def test_one_live_or_confirmed_action_per_intent(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001", state="rejected")
        _action(conn, "A-0002", state="failed")
        _action(conn, "A-0003")
        for number, state in enumerate([*LIVE, "confirmed"], start=4):
            effect = "PO-0001" if state == "confirmed" else None
            with pytest.raises(psycopg.errors.UniqueViolation):
                _action(conn, f"A-{number:04d}", state=state, effect_id=effect)


def test_actions_without_identity_are_exempt_from_i7(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001")
        _action(conn, "A-0002", identity=False)
        _action(conn, "A-0003", identity=False)


def test_one_grant_per_action_and_per_approval(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001", state="rejected")
        _action(conn, "A-0002")
        _grant(conn, "G-0001", "A-0001", "AP-1")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _grant(conn, "G-0009", "A-0001", "AP-9")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _grant(conn, "G-0002", "A-0002", "AP-1")


def test_an_attempt_uses_its_own_actions_grant_and_key(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001", state="rejected")
        _action(conn, "A-0002")
        _with_grant(conn, "A-0001", "G-0001")
        _grant(conn, "G-0002", "A-0002", "AP-2")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _attempt(conn, "A-0002", "G-0002")  # a grant the action does not hold yet
        conn.execute("UPDATE gw.action SET grant_id = 'G-0002' WHERE action_id = 'A-0002'")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _attempt(conn, "A-0002", "G-0001")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _attempt(conn, "A-0002", "G-0002", key="A-0001")
        _attempt(conn, "A-0002", "G-0002")


def test_an_actions_grant_belongs_to_it(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001", state="rejected")
        _action(conn, "A-0002")
        _grant(conn, "G-0001", "A-0001", "AP-1")
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            conn.execute("UPDATE gw.action SET grant_id = 'G-0001' WHERE action_id = 'A-0002'")


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("action_id", "A-0009"),
        ("task_id", "TASK-2"),
        ("intent_id", "REQ-2"),
        ("operation", "reserve_inventory"),
        ("target", "supplier/S-2"),
        ("args", Jsonb({"quantity": 2})),
        ("action_key", "k2"),
        ("identity", False),
        ("contract", "C3"),
        ("idempotency_key", "A-0009"),
        ("created_vt", 5),
    ],
)
def test_what_identifies_an_action_never_changes(
    migrated_database: str, column: str, value: object
) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _intent(conn, "REQ-2")
        _action(conn, "A-0001")
        with pytest.raises(psycopg.errors.RaiseException, match="cannot change"):
            _set(conn, "action", column, value)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("basis", Jsonb({"quote": 2})),
        ("state", "pending_approval"),
        ("updated_vt", 5),
    ],
)
def test_an_action_moves_through_its_mutable_columns(
    migrated_database: str, column: str, value: object
) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001")
        _set(conn, "action", column, value)


@pytest.mark.parametrize(
    ("column", "value"),
    [("grant_id", "G-0009"), ("action_id", "A-0002"), ("approval_id", "AP-9"), ("requested_vt", 5)],
)
def test_a_grant_never_moves(migrated_database: str, column: str, value: object) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001")
        _action(conn, "A-0002", state="rejected")
        _grant(conn, "G-0001", "A-0001", "AP-1")
        with pytest.raises(psycopg.errors.RaiseException, match="I2"):
            _set(conn, "grant", column, value)
        _set(conn, "grant", "state", "active")
        _set(conn, "grant", "consumed_vt", 5)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("attempt_id", "A-0001/9"),
        ("action_id", "A-0002"),
        ("attempt_no", 2),
        ("idempotency_key", "A-0002"),
        ("grant_id", "G-0002"),
        ("check_kind", "full"),
        ("check_approval", "approved"),
        ("check_basis", Jsonb({"exact_ok": True})),
        ("check_seen_seq", 2),
        ("check_vt", 1),
        ("check_approval_expires_at", 5),
        ("sent_vt", 1),
    ],
)
def test_an_attempt_is_history(migrated_database: str, column: str, value: object) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001", state="dispatched")
        _with_grant(conn, "A-0001", "G-0001")
        _attempt(conn, "A-0001", "G-0001")
        with pytest.raises(psycopg.errors.RaiseException, match="history"):
            _set(conn, "attempt", column, value)
        _set(conn, "attempt", "answer_class", "unknown")
        _set(conn, "attempt", "answer_ref", "lost")
        _set(conn, "attempt", "answered_vt", 5)


def test_an_approved_check_keeps_the_approvals_expiry(migrated_database: str) -> None:
    """The expiry is kept exactly when the check found the approval approved (0010)."""
    insert = (
        "INSERT INTO gw.attempt (attempt_id, action_id, attempt_no, idempotency_key, grant_id,"
        " check_kind, check_approval, check_basis, check_seen_seq, check_vt,"
        " check_approval_expires_at, sent_vt) VALUES ('A-0001/1', 'A-0001', 1, 'A-0001',"
        " 'G-0001', %s, %s, %s, 1, 0, %s, 0)"
    )
    basis = Jsonb({"exact_ok": True, "sufficient_ok": True})
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001", state="dispatched")
        _with_grant(conn, "A-0001", "G-0001")
        for kind, approval, expires_at in [
            ("full", "approved", None),
            ("full", "revoked", 5),
            ("exempt", None, 5),
        ]:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    insert, (kind, approval, None if approval is None else basis, expires_at)
                )
        conn.execute(insert, ("full", "approved", basis, 3_600_000))


def test_an_intent_never_changes_and_never_reopens(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _intent(conn)
        with pytest.raises(psycopg.errors.RaiseException, match="cannot change or reopen"):
            _set(conn, "intent", "operation", "reserve_inventory")
        _set(conn, "intent", "closed", True)
        with pytest.raises(psycopg.errors.RaiseException, match="cannot change or reopen"):
            _set(conn, "intent", "closed", False)


def test_a_report_names_the_response_that_accepted_it(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("INSERT INTO gw.report (worker, call_no) VALUES ('W1', 1)")
        conn.execute(
            "INSERT INTO gw.tool_call (call_no, tool, result, as_of_seq, vt)"
            " VALUES (1, 'submit_final_report', '{}', 0, 0)"
        )
        conn.execute("INSERT INTO gw.report (worker, call_no) VALUES ('W1', 1)")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO gw.report (worker, call_no) VALUES ('W2', 1)")


def test_an_intent_is_never_deleted(migrated_database: str) -> None:
    """Deleting and re-adding an intent would reopen it (0010)."""
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _intent(conn)
        _refused(conn, psycopg.errors.RaiseException, "DELETE FROM gw.intent")


def test_a_closed_run_stays_closed_and_its_counters_move_forward(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        conn.execute("UPDATE gw.run_state SET run_closed = true, last_seq = 5")
        for change in (
            "UPDATE gw.run_state SET run_closed = false",
            "UPDATE gw.run_state SET last_seq = 4",
            "UPDATE gw.run_state SET last_action_no = -1",
            "UPDATE gw.run_state SET last_call_no = -1",
            "UPDATE gw.run_state SET last_read_no = -1",
            "UPDATE gw.run_state SET last_proposal_no = -1",
            "DELETE FROM gw.run_state",
        ):
            _refused(conn, psycopg.errors.RaiseException, change)
        assert conn.execute("SELECT run_closed, last_seq FROM gw.run_state").fetchone() == (
            True,
            5,
        )


def test_reports_are_history_for_every_role(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO gw.tool_call (call_no, tool, result, as_of_seq, vt)"
            " VALUES (1, 'submit_final_report', '{}', 0, 0)"
        )
        conn.execute("INSERT INTO gw.report (worker, call_no) VALUES ('W1', 1)")
        _refused(conn, psycopg.errors.RaiseException, "UPDATE gw.report SET worker = 'W2'")
        _refused(conn, psycopg.errors.RaiseException, "DELETE FROM gw.report")


def _proposal(
    conn: Connection,
    proposal_id: str,
    admission: str,
    reason: str | None,
    action_id: str | None,
) -> None:
    conn.execute(
        "INSERT INTO gw.proposal (proposal_id, operation, intent_id, args, based_on,"
        " admission, reason, action_id, vt)"
        " VALUES (%s, 'submit_purchase_order', 'REQ-1', '{}', '[]', %s, %s, %s, 0)",
        (proposal_id, admission, reason, action_id),
    )


def test_reads_and_write_calls_are_history_for_every_role(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO gw.read (read_id, key, version, data, vt)"
            " VALUES ('R-0001', 'f', 1, '{}', 0)"
        )
        _proposal(conn, "P-0001", "refused", "unknown_intent", None)
        for change in (
            "UPDATE gw.read SET version = 2",
            "DELETE FROM gw.read",
            "UPDATE gw.proposal SET reason = 'run_closed'",
            "DELETE FROM gw.proposal",
        ):
            _refused(conn, psycopg.errors.RaiseException, change)


def test_a_write_call_states_its_outcome_consistently(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001")
        _proposal(conn, "P-0001", "admitted", None, "A-0001")
        _proposal(conn, "P-0002", "refused", "intent_busy", "A-0001")
        for proposal_id, admission, reason, action_id in (
            ("P-0003", "admitted", "run_closed", "A-0001"),
            ("P-0004", "refused", None, None),
            ("P-0005", "attached", None, None),
            ("P-0006", "refused", "report_inconsistent", None),
            ("X-0007", "refused", "run_closed", None),
        ):
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                _proposal(conn, proposal_id, admission, reason, action_id)
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            _proposal(conn, "P-0008", "attached", None, "A-0009")


def test_the_run_has_one_state_row(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO gw.run_state DEFAULT VALUES")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO gw.run_state (singleton) VALUES (false)")
        assert conn.execute("SELECT count(*) FROM gw.run_state").fetchone() == (1,)


def test_the_log_is_never_truncated(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001")
        _event(conn, 1, "A-0001")
        _refused(conn, psycopg.errors.RaiseException, "TRUNCATE gw.action_event")
        _refused(conn, psycopg.errors.RaiseException, "TRUNCATE gw.action CASCADE")
        assert conn.execute("SELECT count(*) FROM gw.action_event").fetchone() == (1,)


def test_action_event_is_append_only_for_every_role(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _action(conn, "A-0001")
        _event(conn, 1, "A-0001")
        _refused(conn, psycopg.errors.RaiseException, "UPDATE gw.action_event SET vt = 1")
        _refused(conn, psycopg.errors.RaiseException, "DELETE FROM gw.action_event")
    gateway = make_conninfo(migrated_database, user="ff_gw")
    with psycopg.connect(gateway, autocommit=True) as conn:
        _event(conn, 2, "A-0001")
        _refused(conn, psycopg.errors.InsufficientPrivilege, "UPDATE gw.action_event SET vt = 1")
        _refused(conn, psycopg.errors.InsufficientPrivilege, "DELETE FROM gw.action_event")
        rows = conn.execute("SELECT seq FROM gw.action_event ORDER BY seq").fetchall()
    assert rows == [(1,), (2,)]


EXPECTED: dict[str, dict[str, set[str]]] = {
    "ff_gw": {
        table: {"SELECT", "INSERT", "UPDATE", "DELETE"}
        if table not in ("action_event", "report", "read", "proposal")
        else {"SELECT", "INSERT"}
        for table in TABLES
    },
    "ff_eval": {table: {"SELECT"} for table in TABLES},
    "ff_viewer": {table: {"SELECT"} for table in TABLES},
    "ff_sim": {table: set() for table in TABLES},
    "ff_harness": {table: set() for table in TABLES},
    "ff_agent": {table: set() for table in TABLES},
}
"""Privileges on every gw table: 0008's table, plus record 0010's `report` and 0001's
reads and write calls, which ff_gw only adds to."""


@pytest.mark.parametrize("role", sorted(EXPECTED))
def test_privileges_on_every_gateway_table(migrated_database: str, role: str) -> None:
    with psycopg.connect(migrated_database) as conn:
        for table in TABLES:
            granted = {
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f'gw."{table}"', privilege),
                ).fetchone()
                == (True,)
            }
            assert granted == EXPECTED[role][table], (role, table)
