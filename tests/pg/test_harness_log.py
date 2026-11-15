"""The harness's records in PostgreSQL: the log, its history rules and privileges (0004, 0008)."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import LiteralString

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow

from fivefold.harness.log import FaultRecord, MessageRecord, PgHarnessLog, ReportRecord
from fivefold.harness.reports import TEXT_KEY, ReportSink
from fivefold.harness.scheduler import Scheduler

pytestmark = pytest.mark.pg

Connection = psycopg.Connection[TupleRow]
TABLES = ["run", "run_stop", "message_log", "fault_log", "actor_log", "final_report"]


def _message(seq: int, *, kind: str = "request", correlation: int | None = None) -> MessageRecord:
    return MessageRecord(
        seq=seq,
        hop="gw_sim",
        kind="answer" if kind == "answer" else "request",
        sender="gateway",
        receiver="sim",
        api="submit_purchase_order",
        correlation=correlation,
        intent_id="REQ-1",
        idempotency_key="A-0001",
        answer_class="committed" if kind == "answer" else None,
        body={"n": seq},
        sent_vt=10,
    )


def _harness(dsn: str) -> Connection:
    return psycopg.connect(make_conninfo(dsn, user="ff_harness"), autocommit=True)


def _refused(conn: Connection, error: type[Exception], query: LiteralString) -> None:
    with pytest.raises(error), conn.transaction():
        conn.execute(query)


def test_the_log_writes_messages_their_fates_and_faults(migrated_database: str) -> None:
    with _harness(migrated_database) as conn:
        log = PgHarnessLog(conn)
        request = _message(log.next_seq())
        log.message_sent(request)
        log.message_fate(request.seq, "delivered", vt=30, delivered_seq=log.next_seq())
        answer = _message(log.next_seq(), kind="answer", correlation=request.seq)
        log.message_sent(answer)
        log.fault_fired(
            FaultRecord(
                seq=log.next_seq(),
                fault="F2",
                variant=None,
                point="net.gw_sim.answer",
                occurrence=1,
                action="drop",
                vt=40,
                target_seq=answer.seq,
            )
        )
        log.message_fate(answer.seq, "dropped", vt=40, reason="fault")
        with pytest.raises(ValueError, match="already has a fate"):
            log.message_fate(answer.seq, "dropped", vt=41, reason="fault")
        rows = conn.execute(
            "SELECT seq, kind, fate, drop_reason, delivered_seq, delivered_vt"
            " FROM harness.message_log ORDER BY seq"
        ).fetchall()
        assert rows == [
            (1, "request", "delivered", None, 2, 30),
            (3, "answer", "dropped", "fault", None, None),
        ]
        assert conn.execute("SELECT seq, fault, target_seq FROM harness.fault_log").fetchall() == [
            (4, "F2", 3)
        ]
    with _harness(migrated_database) as conn:
        assert PgHarnessLog(conn).next_seq() == 5, "a new log continues after the stored events"


def test_a_copy_names_the_request_it_repeats_and_its_answer_is_dropped_as_such(
    migrated_database: str,
) -> None:
    """F4 (0006): the copy is a message of its own; the answer to it is never delivered."""
    with _harness(migrated_database) as conn:
        log = PgHarnessLog(conn)
        request = _message(log.next_seq())
        log.message_sent(request)
        copy = replace(request, seq=log.next_seq(), copy_of=request.seq)
        log.message_sent(copy)
        log.message_fate(copy.seq, "delivered", vt=35, delivered_seq=log.next_seq())
        answer = _message(log.next_seq(), kind="answer", correlation=copy.seq)
        log.message_sent(answer)
        log.message_fate(answer.seq, "dropped", vt=60, reason="answer_to_copy")
        rows = conn.execute(
            "SELECT seq, kind, copy_of, fate, drop_reason FROM harness.message_log ORDER BY seq"
        ).fetchall()
        assert rows == [
            (1, "request", None, None, None),
            (2, "request", 1, "delivered", None),
            (4, "answer", None, "dropped", "answer_to_copy"),
        ]
        _refused(
            conn,
            psycopg.errors.RaiseException,
            "UPDATE harness.message_log SET copy_of = NULL WHERE seq = 2",
        )


COPY_INSERT: LiteralString = (
    "INSERT INTO harness.message_log (seq, hop, kind, sender, receiver, api, correlation,"
    " intent_id, idempotency_key, answer_class, body, sent_vt, copy_of)"
    " SELECT %s, hop, kind, sender, receiver, api, correlation, intent_id, idempotency_key,"
    " answer_class, body, sent_vt, %s FROM harness.message_log WHERE seq = %s"
)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("UPDATE harness.message_log SET body = '{\"n\": 9}' WHERE seq = 10", "RaiseException"),
        ("UPDATE harness.message_log SET sender = 'agent-1' WHERE seq = 10", "RaiseException"),
        ("UPDATE harness.message_log SET sent_vt = 11 WHERE seq = 10", "RaiseException"),
    ],
)
def test_a_copy_must_repeat_its_request_field_for_field(
    migrated_database: str, change: LiteralString, error: str
) -> None:
    """The copy is built from a request, then one field is made to differ. The database
    holds every copy to the request it names, as the transport makes it (0006)."""
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        log = PgHarnessLog(conn)
        log.message_sent(_message(1))
        conn.execute("ALTER TABLE harness.message_log DISABLE TRIGGER USER")
        conn.execute(COPY_INSERT, (10, None, 1))
        conn.execute(change)
        conn.execute("ALTER TABLE harness.message_log ENABLE TRIGGER USER")
        with pytest.raises(getattr(psycopg.errors, error)), conn.transaction():
            conn.execute(COPY_INSERT, (11, 1, 10))
        conn.execute(COPY_INSERT, (12, 1, 1))


def test_only_a_later_message_can_copy_a_request_and_never_an_answer(
    migrated_database: str,
) -> None:
    """A copy comes after its request (7 copies 5, 4 cannot), and repeats what a
    component sent: not a copy (7) and not an answer (6). A notification passes the
    request's fault point, so it can be copied (3 copies 2)."""
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        log = PgHarnessLog(conn)
        log.message_sent(replace(_message(2), kind="notification", api="notifications/x"))
        conn.execute(COPY_INSERT, (3, 2, 2))
        log.message_sent(_message(5))
        log.message_sent(_message(6, kind="answer", correlation=5))
        conn.execute(COPY_INSERT, (7, 5, 5))
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(COPY_INSERT, (4, 5, 5))
        with pytest.raises(psycopg.errors.RaiseException, match="not a copy"), conn.transaction():
            conn.execute(COPY_INSERT, (8, 7, 7))
        with pytest.raises(psycopg.errors.RaiseException, match="not a copy"), conn.transaction():
            conn.execute(COPY_INSERT, (9, 6, 6))


def test_the_log_writes_reports_and_numbers_after_them(migrated_database: str) -> None:
    with _harness(migrated_database) as conn:
        log = PgHarnessLog(conn)
        log.report_submitted(
            ReportRecord(
                seq=log.next_seq(),
                worker="agent-1",
                report={"claims": []},
                valid=False,
                errors=("REQ-1 has no claim",),
                accepted_by="sink",
                vt=12,
            )
        )
        row = conn.execute(
            "SELECT seq, worker, report, valid, errors, accepted_by, message_seq, vt"
            " FROM harness.final_report"
        ).fetchone()
        assert row == (
            1,
            "agent-1",
            {"claims": []},
            False,
            ["REQ-1 has no claim"],
            "sink",
            None,
            12,
        )
    with _harness(migrated_database) as conn:
        assert PgHarnessLog(conn).next_seq() == 2, "a new log continues after the stored reports"


def test_the_sink_records_a_report_jsonb_cannot_hold(migrated_database: str) -> None:
    """A NUL character in a report must not stop the sink recording it."""
    report = {"claims": [{"intent_id": "REQ-1", "status": "unknown", "note": "a\x00b"}]}
    with _harness(migrated_database) as conn:
        sink = ReportSink(Scheduler(), PgHarnessLog(conn), ["REQ-1"])
        assert sink.submit("agent-1", report, accepted_by="sink") != ()
        row = conn.execute("SELECT report, valid FROM harness.final_report").fetchone()
    assert row is not None and row[1] is False
    assert json.loads(row[0][TEXT_KEY]) == report


def test_a_message_is_history_and_its_fate_is_set_once(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        log = PgHarnessLog(conn)
        log.message_sent(_message(1))
        _refused(conn, psycopg.errors.RaiseException, "UPDATE harness.message_log SET api = 'x'")
        conn.execute(
            "UPDATE harness.message_log SET fate = 'delivered', delivered_seq = 2,"
            " delivered_vt = 20 WHERE seq = 1"
        )
        _refused(
            conn,
            psycopg.errors.RaiseException,
            "UPDATE harness.message_log SET fate = 'dropped', drop_reason = 'fault',"
            " delivered_seq = NULL, delivered_vt = NULL",
        )
        _refused(conn, psycopg.errors.RaiseException, "DELETE FROM harness.message_log")


@pytest.mark.parametrize(
    "query",
    [
        "UPDATE harness.message_log SET fate = 'delivered'",
        "UPDATE harness.message_log SET fate = 'dropped'",
        "UPDATE harness.message_log SET fate = 'delivered', delivered_seq = 1, delivered_vt = 20",
        "UPDATE harness.message_log SET fate = 'delivered', delivered_seq = 2, delivered_vt = 5",
    ],
)
def test_a_fate_must_be_complete_and_consistent(
    migrated_database: str, query: LiteralString
) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        PgHarnessLog(conn).message_sent(_message(1))
        _refused(conn, psycopg.errors.CheckViolation, query)


def test_faults_actor_actions_and_reports_are_append_only(migrated_database: str) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO harness.fault_log (seq, fault, point, occurrence, action, params, vt)"
            " VALUES (1, 'F11', 'agent.after_tool_call', 1, 'kill', '{}', 0)"
        )
        conn.execute(
            "INSERT INTO harness.actor_log (seq, actor, action, subject, details, vt)"
            " VALUES (2, 'approver', 'approve', 'APR-0001', '{}', 0)"
        )
        conn.execute(
            "INSERT INTO harness.final_report (seq, worker, report, valid, errors, accepted_by, vt)"
            " VALUES (3, 'agent-1', '{}', true, '[]', 'sink', 0)"
        )
        for table in ("fault_log", "actor_log", "final_report"):
            _refused(conn, psycopg.errors.RaiseException, f"UPDATE harness.{table} SET vt = 1")
            _refused(conn, psycopg.errors.RaiseException, f"DELETE FROM harness.{table}")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO harness.fault_log (seq, fault, point, occurrence, action, params, vt)"
                " VALUES (4, 'F13', 'agent.step', 1, 'kill', '{}', 0)"
            )


def test_a_run_stops_once_and_its_stop_never_changes(migrated_database: str) -> None:
    with _harness(migrated_database) as conn:
        conn.execute(
            "INSERT INTO harness.run (run_id, scenario_id, scenario_digest, config, contract,"
            " seed, topology, model_adapter, model, params) VALUES ('RUN-1', 'dev-001', 'abc',"
            " 'G2', 'C2', 7, 'inproc', 'scripted', 'scripted-policy', '{}')"
        )
        log = PgHarnessLog(conn)
        log.run_stopped(40_000)
        assert conn.execute("SELECT run_id, stopped_vt FROM harness.run_stop").fetchall() == [
            ("RUN-1", 40_000)
        ]
        with pytest.raises(psycopg.errors.UniqueViolation):
            log.run_stopped(50_000)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        _refused(conn, psycopg.errors.RaiseException, "UPDATE harness.run_stop SET stopped_vt = 1")
        _refused(conn, psycopg.errors.RaiseException, "DELETE FROM harness.run_stop")


def test_a_run_database_describes_one_run(migrated_database: str) -> None:
    insert: LiteralString = (
        "INSERT INTO harness.run (run_id, scenario_id, scenario_digest, config, contract, seed,"
        " topology, model_adapter, model, params) VALUES (%s, 'dev-001', 'abc', 'G2', 'C2', 7,"
        " 'inproc', 'scripted', 'scripted-policy', '{}')"
    )
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        conn.execute(insert, ("RUN-1",))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, ("RUN-2",))


EXPECTED: dict[str, dict[str, set[str]]] = {
    "ff_harness": {
        table: {"SELECT", "INSERT", "UPDATE", "DELETE"}
        if table == "run"
        else {"SELECT", "INSERT", "UPDATE"}
        if table == "message_log"
        else {"SELECT", "INSERT"}
        for table in TABLES
    },
    "ff_eval": {table: {"SELECT"} for table in TABLES},
    "ff_viewer": {table: {"SELECT"} for table in TABLES},
    "ff_sim": {table: set() for table in TABLES},
    "ff_gw": {table: set() for table in TABLES},
    "ff_agent": {table: set() for table in TABLES},
}
"""Privileges on every harness table: 0008's table, with the history tables append-only."""


@pytest.mark.parametrize("role", sorted(EXPECTED))
def test_privileges_on_every_harness_table(migrated_database: str, role: str) -> None:
    with psycopg.connect(migrated_database) as conn:
        for table in TABLES:
            granted = {
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f"harness.{table}", privilege),
                ).fetchone()
                == (True,)
            }
            assert granted == EXPECTED[role][table], (role, table)
