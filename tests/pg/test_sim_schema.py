"""What the sim schema enforces on its own (0002, 0004, 0005, 0008, 0009).

The service writes only records it decided, so these rules should never fire in normal
use. Each test sets up a run through the service, then writes a broken record directly,
as the admin role, and checks that the database refuses it. The admin role bypasses
privileges but not triggers or constraints, so a simulator with a bug cannot write the
record either.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from fivefold.domain.api import (
    ApprovalAnswer,
    GetBudget,
    GetQuote,
    PurchaseOrderScope,
    RequestApproval,
    SubmitPurchaseOrder,
    WriteCommitted,
    WriteReceived,
)
from fivefold.domain.contracts import Contract
from fivefold.domain.rules import Operation
from fivefold.sim import Simulator

pytestmark = pytest.mark.pg

Connection = psycopg.Connection[TupleRow]
SimFactory = Callable[..., Simulator]
TABLES: list[LiteralString] = ["run", "event", "fact_version", "read_log", "write", "effect"]
TABLES += ["approval", "approval_event"]
APPEND_ONLY: dict[LiteralString, LiteralString] = {
    "event": "vt = vt",
    "fact_version": "cause = cause",
    "read_log": "vt = vt",
    "effect": "committed_vt = committed_vt",
    "approval_event": "vt = vt",
}
"""The append-only tables, with a harmless assignment for an UPDATE of each."""
ORDER_ARGS = {
    "supplier_id": "S-1",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 540,
    "currency": "USD",
    "cost_center": "CC-OPS",
}
ORDER = SubmitPurchaseOrder.model_validate(
    {
        "intent_id": "REQ-1",
        "approval_id": "APR-0001",
        "basis": {"quote/S-1/W-100": 1, "budget/CC-OPS": 1},
        "idempotency_key": "A-0001",
        **ORDER_ARGS,
    }
)


@pytest.fixture
def admin(sim_database: str) -> Iterator[Connection]:
    """An admin connection to the run database: privileges do not apply to it."""
    with psycopg.connect(sim_database, autocommit=True) as conn:
        yield conn


def _event(conn: Connection, kind: str, vt: int, actor: str = "test") -> int:
    row = conn.execute(
        "INSERT INTO sim.event (vt, kind, actor) VALUES (%s, %s, %s) RETURNING seq",
        (vt, kind, actor),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _one(conn: Connection, query: LiteralString, *params: object) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def _refused(
    conn: Connection, error: type[Exception], query: LiteralString, *params: object
) -> None:
    with pytest.raises(error), conn.transaction():
        conn.execute(query, params)


def _approve(sim: Simulator, vt: int = 0) -> str:
    """An approval of ORDER, approved by policy."""
    scope = PurchaseOrderScope(
        supplier_id="S-1",
        sku="W-100",
        max_quantity=100,
        max_amount_minor=54_000,
        currency="USD",
        cost_center="CC-OPS",
    )
    answer = sim.request_approval(
        RequestApproval(intent_id="REQ-1", operation=Operation.SUBMIT_PURCHASE_ORDER, scope=scope),
        caller="gateway",
        vt=vt,
    )
    assert isinstance(answer, ApprovalAnswer)
    return answer.approval_id


def _received(sim: Simulator, vt: int = 0, **changes: object) -> int:
    """A purchase order in flight, received through the split path."""
    answer = sim.receive_write(ORDER.model_copy(update=changes), vt=vt, caller="gateway")
    assert isinstance(answer, WriteReceived)
    return answer.received_seq


def _commit_by_hand(
    conn: Connection,
    received_seq: int,
    vt: int,
    *,
    effect_id: str = "PO-0001",
    args: dict[str, object] | None = None,
    key: str | None = "A-0001",
    committed_minor: int | None = 74_000,
    finish: bool = True,
) -> None:
    """Commit a received write with SQL, as the service would, unless told otherwise."""
    seq = _event(conn, "write_committed", vt)
    conn.execute(
        "INSERT INTO sim.effect (effect_id, intent_id, operation, args, approval_id, basis,"
        " idempotency_key, client_ref, received_seq, commit_seq, committed_vt)"
        " VALUES (%s, 'REQ-1', 'submit_purchase_order', %s, 'APR-0001', %s, %s, NULL,"
        " %s, %s, %s)",
        (
            effect_id,
            Jsonb(ORDER_ARGS if args is None else args),
            Jsonb(ORDER.basis),
            key,
            received_seq,
            seq,
            vt,
        ),
    )
    if committed_minor is not None:
        version = int(
            _one(conn, "SELECT max(version) FROM sim.fact_version WHERE key = 'budget/CC-OPS'")
        )
        conn.execute(
            "INSERT INTO sim.fact_version (key, version, value, valid_from_seq, valid_from_vt,"
            " cause, cause_effect_id) VALUES ('budget/CC-OPS', %s, %s, %s, %s, 'effect', %s)",
            (
                version + 1,
                Jsonb({"limit_minor": 100_000, "committed_minor": committed_minor}),
                seq,
                vt,
                effect_id,
            ),
        )
    if finish:
        conn.execute(
            "UPDATE sim.write SET state = 'committed', outcome_seq = %s, outcome_vt = %s,"
            " effect_id = %s WHERE received_seq = %s",
            (seq, vt, effect_id, received_seq),
        )


# Privileges ----------------------------------------------------------------------------

EXPECTED: dict[str, dict[str, set[str]]] = {
    "ff_sim": {table: {"SELECT", "INSERT", "UPDATE", "DELETE"} for table in TABLES},
    "ff_harness": {table: {"SELECT"} for table in TABLES},
    "ff_eval": {table: {"SELECT"} for table in TABLES},
    "ff_viewer": {table: {"SELECT"} for table in TABLES},
    "ff_gw": {table: set() for table in TABLES},
    "ff_agent": {table: set() for table in TABLES},
}
"""Privileges on every sim table, from 0008's table: only ff_sim writes. Triggers, not
privileges, keep ground truth from being deleted or rewritten, for every role."""


@pytest.mark.parametrize("role", sorted(EXPECTED))
def test_privileges_on_every_sim_table(admin: Connection, role: str) -> None:
    for table in TABLES:
        granted = {
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
            if _has_privilege(admin, role, f"sim.{table}", privilege)
        }
        assert granted == EXPECTED[role][table], (role, table)


def _has_privilege(conn: Connection, role: str, table: str, privilege: str) -> bool:
    return bool(_one(conn, "SELECT has_table_privilege(%s, %s, %s)", role, table, privilege))


@pytest.mark.parametrize("role", ["ff_gw", "ff_agent"])
def test_the_gateway_and_the_agent_cannot_read_ground_truth(
    make_sim: SimFactory, sim_database: str, role: str
) -> None:
    make_sim()
    with (
        psycopg.connect(make_conninfo(sim_database, user=role)) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT count(*) FROM sim.effect")


# The run and its events ------------------------------------------------------------------


def test_events_need_a_run(admin: Connection) -> None:
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "INSERT INTO sim.event (vt, kind, actor) VALUES (0, 'setup', 'test')",
    )


def test_a_run_database_holds_one_run(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    _refused(
        admin,
        psycopg.errors.UniqueViolation,
        "INSERT INTO sim.run (scenario_id, task_id, capabilities, max_hop_latency_ms,"
        " commit_deadline_ms, status_lag_ms, credentials, authority)"
        " SELECT scenario_id, task_id, capabilities, max_hop_latency_ms, commit_deadline_ms,"
        " status_lag_ms, credentials, authority FROM sim.run",
    )


ASSIGNMENTS: list[LiteralString] = [
    "commit_deadline_ms = 1",
    "status_lag_ms = 5",
    "capabilities = ARRAY['status']",
    "credentials = ARRAY['agent']",
    "authority = authority || '{\"approval_ttl_ms\": 1}'",
]


@pytest.mark.parametrize("assignment", ASSIGNMENTS)
def test_the_runs_configuration_never_changes(
    make_sim: SimFactory, admin: Connection, assignment: LiteralString
) -> None:
    make_sim()
    with pytest.raises(psycopg.errors.RaiseException, match="configuration"):
        admin.execute("UPDATE sim.run SET " + assignment)


def test_only_a_new_event_moves_the_sequence_and_the_clock(
    make_sim: SimFactory, admin: Connection
) -> None:
    make_sim()
    with pytest.raises(psycopg.errors.RaiseException, match="only a new event"):
        admin.execute("UPDATE sim.run SET last_seq = last_seq + 1")
    with pytest.raises(psycopg.errors.RaiseException, match="only a new event"):
        admin.execute("UPDATE sim.run SET last_vt = last_vt + 1")


def test_id_counters_never_go_back(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    admin.execute("UPDATE sim.run SET last_apr_no = 5")
    _refused(admin, psycopg.errors.RaiseException, "UPDATE sim.run SET last_apr_no = 4")


def test_events_take_the_next_seq_and_never_leave_a_gap(
    make_sim: SimFactory, admin: Connection
) -> None:
    make_sim()
    assert _event(admin, "fact_changed", 5) == 2
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "INSERT INTO sim.event (seq, vt, kind, actor) VALUES (4, 5, 'read', 'test')",
    )
    admin.execute("INSERT INTO sim.event (seq, vt, kind, actor) VALUES (3, 5, 'read', 'test')")
    rows = admin.execute("SELECT seq FROM sim.event ORDER BY seq").fetchall()
    assert rows == [(1,), (2,), (3,)]
    assert _one(admin, "SELECT last_seq FROM sim.run") == 3


def test_virtual_time_never_goes_back(make_sim: SimFactory, admin: Connection) -> None:
    make_sim(vt=100)
    _event(admin, "read", 100)
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "INSERT INTO sim.event (vt, kind, actor) VALUES (99, 'read', 'test')",
    )


# Facts -----------------------------------------------------------------------------------


def test_fact_versions_run_without_gaps(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    seq = _event(admin, "fact_changed", 0)
    quote = Jsonb(
        {
            "unit_price_minor": 1,
            "currency": "USD",
            "min_quantity": 1,
            "lead_time_days": 1,
            "status": "active",
        }
    )
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "INSERT INTO sim.fact_version (key, version, value, valid_from_seq, valid_from_vt,"
        " cause, cause_actor) VALUES ('quote/S-1/W-100', 3, %s, %s, 0, 'control', 'test')",
        quote,
        seq,
    )


def test_a_fact_is_created_only_at_setup(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    seq = _event(admin, "fact_changed", 0)
    _refused(
        admin,
        psycopg.errors.CheckViolation,
        "INSERT INTO sim.fact_version (key, version, value, valid_from_seq, valid_from_vt,"
        " cause, cause_actor) VALUES ('budget/CC-RND', 1, %s, %s, 0, 'control', 'test')",
        Jsonb({"limit_minor": 1, "committed_minor": 0}),
        seq,
    )


def test_each_version_comes_from_a_later_event(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    seq = _event(admin, "fact_changed", 0)
    insert: LiteralString = (
        "INSERT INTO sim.fact_version (key, version, value, valid_from_seq, valid_from_vt,"
        " cause, cause_actor) VALUES ('budget/CC-OPS', %s, %s, %s, 0, 'control', 'test')"
    )
    admin.execute(insert, (2, Jsonb({"limit_minor": 5, "committed_minor": 0}), seq))
    _refused(
        admin,
        psycopg.errors.RaiseException,
        insert,
        3,
        Jsonb({"limit_minor": 6, "committed_minor": 0}),
        seq,
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("budget/CC-OPS", {"limit_minor": 5}),
        ("budget/CC-OPS", {"limit_minor": 5.5, "committed_minor": 0}),
        ("budget/CC-OPS", {"limit_minor": "5", "committed_minor": 0}),
        (
            "quote/S-1/W-100",
            {
                "unit_price_minor": 1,
                "currency": "USD",
                "min_quantity": 1,
                "lead_time_days": 1,
                "status": 7,
            },
        ),
        ("budget/CC-OPS", ["limit_minor"]),
    ],
)
def test_a_fact_value_fits_its_kind(
    make_sim: SimFactory, admin: Connection, key: str, value: object
) -> None:
    make_sim()
    seq = _event(admin, "fact_changed", 0)
    _refused(
        admin,
        psycopg.errors.CheckViolation,
        "INSERT INTO sim.fact_version (key, version, value, valid_from_seq, valid_from_vt,"
        " cause, cause_actor) VALUES (%s, 2, %s, %s, 0, 'control', 'test')",
        key,
        Jsonb(value),
        seq,
    )


def test_a_version_names_its_cause(make_sim: SimFactory, admin: Connection) -> None:
    """A change names its actor, a later version is never initial, and an effect's
    consequence names its effect."""
    make_sim()
    insert: LiteralString = (
        "INSERT INTO sim.fact_version (key, version, value, valid_from_seq, valid_from_vt,"
        " cause, cause_effect_id, cause_actor) VALUES ('budget/CC-OPS', 2, %s, %s, 0, %s,"
        " NULL, NULL)"
    )
    value = Jsonb({"limit_minor": 5, "committed_minor": 0})
    changed = _event(admin, "fact_changed", 0)
    _refused(admin, psycopg.errors.CheckViolation, insert, value, changed, "control")
    _refused(
        admin, psycopg.errors.CheckViolation, insert, value, _event(admin, "setup", 0), "initial"
    )
    committed = _event(admin, "write_committed", 0)
    _refused(admin, psycopg.errors.RaiseException, insert, value, committed, "effect")


def test_a_change_comes_from_a_fact_changed_event_by_its_actor(
    make_sim: SimFactory, admin: Connection
) -> None:
    make_sim()
    insert: LiteralString = (
        "INSERT INTO sim.fact_version (key, version, value, valid_from_seq, valid_from_vt,"
        " cause, cause_actor) VALUES ('budget/CC-OPS', 2, %s, %s, 0, 'control', 'someone')"
    )
    value = Jsonb({"limit_minor": 5, "committed_minor": 0})
    _refused(admin, psycopg.errors.RaiseException, insert, value, _event(admin, "read", 0))
    _refused(admin, psycopg.errors.RaiseException, insert, value, _event(admin, "fact_changed", 0))


def test_an_effects_consequence_is_exact(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    received = _received(sim)
    with pytest.raises(psycopg.errors.RaiseException, match="consequence"), admin.transaction():
        _commit_by_hand(admin, received, 10, committed_minor=73_999)
    with admin.transaction():
        _commit_by_hand(admin, received, 10)


def test_an_effect_needs_its_consequence_and_its_committed_write(
    make_sim: SimFactory, admin: Connection
) -> None:
    sim = make_sim()
    received = _received(sim)
    with (
        pytest.raises(psycopg.errors.RaiseException, match="needs its consequence"),
        admin.transaction(),
    ):
        _commit_by_hand(admin, received, 10, committed_minor=None)
    with (
        pytest.raises(psycopg.errors.RaiseException, match="needs its consequence"),
        admin.transaction(),
    ):
        _commit_by_hand(admin, received, 10, finish=False)
    assert _one(admin, "SELECT count(*) FROM sim.effect") == 0


# Reads -----------------------------------------------------------------------------------


def test_a_read_serves_the_latest_version(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    sim.control.change_fact("quote/S-1/W-100", {"unit_price_minor": 567}, actor="F7", vt=0)
    seq = _event(admin, "read", 0, actor="gateway")
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "INSERT INTO sim.read_log (seq, key, version, caller, vt)"
        " VALUES (%s, 'quote/S-1/W-100', 1, 'gateway', 0)",
        seq,
    )


def test_a_read_matches_its_event(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    insert: LiteralString = (
        "INSERT INTO sim.read_log (seq, key, version, caller, vt)"
        " VALUES (%s, 'quote/S-1/W-100', 1, 'gateway', 0)"
    )
    _refused(admin, psycopg.errors.RaiseException, insert, _event(admin, "status", 0, "gateway"))
    _refused(admin, psycopg.errors.RaiseException, insert, _event(admin, "read", 0, "agent"))


# Writes ----------------------------------------------------------------------------------


def _insert_write(
    conn: Connection,
    *,
    vt: int = 0,
    key: str | None = "A-0001",
    honoured: bool = True,
    deadline: int = 10_000,
    state: str = "received",
    outcome_seq: int | None = None,
    effect_id: str | None = None,
) -> int:
    seq = _event(conn, "write_received", vt, actor="gateway")
    conn.execute(
        "INSERT INTO sim.write (received_seq, received_vt, caller, operation, intent_id,"
        " approval_id, args, basis, idempotency_key, key_honoured, client_ref, deadline_vt,"
        " deadline_waived, state, outcome_seq, outcome_vt, effect_id) VALUES (%s, %s,"
        " 'gateway', 'submit_purchase_order', 'REQ-1', 'APR-0001', %s, %s, %s, %s, NULL, %s,"
        " false, %s, %s, %s, %s)",
        (
            seq,
            vt,
            Jsonb(ORDER_ARGS),
            Jsonb(ORDER.basis),
            key,
            honoured,
            deadline,
            state,
            outcome_seq,
            None if outcome_seq is None else vt,
            effect_id,
        ),
    )
    return seq


def test_a_write_has_the_destinations_deadline(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    with pytest.raises(psycopg.errors.RaiseException, match="deadline"), admin.transaction():
        _insert_write(admin, deadline=10_001)
    with admin.transaction():
        _insert_write(admin, deadline=10_000)


@pytest.mark.parametrize(
    ("contract", "honoured"), [(Contract.C2, False), (Contract.C0, True), (Contract.C1, True)]
)
def test_a_write_keeps_its_key_exactly_when_the_destination_honours_keys(
    make_sim: SimFactory, admin: Connection, contract: Contract, honoured: bool
) -> None:
    make_sim(contract)
    with pytest.raises(psycopg.errors.RaiseException, match="key rules"), admin.transaction():
        _insert_write(admin, honoured=honoured)
    with pytest.raises(psycopg.errors.RaiseException, match="key rules"), admin.transaction():
        _insert_write(admin, key=None, honoured=True)


def test_a_key_is_in_flight_once(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    _insert_write(admin)
    with pytest.raises(psycopg.errors.UniqueViolation), admin.transaction():
        _insert_write(admin)


def test_a_write_cannot_start_committed_or_aborted(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    for state in ("committed", "aborted"):
        with (
            pytest.raises(psycopg.errors.RaiseException, match="received before"),
            admin.transaction(),
        ):
            _insert_write(admin, state=state, outcome_seq=_event(admin, f"write_{state}", 0))


def test_a_write_is_in_progress_only_while_its_key_is_in_flight(
    make_sim: SimFactory, admin: Connection
) -> None:
    make_sim()
    with pytest.raises(psycopg.errors.RaiseException, match="in progress"), admin.transaction():
        _insert_write(admin, state="in_progress", outcome_seq=_event(admin, "write_in_progress", 0))


def test_a_write_replays_only_the_effect_of_its_key(
    make_sim: SimFactory, admin: Connection
) -> None:
    sim = make_sim()
    sim.submit_purchase_order(
        ORDER.model_copy(update={"idempotency_key": "A-0009"}), vt=0, caller="gateway"
    )
    with pytest.raises(psycopg.errors.RaiseException, match="replays only"), admin.transaction():
        _insert_write(
            admin,
            state="replayed",
            outcome_seq=_event(admin, "write_replayed", 0),
            effect_id="PO-0001",
        )


def test_a_write_outcome_matches_its_event(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    received = _insert_write(admin)
    outcome = _event(admin, "write_rejected", 0)
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "UPDATE sim.write SET state = 'committed', outcome_seq = %s, outcome_vt = 0,"
        " effect_id = NULL WHERE received_seq = %s",
        outcome,
        received,
    )


def test_a_write_never_changes_what_it_asked_for(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    received = _received(sim)
    assignments: list[LiteralString] = ["intent_id = 'REQ-2'", "args = '{}'"]
    assignments.append("deadline_waived = true")
    for assignment in assignments:
        with pytest.raises(psycopg.errors.RaiseException, match="never changes"):
            admin.execute(
                "UPDATE sim.write SET " + assignment + " WHERE received_seq = %s", (received,)
            )


def test_a_write_has_one_outcome(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    sim.submit_purchase_order(ORDER, vt=0, caller="gateway")
    outcome = _event(admin, "write_aborted", 20_000)
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "UPDATE sim.write SET state = 'aborted', outcome_seq = %s, outcome_vt = 20000,"
        " effect_id = NULL",
        outcome,
    )


def test_a_write_aborts_only_after_its_deadline(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    received = _received(sim)
    abort: LiteralString = (
        "UPDATE sim.write SET state = 'aborted', outcome_seq = %s, outcome_vt = %s"
        " WHERE received_seq = %s"
    )
    _refused(
        admin,
        psycopg.errors.RaiseException,
        abort,
        _event(admin, "write_aborted", 10_000),
        10_000,
        received,
    )
    admin.execute(abort, (_event(admin, "write_aborted", 10_001), 10_001, received))


def test_a_waived_deadline_never_aborts(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    answer = sim.receive_write(ORDER, vt=0, waive_deadline=True, caller="gateway")
    assert isinstance(answer, WriteReceived)
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "UPDATE sim.write SET state = 'aborted', outcome_seq = %s, outcome_vt = 20000"
        " WHERE received_seq = %s",
        _event(admin, "write_aborted", 20_000),
        answer.received_seq,
    )


# Effects ---------------------------------------------------------------------------------


def test_an_effect_repeats_its_write(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    received = _received(sim)
    for args in ({**ORDER_ARGS, "quantity": 99}, {**ORDER_ARGS, "unit_price_minor": 1}):
        with (
            pytest.raises(psycopg.errors.RaiseException, match="does not repeat"),
            admin.transaction(),
        ):
            _commit_by_hand(admin, received, 10, args=args)


@pytest.mark.parametrize(("contract", "key"), [(Contract.C2, None), (Contract.C0, "A-0001")])
def test_an_effect_stores_the_key_exactly_when_the_destination_honours_it(
    make_sim: SimFactory, admin: Connection, contract: Contract, key: str | None
) -> None:
    sim = make_sim(contract)
    received = _received(sim)
    with pytest.raises(psycopg.errors.RaiseException, match="does not repeat"), admin.transaction():
        _commit_by_hand(admin, received, 10, key=key)


def test_a_key_commits_once(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    first = sim.submit_purchase_order(ORDER, vt=0, caller="gateway")
    assert isinstance(first, WriteCommitted)
    second = _insert_write(admin)
    with pytest.raises(psycopg.errors.UniqueViolation), admin.transaction():
        _commit_by_hand(admin, second, 10, effect_id="PO-0002", committed_minor=128_000)


def test_an_effect_commits_by_its_deadline(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    received = _received(sim)
    with (
        pytest.raises(psycopg.errors.RaiseException, match="after its deadline"),
        admin.transaction(),
    ):
        _commit_by_hand(admin, received, 10_001)
    with admin.transaction():
        _commit_by_hand(admin, received, 10_000)


def test_a_waived_deadline_lets_an_effect_commit_late(
    make_sim: SimFactory, admin: Connection
) -> None:
    sim = make_sim()
    answer = sim.receive_write(ORDER, vt=0, waive_deadline=True, caller="gateway")
    assert isinstance(answer, WriteReceived)
    with admin.transaction():
        _commit_by_hand(admin, answer.received_seq, 15_000)


def test_an_effect_id_names_its_operation(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    received = _received(sim)
    with pytest.raises(psycopg.errors.CheckViolation), admin.transaction():
        _commit_by_hand(admin, received, 10, effect_id="RS-0001")


@pytest.mark.parametrize("state", ["unknown", "used", "revoked", "expired"])
def test_at_c3_an_effect_needs_a_valid_unused_approval(
    make_sim: SimFactory, admin: Connection, state: str
) -> None:
    sim = make_sim(Contract.C3)
    if state != "unknown":
        _approve(sim)
    effect_id, committed = "PO-0001", 74_000
    if state == "used":
        sim.get_quote(GetQuote(supplier_id="S-1", sku="W-100"), vt=0, caller="gateway")
        sim.get_budget(GetBudget(cost_center="CC-OPS"), vt=0, caller="gateway")
        first = sim.submit_purchase_order(
            ORDER.model_copy(update={"idempotency_key": "A-0007"}), vt=0, caller="gateway"
        )
        assert isinstance(first, WriteCommitted)
        effect_id, committed = "PO-0002", 128_000
    if state == "revoked":
        sim.control.revoke("APR-0001", actor="F6", vt=0)
    vt = 3_600_000 if state == "expired" else 0
    received = _received(sim, vt=vt)
    with (
        pytest.raises(psycopg.errors.RaiseException, match="valid unused approval"),
        admin.transaction(),
    ):
        _commit_by_hand(admin, received, vt, effect_id=effect_id, committed_minor=committed)


# Approvals -------------------------------------------------------------------------------


def test_an_approval_is_filed_pending(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    seq = _event(admin, "approval_requested", 0, actor="gateway")
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "INSERT INTO sim.approval (approval_id, intent_id, operation, scope, requester,"
        " requested_seq, requested_vt, status, decided_seq, decided_vt, decided_by, expires_at)"
        " VALUES ('APR-0001', 'REQ-1', 'submit_purchase_order', '{}', 'gateway', %s, 0,"
        " 'approved', %s, 0, 'policy', 3600000)",
        seq,
        seq,
    )


def test_an_approvals_request_never_changes(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    _approve(sim)
    assignments: list[LiteralString] = ["scope = '{}'", "intent_id = 'REQ-2'"]
    assignments.append("requester = 'agent'")
    for assignment in assignments:
        with pytest.raises(psycopg.errors.RaiseException, match="never changes"):
            admin.execute("UPDATE sim.approval SET " + assignment)


def test_an_approval_moves_only_forward(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    _approve(sim)
    with pytest.raises(psycopg.errors.RaiseException, match="cannot move"):
        admin.execute("UPDATE sim.approval SET status = 'denied', expires_at = NULL")


def test_a_decision_needs_its_approval_event(make_sim: SimFactory, admin: Connection) -> None:
    make_sim()
    requested = _event(admin, "approval_requested", 0, actor="gateway")
    admin.execute(
        "INSERT INTO sim.approval (approval_id, intent_id, operation, scope, requester,"
        " requested_seq, requested_vt, status) VALUES ('APR-0001', 'REQ-1',"
        " 'submit_purchase_order', '{}', 'gateway', %s, 0, 'pending')",
        (requested,),
    )
    decided = _event(admin, "approval_approved", 0, actor="approver")
    decide: LiteralString = (
        "UPDATE sim.approval SET status = 'approved', decided_seq = %s, decided_vt = 0,"
        " decided_by = 'approver', expires_at = %s"
    )
    _refused(admin, psycopg.errors.RaiseException, decide, decided, 3_600_000)
    admin.execute(
        "INSERT INTO sim.approval_event (seq, approval_id, kind, actor, vt)"
        " VALUES (%s, 'APR-0001', 'approved', 'approver', 0)",
        (decided,),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="must expire"), admin.transaction():
        admin.execute(decide, (decided, 3_600_001))
    admin.execute(decide, (decided, 3_600_000))


def test_a_decision_never_changes(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    _approve(sim)
    assignments: list[LiteralString] = ["decided_by = 'approver'", "expires_at = expires_at + 1"]
    for assignment in assignments:
        with pytest.raises(psycopg.errors.RaiseException, match="never changes"):
            admin.execute("UPDATE sim.approval SET " + assignment)


def test_a_revocation_needs_its_event(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    _approve(sim)
    seq = _event(admin, "approval_revoked", 0, actor="F6")
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "UPDATE sim.approval SET status = 'revoked', revoked_seq = %s, revoked_vt = 0,"
        " revoked_by = 'F6'",
        seq,
    )


def test_an_approval_event_matches_its_event(make_sim: SimFactory, admin: Connection) -> None:
    sim = make_sim()
    _approve(sim)
    _refused(
        admin,
        psycopg.errors.RaiseException,
        "INSERT INTO sim.approval_event (seq, approval_id, kind, actor, vt)"
        " VALUES (%s, 'APR-0001', 'revoked', 'F6', 0)",
        _event(admin, "approval_approved", 0, actor="F6"),
    )


def test_an_approval_backs_one_effect_that_cites_it(
    make_sim: SimFactory, admin: Connection
) -> None:
    sim = make_sim()
    _approve(sim)
    other = ORDER.model_copy(update={"approval_id": "APR-0009", "idempotency_key": "A-0002"})
    committed = sim.submit_purchase_order(other, vt=0, caller="gateway")
    assert isinstance(committed, WriteCommitted)
    _refused(admin, psycopg.errors.RaiseException, "UPDATE sim.approval SET effect_id = 'PO-0001'")
    sim.submit_purchase_order(ORDER, vt=0, caller="gateway")
    assert _one(admin, "SELECT effect_id FROM sim.approval") == "PO-0002"
    _refused(admin, psycopg.errors.RaiseException, "UPDATE sim.approval SET effect_id = NULL")


# Ground truth is never deleted ------------------------------------------------------------


@pytest.mark.parametrize("user", [None, "ff_sim"])
def test_the_logs_are_append_only_for_every_role(
    make_sim: SimFactory, sim_database: str, user: str | None
) -> None:
    sim = make_sim()
    _approve(sim)
    sim.get_quote(GetQuote(supplier_id="S-1", sku="W-100"), vt=0, caller="gateway")
    sim.submit_purchase_order(ORDER, vt=0, caller="gateway")
    conninfo = sim_database if user is None else make_conninfo(sim_database, user=user)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        for table, assignment in APPEND_ONLY.items():
            with pytest.raises(psycopg.errors.RaiseException, match="ground truth"):
                conn.execute("UPDATE sim." + table + " SET " + assignment)
            with pytest.raises(psycopg.errors.RaiseException, match="ground truth"):
                conn.execute("DELETE FROM sim." + table)
            assert _one(conn, "SELECT count(*) FROM sim." + table) > 0


@pytest.mark.parametrize("user", [None, "ff_sim"])
def test_ground_truth_is_never_deleted_or_truncated(
    make_sim: SimFactory, sim_database: str, user: str | None
) -> None:
    """The admin role meets the triggers; ff_sim has no TRUNCATE privilege at all."""
    sim = make_sim()
    _approve(sim)
    sim.submit_purchase_order(ORDER, vt=0, caller="gateway")
    conninfo = sim_database if user is None else make_conninfo(sim_database, user=user)
    truncate_error: type[Exception] = (
        psycopg.errors.RaiseException if user is None else psycopg.errors.InsufficientPrivilege
    )
    with psycopg.connect(conninfo, autocommit=True) as conn:
        mutable: list[LiteralString] = ["run", "write", "approval"]
        for table in mutable:
            with pytest.raises(psycopg.errors.RaiseException, match="ground truth"):
                conn.execute("DELETE FROM sim." + table)
        for table in TABLES:
            with pytest.raises(truncate_error):
                conn.execute("TRUNCATE sim." + table + " CASCADE")
        assert _one(conn, "SELECT count(*) FROM sim.effect") == 1
