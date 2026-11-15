"""The simulator's behaviour, through its API and control interface (0002, 0004, 0005, 0009).

Each test sets up a fresh run of the dev-001 scenario, calls the simulator as the gateway
or the agent would, and checks both the answer and the ground truth in the sim tables.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import JsonValue

from fivefold.domain.api import (
    ApprovalAnswer,
    CheckName,
    FactRead,
    FailedCheck,
    GetApprovalStatus,
    GetBudget,
    GetInventory,
    GetOrderStatus,
    GetQuote,
    GetReservationStatus,
    ListSuppliers,
    PurchaseOrderScope,
    Refusal,
    RefusalReason,
    RequestApproval,
    ReservationScope,
    ReserveInventory,
    StatusAnswer,
    StatusEffect,
    SubmitPurchaseOrder,
    WriteAborted,
    WriteCommitted,
    WriteInProgress,
    WriteReceived,
    WriteRejected,
    WriteRejectReason,
)
from fivefold.domain.authority import ApprovalStatus, Decision
from fivefold.domain.contracts import Contract
from fivefold.domain.rules import Operation
from fivefold.domain.scenario import scenario_from_data
from fivefold.sim import (
    AlreadySetUp,
    ClockError,
    ControlError,
    NotSetUp,
    SimConfig,
    Simulator,
    WrongRole,
    setup_run,
)

pytestmark = pytest.mark.pg

SimFactory = Callable[..., Simulator]
Data = dict[str, Any]
GW = "gateway"
TTL = 3_600_000
ORDER_ARGS: dict[str, str | int] = {
    "supplier_id": "S-1",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 540,
    "currency": "USD",
    "cost_center": "CC-OPS",
}
QUOTE_V1: dict[str, JsonValue] = {
    "unit_price_minor": 540,
    "currency": "USD",
    "min_quantity": 10,
    "lead_time_days": 5,
    "status": "active",
}


def _order(**changes: object) -> SubmitPurchaseOrder:
    data: Data = {
        "intent_id": "REQ-1",
        "approval_id": "APR-0001",
        "basis": {"quote/S-1/W-100": 1, "budget/CC-OPS": 1},
        "idempotency_key": "A-0001",
        **ORDER_ARGS,
    }
    data.update(changes)
    return SubmitPurchaseOrder.model_validate(data)


def _reservation(**changes: object) -> ReserveInventory:
    data: Data = {
        "intent_id": "REQ-2",
        "approval_id": "APR-0001",
        "basis": {"inventory/WH-1/W-100": 1},
        "idempotency_key": "A-0002",
        "warehouse_id": "WH-1",
        "sku": "W-100",
        "quantity": 30,
    }
    data.update(changes)
    return ReserveInventory.model_validate(data)


def _scope(**changes: object) -> PurchaseOrderScope:
    data: Data = {
        "supplier_id": "S-1",
        "sku": "W-100",
        "max_quantity": 100,
        "max_amount_minor": 54_000,
        "currency": "USD",
        "cost_center": "CC-OPS",
    }
    data.update(changes)
    return PurchaseOrderScope.model_validate(data)


def _request(
    sim: Simulator,
    vt: int = 0,
    *,
    key: str | None = None,
    scope: dict[str, object] | None = None,
) -> ApprovalAnswer:
    answer = sim.request_approval(
        RequestApproval(
            intent_id="REQ-1",
            operation=Operation.SUBMIT_PURCHASE_ORDER,
            scope=_scope(**(scope or {})),
            client_request_key=key,
        ),
        caller=GW,
        vt=vt,
    )
    assert isinstance(answer, ApprovalAnswer)
    return answer


def _read_inputs(sim: Simulator, vt: int = 0, caller: str = GW) -> dict[str, int]:
    """Read the order's quote and budget, and return them as a basis."""
    quote = sim.get_quote(GetQuote(supplier_id="S-1", sku="W-100"), caller=caller, vt=vt)
    budget = sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=caller, vt=vt)
    assert isinstance(quote, FactRead) and isinstance(budget, FactRead)
    return {quote.key: quote.version, budget.key: budget.version}


def _committed(answer: object) -> WriteCommitted:
    assert isinstance(answer, WriteCommitted), answer
    return answer


def _rejected(answer: object) -> WriteRejected:
    assert isinstance(answer, WriteRejected), answer
    return answer


def _rows(dsn: str, query: LiteralString, *params: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def _count(dsn: str, table: LiteralString) -> int:
    return int(_rows(dsn, "SELECT count(*) FROM sim." + table)[0][0])


def _budget(dsn: str) -> list[tuple[Any, ...]]:
    return _rows(
        dsn,
        "SELECT version, value ->> 'committed_minor', cause, cause_effect_id FROM"
        " sim.fact_version WHERE key = 'budget/CC-OPS' ORDER BY version",
    )


def _with_stock(data: Data, available: int = 50) -> Data:
    data["facts"]["inventory/WH-1/W-100"] = {"available": available}
    return data


# Setup -----------------------------------------------------------------------------------


def test_setup_records_the_initial_facts_at_version_1(
    make_sim: SimFactory, sim_database: str
) -> None:
    make_sim(vt=7)
    assert _rows(sim_database, "SELECT seq, vt, kind, actor, ref FROM sim.event") == [
        (1, 7, "setup", "simulator", "dev-001-single-po")
    ]
    facts = _rows(
        sim_database,
        "SELECT key, version, valid_from_seq, valid_from_vt, cause FROM sim.fact_version"
        " ORDER BY key",
    )
    assert facts == [
        ("budget/CC-OPS", 1, 1, 7, "initial"),
        ("quote/S-1/W-100", 1, 1, 7, "initial"),
        ("quote/S-2/W-100", 1, 1, 7, "initial"),
        ("suppliers", 1, 1, 7, "initial"),
    ]
    run = _rows(
        sim_database,
        "SELECT scenario_id, task_id, capabilities, commit_deadline_ms, credentials FROM sim.run",
    )
    assert run == [("dev-001-single-po", "TASK-dev-001", ["idempotency_key"], 10_000, ["gateway"])]


def test_the_supplier_directory_lists_quoted_and_allowlisted_suppliers(
    make_sim: SimFactory, dev_data: Data
) -> None:
    dev_data["authority"]["allowlist"] = ["S-1", "S-3"]
    sim = make_sim()
    answer = sim.list_suppliers(ListSuppliers(), caller=GW, vt=0)
    assert answer == FactRead(
        key="suppliers",
        version=1,
        data={
            "suppliers": [
                {"supplier_id": "S-1", "allowlisted": True},
                {"supplier_id": "S-2", "allowlisted": False},
                {"supplier_id": "S-3", "allowlisted": True},
            ]
        },
    )


def test_a_run_is_set_up_once(make_sim: SimFactory, sim_database: str, dev_data: Data) -> None:
    make_sim()
    config = SimConfig.preset(Contract.C2, credentials=[GW])
    with pytest.raises(AlreadySetUp):
        setup_run(make_conninfo(sim_database, user="ff_sim"), scenario_from_data(dev_data), config)


@pytest.mark.parametrize("user", [None, "ff_gw", "ff_harness"])
def test_the_simulator_connects_only_as_ff_sim(
    make_sim: SimFactory, sim_database: str, dev_data: Data, user: str | None
) -> None:
    make_sim()
    conninfo = sim_database if user is None else make_conninfo(sim_database, user=user)
    with pytest.raises(WrongRole):
        Simulator(conninfo)
    config = SimConfig.preset(Contract.C2, credentials=[GW])
    with pytest.raises(WrongRole):
        setup_run(conninfo, scenario_from_data(dev_data), config)


def test_a_run_must_be_set_up_before_it_opens(sim_database: str) -> None:
    with pytest.raises(NotSetUp):
        Simulator(make_conninfo(sim_database, user="ff_sim"))


def test_a_reopened_simulator_has_the_runs_configuration(
    make_sim: SimFactory, sim_database: str
) -> None:
    first = make_sim(Contract.C3, credentials=["gateway", "auditor"])
    with Simulator(make_conninfo(sim_database, user="ff_sim")) as second:
        assert second.config == first.config
        assert second.authority == first.authority
        assert (second.scenario_id, second.task_id) == ("dev-001-single-po", "TASK-dev-001")


# Reads -----------------------------------------------------------------------------------


def test_a_read_serves_the_current_version_and_logs_its_caller(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim()
    first = sim.get_quote(GetQuote(supplier_id="S-1", sku="W-100"), caller=GW, vt=10)
    assert first == FactRead(key="quote/S-1/W-100", version=1, data=QUOTE_V1)
    sim.control.change_fact("quote/S-1/W-100", {"unit_price_minor": 567}, actor="F7", vt=20)
    second = sim.get_quote(GetQuote(supplier_id="S-1", sku="W-100"), caller=GW, vt=30)
    assert isinstance(second, FactRead)
    assert (second.version, second.data["unit_price_minor"]) == (2, 567)
    assert _rows(sim_database, "SELECT seq, key, version, caller, vt FROM sim.read_log") == [
        (2, "quote/S-1/W-100", 1, GW, 10),
        (4, "quote/S-1/W-100", 2, GW, 30),
    ]


def test_every_kind_of_fact_can_be_read(make_sim: SimFactory, dev_data: Data) -> None:
    sim = make_sim(data=_with_stock(dev_data))
    budget = sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=GW, vt=0)
    stock = sim.get_inventory(GetInventory(warehouse_id="WH-1", sku="W-100"), caller=GW, vt=0)
    assert budget == FactRead(
        key="budget/CC-OPS", version=1, data={"limit_minor": 100_000, "committed_minor": 20_000}
    )
    assert stock == FactRead(key="inventory/WH-1/W-100", version=1, data={"available": 50})


def test_a_missing_fact_is_not_found_and_nothing_is_logged(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim()
    answer = sim.get_quote(GetQuote(supplier_id="S-9", sku="W-100"), caller=GW, vt=0)
    assert isinstance(answer, Refusal) and answer.reason is RefusalReason.NOT_FOUND
    assert _count(sim_database, "read_log") == 0
    assert _count(sim_database, "event") == 1


# Trust boundary (0008) -------------------------------------------------------------------


def test_in_g_modes_the_simulator_refuses_the_agent(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim(Contract.C1)
    agent = "agent"
    refused = [
        sim.get_quote(GetQuote(supplier_id="S-1", sku="W-100"), caller=agent, vt=0),
        sim.list_suppliers(ListSuppliers(), caller=agent, vt=0),
        sim.request_approval(
            RequestApproval(
                intent_id="REQ-1", operation=Operation.SUBMIT_PURCHASE_ORDER, scope=_scope()
            ),
            caller=agent,
            vt=0,
        ),
        sim.get_approval_status(GetApprovalStatus(approval_id="APR-0001"), caller=agent, vt=0),
        sim.get_order_status(GetOrderStatus(intent_id="REQ-1"), caller=agent, vt=0),
    ]
    for answer in refused:
        assert isinstance(answer, Refusal) and answer.reason is RefusalReason.FORBIDDEN
    writes = [
        sim.submit_purchase_order(_order(), caller=agent, vt=0),
        sim.reserve_inventory(_reservation(), caller=agent, vt=0),
        sim.receive_write(_order(), caller=agent, vt=0),
    ]
    for write in writes:
        assert _rejected(write).reason is WriteRejectReason.FORBIDDEN
    for table in ("effect", "write", "approval", "read_log"):
        assert _count(sim_database, table) == 0
    assert _count(sim_database, "event") == 1


def test_in_baselines_the_agent_holds_the_credential(make_sim: SimFactory) -> None:
    sim = make_sim(Contract.C2, credentials=["agent-1", "agent-2"])
    assert _committed(sim.submit_purchase_order(_order(), caller="agent-2", vt=0)).effect_id
    gateway = sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=GW, vt=0)
    assert isinstance(gateway, Refusal) and gateway.reason is RefusalReason.FORBIDDEN


# Virtual time ----------------------------------------------------------------------------


def test_virtual_time_never_goes_back(make_sim: SimFactory, sim_database: str) -> None:
    sim = make_sim()
    sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=GW, vt=100)
    with pytest.raises(ClockError):
        sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=GW, vt=99)
    with pytest.raises(ClockError):
        sim.control.change_fact("budget/CC-OPS", {"limit_minor": 1}, actor="F7", vt=99)
    for vt in (-1, True, 1.5):
        with pytest.raises(ValueError, match="virtual time"):
            sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=GW, vt=vt)  # type: ignore[arg-type]
    assert _count(sim_database, "event") == 2


# Fact history ----------------------------------------------------------------------------


def test_a_committed_order_charges_its_budget_as_a_new_version(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim()
    committed = _committed(sim.submit_purchase_order(_order(), caller=GW, vt=40))
    assert committed == WriteCommitted(
        effect_id="PO-0001",
        replay=False,
        intent_id="REQ-1",
        operation=Operation.SUBMIT_PURCHASE_ORDER,
        args=ORDER_ARGS,
        committed_vt=40,
    )
    assert list(committed.args) == list(ORDER_ARGS)
    assert _budget(sim_database) == [
        (1, "20000", "initial", None),
        (2, "74000", "effect", "PO-0001"),
    ]
    commit_seq = _rows(sim_database, "SELECT commit_seq FROM sim.effect")[0][0]
    assert _rows(
        sim_database,
        "SELECT valid_from_seq, valid_from_vt FROM sim.fact_version WHERE cause = 'effect'",
    ) == [(commit_seq, 40)]


def test_a_reservation_takes_stock_as_a_new_version(
    make_sim: SimFactory, sim_database: str, dev_data: Data
) -> None:
    sim = make_sim(data=_with_stock(dev_data))
    committed = _committed(sim.reserve_inventory(_reservation(), caller=GW, vt=0))
    assert (committed.effect_id, committed.operation) == ("RS-0001", Operation.RESERVE_INVENTORY)
    assert committed.args == {"warehouse_id": "WH-1", "sku": "W-100", "quantity": 30}
    assert _rows(
        sim_database,
        "SELECT version, value, cause_effect_id FROM sim.fact_version"
        " WHERE key = 'inventory/WH-1/W-100' ORDER BY version",
    ) == [(1, {"available": 50}, None), (2, {"available": 20}, "RS-0001")]


def test_a_control_change_appends_a_version_with_its_actor(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim()
    answer = sim.control.change_fact(
        "quote/S-1/W-100", {"lead_time_days": 9}, actor="fault:F7", vt=5
    )
    assert answer == FactRead(
        key="quote/S-1/W-100", version=2, data={**QUOTE_V1, "lead_time_days": 9}
    )
    assert _rows(
        sim_database,
        "SELECT version, valid_from_seq, valid_from_vt, cause, cause_actor FROM sim.fact_version"
        " WHERE key = 'quote/S-1/W-100' ORDER BY version",
    ) == [(1, 1, 0, "initial", None), (2, 2, 5, "control", "fault:F7")]
    assert _rows(sim_database, "SELECT kind, actor, ref FROM sim.event WHERE seq = 2") == [
        ("fact_changed", "fault:F7", "quote/S-1/W-100")
    ]
    assert _count(sim_database, "read_log") == 0


@pytest.mark.parametrize(
    ("key", "changes", "message"),
    [
        ("quote/S-9/W-100", {"unit_price_minor": 1}, "no fact"),
        ("suppliers", {"suppliers": 1}, "never changes"),
        ("price/S-1", {"unit_price_minor": 1}, "not a fact key"),
        ("quote/S-1/W-100", {}, "at least one field"),
        ("quote/S-1/W-100", {"discount": 5}, "has no fields"),
        ("quote/S-1/W-100", {"unit_price_minor": "cheap"}, "breaks fact"),
        ("budget/CC-OPS", {"committed_minor": -1}, "breaks fact"),
        ("quote/S-1/W-100", {"unit_price_minor": 540}, "leaves fact"),
    ],
)
def test_a_control_change_must_change_a_known_field_validly(
    make_sim: SimFactory,
    sim_database: str,
    key: str,
    changes: dict[str, str | int],
    message: str,
) -> None:
    sim = make_sim()
    with pytest.raises(ControlError, match=message):
        sim.control.change_fact(key, changes, actor="F7", vt=0)
    assert _count(sim_database, "event") == 1


# Writes without capabilities (C0) --------------------------------------------------------


def test_without_capabilities_the_destination_simply_commits(
    make_sim: SimFactory, sim_database: str
) -> None:
    """No key, no checks: a resend commits again, cites the same approval, overdraws."""
    sim = make_sim(Contract.C0)
    stale = _order(approval_id="APR-0404", basis={})
    first = _committed(sim.submit_purchase_order(stale, caller=GW, vt=0))
    second = _committed(sim.submit_purchase_order(stale, caller=GW, vt=10))
    assert (first.effect_id, second.effect_id) == ("PO-0001", "PO-0002")
    assert not second.replay
    assert _rows(sim_database, "SELECT idempotency_key FROM sim.effect") == [(None,), (None,)]
    assert _rows(sim_database, "SELECT key_honoured, idempotency_key FROM sim.write") == [
        (False, "A-0001"),
        (False, "A-0001"),
    ]
    assert _budget(sim_database)[-1][:2] == (3, "128000")


def test_a_write_to_a_budget_that_does_not_exist_is_rejected(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim(Contract.C0)
    answer = _rejected(sim.submit_purchase_order(_order(cost_center="CC-RND"), caller=GW, vt=0))
    assert answer.reason is WriteRejectReason.UNKNOWN_FACT
    assert answer.failed_checks == []
    assert _rows(sim_database, "SELECT state, rejection ->> 'reason' FROM sim.write") == [
        ("rejected", "unknown_fact")
    ]
    assert _count(sim_database, "effect") == 0


# Idempotency keys (C2) -------------------------------------------------------------------


def test_a_committed_key_gets_the_stored_result_as_a_replay(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim(Contract.C2)
    first = _committed(sim.submit_purchase_order(_order(), caller=GW, vt=40))
    retry = _order(quantity=1, client_ref="A-0001/2")
    replay = _committed(sim.submit_purchase_order(retry, caller=GW, vt=4_000))
    assert replay == first.model_copy(update={"replay": True})
    assert _count(sim_database, "effect") == 1
    assert len(_budget(sim_database)) == 2
    assert _rows(sim_database, "SELECT state, effect_id FROM sim.write ORDER BY received_seq") == [
        ("committed", "PO-0001"),
        ("replayed", "PO-0001"),
    ]
    assert _rows(sim_database, "SELECT idempotency_key FROM sim.effect") == [("A-0001",)]


def test_a_write_without_a_key_is_not_deduplicated(make_sim: SimFactory) -> None:
    sim = make_sim(Contract.C2)
    plain = _order(idempotency_key=None)
    first = _committed(sim.submit_purchase_order(plain, caller=GW, vt=0))
    second = _committed(sim.submit_purchase_order(plain, caller=GW, vt=0))
    assert first.effect_id != second.effect_id


def test_a_key_in_flight_gets_in_progress(make_sim: SimFactory, sim_database: str) -> None:
    sim = make_sim(Contract.C2)
    received = sim.receive_write(_order(), caller=GW, vt=0)
    assert isinstance(received, WriteReceived)
    assert received.deadline_vt == 10_000
    assert sim.submit_purchase_order(_order(), caller=GW, vt=2_000) == WriteInProgress(
        idempotency_key="A-0001"
    )
    assert sim.receive_write(_order(), caller=GW, vt=2_500) == WriteInProgress(
        idempotency_key="A-0001"
    )
    committed = _committed(sim.commit_received(received.received_seq, vt=5_000))
    assert (committed.effect_id, committed.committed_vt, committed.replay) == (
        "PO-0001",
        5_000,
        False,
    )
    assert _committed(sim.submit_purchase_order(_order(), caller=GW, vt=6_000)).replay
    assert _count(sim_database, "effect") == 1
    assert _rows(sim_database, "SELECT state FROM sim.write ORDER BY received_seq") == [
        ("committed",),
        ("in_progress",),
        ("in_progress",),
        ("replayed",),
    ]


@pytest.mark.parametrize("contract", [Contract.C0, Contract.C1])
def test_without_the_key_capability_a_duplicate_commits_twice(
    make_sim: SimFactory, sim_database: str, contract: Contract
) -> None:
    sim = make_sim(contract)
    received = sim.receive_write(_order(), caller=GW, vt=0)
    assert isinstance(received, WriteReceived)
    _committed(sim.submit_purchase_order(_order(), caller=GW, vt=2_000))
    _committed(sim.commit_received(received.received_seq, vt=5_000))
    assert _count(sim_database, "effect") == 2


def test_the_split_path_answers_a_replay_at_once(make_sim: SimFactory) -> None:
    sim = make_sim(Contract.C3)
    sim.request_approval(
        RequestApproval(
            intent_id="REQ-1", operation=Operation.SUBMIT_PURCHASE_ORDER, scope=_scope()
        ),
        caller=GW,
        vt=0,
    )
    basis = _read_inputs(sim)
    first = _committed(sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0))
    again = sim.receive_write(_order(basis=basis), caller=GW, vt=10)
    assert again == first.model_copy(update={"replay": True})


# The commit deadline (0005) --------------------------------------------------------------


def test_a_write_commits_up_to_its_deadline(make_sim: SimFactory) -> None:
    sim = make_sim()
    received = sim.receive_write(_order(), caller=GW, vt=0)
    assert isinstance(received, WriteReceived)
    assert _committed(sim.commit_received(received.received_seq, vt=10_000)).committed_vt == 10_000


def test_a_write_that_misses_its_deadline_aborts_and_never_commits(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim()
    received = sim.receive_write(_order(), caller=GW, vt=0)
    assert isinstance(received, WriteReceived)
    late = sim.commit_received(received.received_seq, vt=10_001)
    assert late == WriteAborted(received_seq=received.received_seq, deadline_vt=10_000)
    assert sim.commit_received(received.received_seq, vt=20_000) == late
    assert _count(sim_database, "effect") == 0
    assert _rows(
        sim_database,
        "SELECT w.state, w.outcome_vt, e.kind, e.actor FROM sim.write w"
        " JOIN sim.event e ON e.seq = w.outcome_seq",
    ) == [("aborted", 10_001, "write_aborted", "simulator")]
    assert len(_budget(sim_database)) == 1


def test_any_call_after_the_deadline_aborts_the_write_and_frees_its_key(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim()
    received = sim.receive_write(_order(), caller=GW, vt=0)
    assert isinstance(received, WriteReceived)
    sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=GW, vt=10_000)
    assert _rows(sim_database, "SELECT state FROM sim.write") == [("received",)]
    sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=GW, vt=10_001)
    assert _rows(sim_database, "SELECT state, outcome_vt FROM sim.write") == [("aborted", 10_001)]
    assert isinstance(sim.commit_received(received.received_seq, vt=10_002), WriteAborted)
    retry = _committed(sim.submit_purchase_order(_order(), caller=GW, vt=10_003))
    assert not retry.replay


def test_the_harness_aborts_overdue_writes_at_the_end_of_a_run(make_sim: SimFactory) -> None:
    sim = make_sim()
    first = sim.receive_write(_order(), caller=GW, vt=0)
    second = sim.receive_write(_order(idempotency_key="A-0002"), caller=GW, vt=5)
    assert isinstance(first, WriteReceived) and isinstance(second, WriteReceived)
    assert sim.control.abort_overdue(vt=10_003) == [first.received_seq]
    assert sim.control.abort_overdue(vt=10_006) == [second.received_seq]
    assert sim.control.abort_overdue(vt=20_000) == []


def test_a_waived_deadline_lets_a_write_commit_late(
    make_sim: SimFactory, sim_database: str
) -> None:
    """Fault F5 beyond the deadline: a contract violation that only a fault may cause."""
    sim = make_sim(Contract.C2)
    received = sim.receive_write(_order(), caller=GW, vt=0, waive_deadline=True)
    assert isinstance(received, WriteReceived)
    late_duplicate = sim.submit_purchase_order(_order(), caller=GW, vt=12_000)
    assert late_duplicate == WriteInProgress(idempotency_key="A-0001")
    assert _committed(sim.commit_received(received.received_seq, vt=15_000)).committed_vt == 15_000
    assert _rows(sim_database, "SELECT deadline_waived, state FROM sim.write ORDER BY 2") == [
        (True, "committed"),
        (False, "in_progress"),
    ]


def test_commit_received_needs_a_write_in_flight(make_sim: SimFactory) -> None:
    sim = make_sim()
    with pytest.raises(ControlError, match="no write"):
        sim.commit_received(99, vt=0)
    committed = _committed(sim.submit_purchase_order(_order(), caller=GW, vt=0))
    assert committed.effect_id == "PO-0001"
    with pytest.raises(ControlError, match="already ended as committed"):
        sim.commit_received(2, vt=0)


# Conditional writes (C3) -----------------------------------------------------------------


def _c3(make_sim: SimFactory, dev_data: Data, **authority: object) -> Simulator:
    dev_data["facts"]["budget/CC-OPS"]["limit_minor"] = 200_000
    dev_data["authority"].update(authority)
    return make_sim(Contract.C3)


def test_c3_commits_a_write_whose_checks_pass(make_sim: SimFactory, dev_data: Data) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim)
    basis = _read_inputs(sim)
    assert _committed(sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0))


def test_c3_allows_a_harmless_quote_change(make_sim: SimFactory, dev_data: Data) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim)
    basis = _read_inputs(sim)
    sim.control.change_fact("quote/S-1/W-100", {"lead_time_days": 9}, actor="F7", vt=0)
    assert _committed(sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0))


def _failed(answer: object) -> list[FailedCheck]:
    rejected = _rejected(answer)
    assert rejected.reason is WriteRejectReason.PRECONDITION_FAILED
    return rejected.failed_checks


def test_c3_rejects_an_unknown_approval(make_sim: SimFactory, dev_data: Data) -> None:
    sim = _c3(make_sim, dev_data)
    basis = _read_inputs(sim)
    answer = sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0)
    assert _failed(answer) == [FailedCheck(check=CheckName.APPROVAL_UNKNOWN)]


def test_c3_rejects_a_pending_approval(make_sim: SimFactory, dev_data: Data) -> None:
    sim = _c3(make_sim, dev_data, auto_approve_up_to_minor=0)
    assert _request(sim).status is ApprovalStatus.PENDING
    basis = _read_inputs(sim)
    answer = sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0)
    assert _failed(answer) == [FailedCheck(check=CheckName.APPROVAL_PENDING)]


def test_c3_rejects_a_denied_approval(make_sim: SimFactory, dev_data: Data) -> None:
    sim = _c3(make_sim, dev_data, auto_approve_up_to_minor=0)
    _request(sim)
    sim.control.decide("APR-0001", Decision.DENY, actor="approver", vt=0)
    basis = _read_inputs(sim)
    answer = sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0)
    assert _failed(answer) == [FailedCheck(check=CheckName.APPROVAL_DENIED)]


def test_c3_rejects_a_revoked_approval(make_sim: SimFactory, dev_data: Data) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim)
    sim.control.revoke("APR-0001", actor="fault:F6", vt=0)
    basis = _read_inputs(sim)
    answer = sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0)
    assert _failed(answer) == [FailedCheck(check=CheckName.APPROVAL_REVOKED)]


def test_c3_rejects_an_expired_approval_from_its_expiry_on(
    make_sim: SimFactory, dev_data: Data
) -> None:
    """Both approvals expire at TTL: the first commits 1 ms before, the second at TTL."""
    sim = _c3(make_sim, dev_data)
    _request(sim)
    _request(sim)
    basis = _read_inputs(sim, vt=TTL - 1)
    first = _order(basis=basis, idempotency_key="A-1")
    assert _committed(sim.submit_purchase_order(first, caller=GW, vt=TTL - 1))
    basis = _read_inputs(sim, vt=TTL)
    second = _order(basis=basis, approval_id="APR-0002", idempotency_key="A-2")
    answer = sim.submit_purchase_order(second, caller=GW, vt=TTL)
    assert _failed(answer) == [FailedCheck(check=CheckName.APPROVAL_EXPIRED)]


def test_c3_rejects_a_write_outside_its_approvals_scope(
    make_sim: SimFactory, dev_data: Data
) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim, scope={"max_quantity": 50, "max_amount_minor": 27_000})
    basis = _read_inputs(sim)
    answer = sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0)
    assert _failed(answer) == [
        FailedCheck(
            check=CheckName.APPROVAL_OUT_OF_SCOPE,
            mismatched_fields=["max_quantity", "max_amount_minor"],
        )
    ]


def test_c3_rejects_an_approval_that_backs_another_effect(
    make_sim: SimFactory, dev_data: Data
) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim)
    _committed(sim.submit_purchase_order(_order(basis=_read_inputs(sim)), caller=GW, vt=0))
    second = _order(basis=_read_inputs(sim), idempotency_key="A-0002")
    answer = sim.submit_purchase_order(second, caller=GW, vt=0)
    assert _failed(answer) == [FailedCheck(check=CheckName.APPROVAL_USED, effect_id="PO-0001")]


def test_c3_rejects_an_undeclared_input(make_sim: SimFactory, dev_data: Data) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim)
    _read_inputs(sim)
    answer = sim.submit_purchase_order(_order(basis={"budget/CC-OPS": 1}), caller=GW, vt=0)
    assert _failed(answer) == [
        FailedCheck(check=CheckName.UNDECLARED_INPUT, key="quote/S-1/W-100", current_version=1)
    ]


def test_c3_rejects_an_input_never_read(make_sim: SimFactory, dev_data: Data) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim)
    sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=GW, vt=0)
    answer = sim.submit_purchase_order(_order(), caller=GW, vt=0)
    assert _failed(answer) == [
        FailedCheck(
            check=CheckName.UNOBSERVED_INPUT,
            key="quote/S-1/W-100",
            cited_version=1,
            current_version=1,
        )
    ]


def test_c3_counts_only_reads_served_before_the_write_arrived(
    make_sim: SimFactory, dev_data: Data
) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim)
    received = sim.receive_write(_order(), caller=GW, vt=0)
    assert isinstance(received, WriteReceived)
    _read_inputs(sim, vt=10)
    checks = _failed(sim.commit_received(received.received_seq, vt=20))
    assert [(check.check, check.key) for check in checks] == [
        (CheckName.UNOBSERVED_INPUT, "quote/S-1/W-100"),
        (CheckName.UNOBSERVED_INPUT, "budget/CC-OPS"),
    ]


def test_c3_rejects_a_quote_that_changed_since_the_basis(
    make_sim: SimFactory, dev_data: Data
) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim)
    basis = _read_inputs(sim)
    sim.control.change_fact("quote/S-1/W-100", {"unit_price_minor": 567}, actor="F7", vt=0)
    answer = sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0)
    assert _failed(answer) == [
        FailedCheck(
            check=CheckName.STALE_INPUT,
            key="quote/S-1/W-100",
            cited_version=1,
            current_version=2,
            changed_fields=["unit_price_minor"],
        )
    ]


def test_c3_rejects_a_budget_that_no_longer_covers_the_order(
    make_sim: SimFactory, dev_data: Data
) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim)
    basis = _read_inputs(sim)
    sim.control.change_fact("budget/CC-OPS", {"committed_minor": 146_001}, actor="F7", vt=0)
    answer = sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0)
    assert _failed(answer) == [
        FailedCheck(
            check=CheckName.STALE_INPUT,
            key="budget/CC-OPS",
            cited_version=1,
            current_version=2,
            changed_fields=["committed_minor"],
        )
    ]


def test_c3_rejects_stock_that_no_longer_covers_the_reservation(
    make_sim: SimFactory, dev_data: Data
) -> None:
    sim = make_sim(Contract.C3, data=_with_stock(dev_data))
    approval = sim.request_approval(
        RequestApproval(
            intent_id="REQ-2",
            operation=Operation.RESERVE_INVENTORY,
            scope=ReservationScope(warehouse_id="WH-1", sku="W-100", max_quantity=30),
        ),
        caller=GW,
        vt=0,
    )
    assert isinstance(approval, ApprovalAnswer) and approval.status is ApprovalStatus.APPROVED
    sim.get_inventory(GetInventory(warehouse_id="WH-1", sku="W-100"), caller=GW, vt=0)
    sim.control.change_fact("inventory/WH-1/W-100", {"available": 29}, actor="F7", vt=0)
    answer = sim.reserve_inventory(_reservation(), caller=GW, vt=0)
    assert _failed(answer) == [
        FailedCheck(
            check=CheckName.STALE_INPUT,
            key="inventory/WH-1/W-100",
            cited_version=1,
            current_version=2,
            changed_fields=["available"],
        )
    ]
    sim.control.change_fact("inventory/WH-1/W-100", {"available": 30}, actor="F7", vt=0)
    assert _committed(sim.reserve_inventory(_reservation(), caller=GW, vt=0))


def test_c3_names_every_failed_check_authority_first(
    make_sim: SimFactory, dev_data: Data, sim_database: str
) -> None:
    sim = _c3(make_sim, dev_data)
    _request(sim, scope={"cost_center": "CC-RND"})
    basis = _read_inputs(sim)
    sim.control.revoke("APR-0001", actor="F6", vt=0)
    sim.control.change_fact("quote/S-1/W-100", {"status": "withdrawn"}, actor="F7", vt=0)
    checks = _failed(sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0))
    assert [check.check for check in checks] == [
        CheckName.APPROVAL_REVOKED,
        CheckName.APPROVAL_OUT_OF_SCOPE,
        CheckName.STALE_INPUT,
    ]
    rejection = _rows(sim_database, "SELECT state, rejection FROM sim.write")
    assert rejection[0][0] == "rejected"
    assert [item["check"] for item in rejection[0][1]["failed_checks"]] == [
        "approval_revoked",
        "approval_out_of_scope",
        "stale_input",
    ]
    assert _count(sim_database, "effect") == 0


def test_c3_looks_up_the_key_before_the_preconditions(make_sim: SimFactory, dev_data: Data) -> None:
    """A retry of a committed write gets the stored result, though its inputs went stale."""
    sim = _c3(make_sim, dev_data)
    _request(sim)
    basis = _read_inputs(sim)
    first = _committed(sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=0))
    sim.control.change_fact("quote/S-1/W-100", {"unit_price_minor": 600}, actor="F7", vt=1)
    sim.control.revoke("APR-0001", actor="F6", vt=1)
    retry = sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=2)
    assert retry == first.model_copy(update={"replay": True})


# Status (C1) -----------------------------------------------------------------------------


def _status(sim: Simulator, vt: int, intent_id: str = "REQ-1", **options: Any) -> StatusAnswer:
    answer = sim.get_order_status(GetOrderStatus(intent_id=intent_id), caller=GW, vt=vt, **options)
    assert isinstance(answer, StatusAnswer), answer
    return answer


def test_status_lists_the_effects_of_an_intent(make_sim: SimFactory, sim_database: str) -> None:
    sim = make_sim(Contract.C1)
    sim.submit_purchase_order(_order(), caller=GW, vt=10)
    sim.submit_purchase_order(_order(), caller=GW, vt=20)
    answer = _status(sim, 30)
    assert answer == StatusAnswer(
        intent_id="REQ-1",
        operation=Operation.SUBMIT_PURCHASE_ORDER,
        effects=[
            StatusEffect(effect_id="PO-0001", args=ORDER_ARGS),
            StatusEffect(effect_id="PO-0002", args=ORDER_ARGS),
        ],
        as_of_vt=30,
    )
    assert _status(sim, 30, intent_id="REQ-2").effects == []
    reservations = sim.get_reservation_status(
        GetReservationStatus(intent_id="REQ-1"), caller=GW, vt=30
    )
    assert isinstance(reservations, StatusAnswer) and reservations.effects == []
    assert _rows(sim_database, "SELECT kind, ref FROM sim.event WHERE kind = 'status'") == [
        ("status", "REQ-1"),
        ("status", "REQ-2"),
        ("status", "REQ-1"),
    ]


def test_a_status_answer_may_lag_by_at_most_l(make_sim: SimFactory, dev_data: Data) -> None:
    dev_data["params"]["status_lag_ms"] = 3_000
    sim = make_sim(Contract.C1)
    sim.submit_purchase_order(_order(), caller=GW, vt=1_000)
    lagging = _status(sim, 3_999, as_of_vt=999)
    assert (lagging.as_of_vt, lagging.effects) == (999, [])
    assert [e.effect_id for e in _status(sim, 4_000, as_of_vt=1_000).effects] == ["PO-0001"]
    with pytest.raises(ValueError, match="breaks the status promise"):
        _status(sim, 4_001, as_of_vt=1_000)
    stale = _status(sim, 9_000, as_of_vt=500, contract_violation=True)
    assert (stale.as_of_vt, stale.effects) == (500, [])


def test_a_status_answer_that_misreports_its_time_needs_a_fault(make_sim: SimFactory) -> None:
    sim = make_sim(Contract.C1)
    sim.submit_purchase_order(_order(), caller=GW, vt=1_000)
    with pytest.raises(ValueError, match="breaks the status promise"):
        _status(sim, 15_000, reflect_vt=500)
    answer = _status(sim, 15_000, reflect_vt=500, contract_violation=True)
    assert (answer.as_of_vt, answer.effects) == (15_000, [])
    with pytest.raises(ValueError, match="cannot reflect the future"):
        _status(sim, 15_000, as_of_vt=15_001)


@pytest.mark.parametrize("contract", [Contract.C0, Contract.C2, Contract.C3])
def test_status_needs_the_capability(
    make_sim: SimFactory, sim_database: str, contract: Contract
) -> None:
    sim = make_sim(contract)
    answer = sim.get_order_status(GetOrderStatus(intent_id="REQ-1"), caller=GW, vt=0)
    assert isinstance(answer, Refusal) and answer.reason is RefusalReason.UNSUPPORTED
    assert _count(sim_database, "event") == 1


# Approvals (0002) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "status", "decided_by"),
    [
        ({}, ApprovalStatus.APPROVED, "policy"),
        ({"max_amount_minor": 60_000}, ApprovalStatus.APPROVED, "policy"),
        ({"max_amount_minor": 60_001}, ApprovalStatus.PENDING, None),
        ({"supplier_id": "S-3"}, ApprovalStatus.DENIED, "policy"),
        ({"max_amount_minor": 100_001}, ApprovalStatus.DENIED, "policy"),
    ],
)
def test_policy_decides_each_request_on_arrival(
    make_sim: SimFactory,
    sim_database: str,
    scope: dict[str, object],
    status: ApprovalStatus,
    decided_by: str | None,
) -> None:
    sim = make_sim()
    answer = _request(sim, 50, scope=scope)
    assert (answer.approval_id, answer.status, answer.decided_by) == (
        "APR-0001",
        status,
        decided_by,
    )
    assert answer.requested_vt == 50
    expected_expiry = 50 + TTL if status is ApprovalStatus.APPROVED else None
    assert answer.expires_at == expected_expiry
    kinds = [
        row[0] for row in _rows(sim_database, "SELECT kind FROM sim.approval_event ORDER BY seq")
    ]
    assert kinds == ["requested"] + ([] if decided_by is None else [str(status)])
    queued = sim.control.pending_approvals()
    assert [item.approval_id for item in queued] == (["APR-0001"] if decided_by is None else [])


def test_policy_approves_every_reservation(make_sim: SimFactory, dev_data: Data) -> None:
    dev_data["authority"]["allowlist"] = ["S-1"]
    dev_data["authority"]["auto_approve_up_to_minor"] = 0
    sim = make_sim()
    answer = sim.request_approval(
        RequestApproval(
            intent_id="REQ-2",
            operation=Operation.RESERVE_INVENTORY,
            scope=ReservationScope(warehouse_id="WH-1", sku="W-100", max_quantity=9_999),
        ),
        caller=GW,
        vt=0,
    )
    assert isinstance(answer, ApprovalAnswer)
    assert (answer.status, answer.decided_by) == (ApprovalStatus.APPROVED, "policy")


def test_a_repeated_client_request_key_returns_the_original(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim()
    first = _request(sim, 0, key="A-0001")
    again = _request(sim, 10, key="A-0001", scope={"max_quantity": 1, "max_amount_minor": 1})
    assert again == first.model_copy(update={"repeated": True})
    assert _count(sim_database, "approval") == 1
    assert _rows(sim_database, "SELECT kind, ref FROM sim.event ORDER BY seq DESC LIMIT 1") == [
        ("approval_repeated", "APR-0001")
    ]
    assert _request(sim, 20).approval_id == "APR-0002"
    assert _request(sim, 30).approval_id == "APR-0003"


def test_the_scripted_approver_decides_queued_requests(
    make_sim: SimFactory, sim_database: str
) -> None:
    sim = make_sim()
    _request(sim, 0, scope={"max_amount_minor": 70_000})
    _request(sim, 5, scope={"max_amount_minor": 80_000})
    assert [item.approval_id for item in sim.control.pending_approvals()] == [
        "APR-0001",
        "APR-0002",
    ]
    approved = sim.control.decide("APR-0001", Decision.APPROVE, actor="approver", vt=2_000)
    assert (approved.status, approved.decided_vt, approved.decided_by, approved.expires_at) == (
        ApprovalStatus.APPROVED,
        2_000,
        "approver",
        2_000 + TTL,
    )
    denied = sim.control.decide("APR-0002", Decision.DENY, actor="approver", vt=2_005)
    assert (denied.status, denied.expires_at) == (ApprovalStatus.DENIED, None)
    assert sim.control.pending_approvals() == []
    assert _rows(
        sim_database, "SELECT approval_id, kind, actor, vt FROM sim.approval_event ORDER BY seq"
    ) == [
        ("APR-0001", "requested", GW, 0),
        ("APR-0002", "requested", GW, 5),
        ("APR-0001", "approved", "approver", 2_000),
        ("APR-0002", "denied", "approver", 2_005),
    ]


def test_only_a_pending_request_can_be_decided(make_sim: SimFactory) -> None:
    sim = make_sim()
    _request(sim)
    with pytest.raises(ControlError, match="not pending"):
        sim.control.decide("APR-0001", Decision.DENY, actor="approver", vt=0)
    with pytest.raises(ControlError, match="no approval"):
        sim.control.decide("APR-0404", Decision.APPROVE, actor="approver", vt=0)


def test_an_approval_expires_its_ttl_after_its_decision(make_sim: SimFactory) -> None:
    sim = make_sim()
    _request(sim, 100)
    request = GetApprovalStatus(approval_id="APR-0001", wait_ms=60_000)
    before = sim.get_approval_status(request, caller=GW, vt=99 + TTL)
    after = sim.get_approval_status(request, caller=GW, vt=100 + TTL)
    assert isinstance(before, ApprovalAnswer) and isinstance(after, ApprovalAnswer)
    assert (before.status, after.status) == (ApprovalStatus.APPROVED, ApprovalStatus.EXPIRED)


def test_revocation_is_permanent(make_sim: SimFactory, sim_database: str) -> None:
    sim = make_sim()
    _request(sim)
    revoked = sim.control.revoke("APR-0001", actor="fault:F6", vt=10)
    assert (revoked.status, revoked.revoked_vt) == (ApprovalStatus.REVOKED, 10)
    later = sim.get_approval_status(
        GetApprovalStatus(approval_id="APR-0001"), caller=GW, vt=2 * TTL
    )
    assert isinstance(later, ApprovalAnswer) and later.status is ApprovalStatus.REVOKED
    with pytest.raises(ControlError, match="only approved ones"):
        sim.control.revoke("APR-0001", actor="fault:F6", vt=2 * TTL)
    assert _rows(sim_database, "SELECT kind, actor FROM sim.approval_event ORDER BY seq") == [
        ("requested", GW),
        ("approved", "policy"),
        ("revoked", "fault:F6"),
    ]


@pytest.mark.parametrize("scope", [{"max_amount_minor": 70_000}, {"supplier_id": "S-3"}])
def test_only_an_approved_approval_can_be_revoked(
    make_sim: SimFactory, scope: dict[str, object]
) -> None:
    sim = make_sim()
    _request(sim, scope=scope)
    with pytest.raises(ControlError, match="only approved ones"):
        sim.control.revoke("APR-0001", actor="fault:F6", vt=0)


def test_an_unknown_approval_is_not_found(make_sim: SimFactory, sim_database: str) -> None:
    sim = make_sim()
    answer = sim.get_approval_status(GetApprovalStatus(approval_id="APR-0404"), caller=GW, vt=0)
    assert isinstance(answer, Refusal) and answer.reason is RefusalReason.NOT_FOUND
    assert _count(sim_database, "event") == 1


def test_an_approval_records_the_first_effect_that_cites_it(
    make_sim: SimFactory, sim_database: str
) -> None:
    """At C0 to C2 the destination records the citation but does not check it (rule 11)."""
    sim = make_sim(Contract.C2)
    _request(sim)
    sim.submit_purchase_order(_order(), caller=GW, vt=0)
    sim.submit_purchase_order(_order(idempotency_key="A-0002"), caller=GW, vt=0)
    assert _rows(sim_database, "SELECT approval_id, effect_id FROM sim.approval") == [
        ("APR-0001", "PO-0001")
    ]
    assert _rows(sim_database, "SELECT approval_id FROM sim.effect ORDER BY effect_id") == [
        ("APR-0001",),
        ("APR-0001",),
    ]


# One sequence, deterministic ground truth -------------------------------------------------


def _workload(sim: Simulator) -> None:
    """A run that touches every kind of event."""
    sim.list_suppliers(ListSuppliers(), caller=GW, vt=0)
    _request(sim, 10, key="A-0001")
    _request(sim, 20, key="A-0001")
    basis = _read_inputs(sim, 30)
    received = sim.receive_write(_order(basis=basis), caller=GW, vt=40)
    assert isinstance(received, WriteReceived)
    sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=50)
    sim.commit_received(received.received_seq, vt=60)
    sim.submit_purchase_order(_order(basis=basis), caller=GW, vt=70)
    sim.get_approval_status(GetApprovalStatus(approval_id="APR-0001"), caller=GW, vt=80)
    _request(sim, 90, scope={"max_amount_minor": 70_000})
    sim.control.decide("APR-0002", Decision.APPROVE, actor="approver", vt=2_090)
    sim.control.revoke("APR-0002", actor="fault:F6", vt=2_100)
    sim.control.change_fact("quote/S-1/W-100", {"unit_price_minor": 567}, actor="F7", vt=2_200)
    sim.submit_purchase_order(
        _order(basis=basis, approval_id="APR-0002", idempotency_key="A-0002"),
        caller=GW,
        vt=2_300,
    )
    sim.receive_write(_order(idempotency_key="A-0003"), caller=GW, vt=2_400)
    sim.control.abort_overdue(vt=20_000)


SIM_TABLES: dict[LiteralString, LiteralString] = {
    "run": "singleton",
    "event": "seq",
    "fact_version": "key, version",
    "read_log": "seq",
    "write": "received_seq",
    "effect": "effect_id",
    "approval": "approval_id",
    "approval_event": "seq",
}


def _dump(dsn: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every sim table's rows in key order, without wall-clock columns."""
    dump = {}
    with psycopg.connect(dsn) as conn:
        for table, order in SIM_TABLES.items():
            rows = conn.execute(
                "SELECT to_jsonb(t) - 'wall_at' FROM sim." + table + " t ORDER BY " + order
            ).fetchall()
            dump[table] = rows
    return dump


def test_one_gap_free_sequence_orders_every_event(make_sim: SimFactory, sim_database: str) -> None:
    sim = make_sim(Contract.C3)
    _workload(sim)
    events = _rows(sim_database, "SELECT seq, vt FROM sim.event ORDER BY seq")
    assert [seq for seq, _ in events] == list(range(1, len(events) + 1))
    assert [vt for _, vt in events] == sorted(vt for _, vt in events)
    assert _rows(sim_database, "SELECT last_seq, last_vt FROM sim.run") == [(len(events), 20_000)]
    kinds = {row[0] for row in _rows(sim_database, "SELECT DISTINCT kind FROM sim.event")}
    assert kinds == {
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
        "approval_revoked",
        "approval_status",
    }


def test_the_same_calls_give_the_same_ground_truth(
    make_sim: SimFactory,
    sim_database: str,
    admin_dsn: str,
    sim_template: str,
    db_prefix: str,
    dev_data: Data,
) -> None:
    """Determinism (0009): the same calls on the same scenario give byte-identical tables."""
    _workload(make_sim(Contract.C3))
    first = _dump(sim_database)
    name = f"{db_prefix}_sim_twin"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(sim_template)
            )
        )
    twin = make_conninfo(admin_dsn, dbname=name)
    try:
        scenario = scenario_from_data(dev_data)
        config = SimConfig.preset(
            Contract.C3, credentials=[GW], params=scenario.params.contract_params()
        )
        with Simulator.setup(make_conninfo(twin, user="ff_sim"), scenario, config) as sim:
            _workload(sim)
        second = _dump(twin)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
    assert first == second
    assert len(first["event"]) == 22
