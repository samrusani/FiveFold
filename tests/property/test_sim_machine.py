"""Random runs of the simulator keep its ground truth whole (0002, 0004, 0005, 0009).

A Hypothesis machine calls the simulator as a gateway would: reads, approval requests,
decisions, revocations, quote changes, writes that commit at once or later (the split
path), jumps in virtual time past deadlines and expiries, and status lookups. After each
example, `check_ground_truth` re-derives what must hold from the sim tables alone:

- one gap-free sequence, with virtual time that never goes back;
- every effect commits its own write once, by its deadline, with its consequence, and the
  budget is the initial amount plus every order's amount;
- writes in flight are within their deadline; aborted writes missed it;
- a key the destination honours commits at most once;
- each approval's status follows its events;
- at C3, every effect passes properties 2 and 3 as the evaluator judges them from history
  (0004), not as the simulator judged them at commit.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, ClassVar, LiteralString, cast

import psycopg
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, precondition, rule
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow

from fivefold.domain.api import (
    ApprovalAnswer,
    FactRead,
    GetBudget,
    GetOrderStatus,
    GetQuote,
    PurchaseOrderScope,
    Refusal,
    RefusalReason,
    RequestApproval,
    StatusAnswer,
    SubmitPurchaseOrder,
    WriteAborted,
    WriteCommitted,
    WriteInProgress,
    WriteReceived,
    WriteRejected,
    WriteRejectReason,
)
from fivefold.domain.authority import Decision, scope_mismatches
from fivefold.domain.contracts import Capability, Contract
from fivefold.domain.rules import FactValue, Operation, check_basis, order_amount_minor
from fivefold.domain.scenario import scenario_from_data
from fivefold.sim import ControlError, SimConfig, Simulator

Connection = psycopg.Connection[TupleRow]
GW = "gateway"
TTL = 30_000
DEADLINE = 10_000
SUPPLIERS = ("S-1", "S-2")
KEYS = (None, "K-1", "K-2", "K-3")
SIM_TABLES: list[LiteralString] = ["run", "event", "fact_version", "read_log", "write"]
SIM_TABLES += ["effect", "approval", "approval_event"]


def _scenario(dev_data: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(dev_data)
    data["facts"]["budget/CC-OPS"]["limit_minor"] = 200_000
    data["authority"]["approval_ttl_ms"] = TTL
    return data


@dataclass(frozen=True)
class InFlight:
    """A write received through the split path, waiting for its commit."""

    order: SubmitPurchaseOrder
    deadline_vt: int
    waived: bool


@dataclass(frozen=True)
class MachineDatabase:
    """One run database for the machine, emptied before each example."""

    admin: str
    sim: str
    data: dict[str, Any]

    def reset(self) -> None:
        """Empty every sim table. Needs the admin role, with the triggers off meanwhile."""
        with psycopg.connect(self.admin, autocommit=True) as conn, conn.transaction():
            for table in SIM_TABLES:
                conn.execute("ALTER TABLE sim." + table + " DISABLE TRIGGER USER")
            conn.execute("TRUNCATE " + ", ".join("sim." + table for table in SIM_TABLES))
            for table in SIM_TABLES:
                conn.execute("ALTER TABLE sim." + table + " ENABLE TRIGGER USER")


class SimMachine(RuleBasedStateMachine):
    """Random calls to one run's simulator, as a gateway would make them."""

    contract: ClassVar[Contract]
    database: ClassVar[MachineDatabase | None] = None

    def __init__(self) -> None:
        super().__init__()
        database = SimMachine.database
        if database is None:
            raise RuntimeError("the simulator machine needs the sim_machine fixture")
        database.reset()
        self.db = database
        scenario = scenario_from_data(database.data)
        config = SimConfig.preset(self.contract, credentials=[GW])
        self.sim = Simulator.setup(database.sim, scenario, config)
        self.vt = 0
        self.served: dict[str, set[int]] = {}
        self.approvals: list[str] = []
        self.scopes: dict[str, PurchaseOrderScope] = {}
        self.in_flight: dict[int, InFlight] = {}
        """Writes received through the split path, by received seq."""
        self.used: set[str] = set()
        """Approvals the machine filed that back an effect."""
        self.keys = 0

    def teardown(self) -> None:
        try:
            with psycopg.connect(self.db.admin) as conn:
                check_ground_truth(conn)
        finally:
            self.sim.close()

    @initialize()
    def start(self) -> None:
        """Read every input and file three requests: two approved by policy, one queued."""
        for supplier in SUPPLIERS:
            self.read_quote(supplier)
        self.read_budget()
        self.request_approval("S-1", 54_000, None)
        self.request_approval("S-2", 57_500, None)
        self.request_approval("S-1", 70_000, None)

    @rule(dt=st.sampled_from([0, 1, 1_000, DEADLINE - 1, DEADLINE + 1, TTL]))
    def advance(self, dt: int) -> None:
        self.vt += dt

    @precondition(lambda self: bool(self.in_flight))
    @rule(data=st.data(), past=st.booleans())
    def wait_out_a_deadline(self, data: st.DataObject, past: bool) -> None:
        """Jump to the deadline of a write in flight, or just past it, then make any call."""
        flight = data.draw(st.sampled_from([self.in_flight[seq] for seq in sorted(self.in_flight)]))
        self.vt = max(self.vt, flight.deadline_vt + (1 if past else 0))
        self.read_budget()

    @rule(supplier=st.sampled_from(SUPPLIERS))
    def read_quote(self, supplier: str) -> None:
        answer = self.sim.get_quote(
            GetQuote(supplier_id=supplier, sku="W-100"), caller=GW, vt=self.vt
        )
        assert isinstance(answer, FactRead)
        self.served.setdefault(answer.key, set()).add(answer.version)

    @rule()
    def read_budget(self) -> None:
        answer = self.sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=GW, vt=self.vt)
        assert isinstance(answer, FactRead)
        self.served.setdefault(answer.key, set()).add(answer.version)

    @rule(
        supplier=st.sampled_from(SUPPLIERS),
        amount=st.sampled_from([54_000, 57_500, 70_000, 100_001]),
        key=st.sampled_from([None, "R-1", "R-2"]),
    )
    def request_approval(self, supplier: str, amount: int, key: str | None) -> None:
        scope = PurchaseOrderScope(
            supplier_id=supplier,
            sku="W-100",
            max_quantity=100,
            max_amount_minor=amount,
            currency="USD",
            cost_center="CC-OPS",
        )
        answer = self.sim.request_approval(
            RequestApproval(
                intent_id="REQ-1",
                operation=Operation.SUBMIT_PURCHASE_ORDER,
                scope=scope,
                client_request_key=key,
            ),
            caller=GW,
            vt=self.vt,
        )
        assert isinstance(answer, ApprovalAnswer)
        if answer.approval_id not in self.approvals:
            self.approvals.append(answer.approval_id)
            assert isinstance(answer.scope, PurchaseOrderScope)
            self.scopes[answer.approval_id] = answer.scope

    @rule(data=st.data(), approve=st.booleans())
    def decide(self, data: st.DataObject, approve: bool) -> None:
        pending = self.sim.control.pending_approvals()
        if pending:
            approval = data.draw(st.sampled_from(pending))
            decision = Decision.APPROVE if approve else Decision.DENY
            self.sim.control.decide(approval.approval_id, decision, actor="approver", vt=self.vt)

    @rule(data=st.data())
    def revoke(self, data: st.DataObject) -> None:
        if self.approvals:
            approval_id = data.draw(st.sampled_from(self.approvals))
            with suppress(ControlError):
                self.sim.control.revoke(approval_id, actor="F6", vt=self.vt)

    @rule(
        supplier=st.sampled_from(SUPPLIERS),
        field=st.sampled_from(["unit_price_minor", "lead_time_days"]),
        value=st.sampled_from([3, 540, 567, 575]),
    )
    def change_quote(self, supplier: str, field: str, value: int) -> None:
        with suppress(ControlError):
            self.sim.control.change_fact(
                f"quote/{supplier}/W-100", {field: value}, actor="F7", vt=self.vt
            )

    @rule(data=st.data(), split=st.booleans(), waive=st.booleans(), fresh=st.booleans())
    def write_as_planned(self, data: st.DataObject, split: bool, waive: bool, fresh: bool) -> None:
        """A write a careful client would send: within an approval's scope, citing the
        latest versions it read, with a new key or one it used before."""
        approval_id = data.draw(st.sampled_from(self.approvals))
        if fresh or not self.keys:
            self.keys += 1
        number = self.keys if fresh else data.draw(st.integers(1, self.keys))
        self.send(self.planned(approval_id, f"P-{number}"), split=split, waive=waive)

    @precondition(lambda self: bool(self.used))
    @rule(data=st.data())
    def reuse_an_approval(self, data: st.DataObject) -> None:
        """Read every input again, then cite an approval that already backs an effect."""
        approval_id = data.draw(st.sampled_from(sorted(self.used)))
        self.read_quote(self.scopes[approval_id].supplier_id)
        self.read_budget()
        self.keys += 1
        self.send(self.planned(approval_id, f"P-{self.keys}"), split=False, waive=False)

    @rule(data=st.data())
    def stray_from_a_scope(self, data: st.DataObject) -> None:
        """Read every input again, then order from the supplier the approval does not name.
        At C3 only the scope check stands between this write and a commit."""
        approval_id = data.draw(st.sampled_from(self.approvals))
        other = next(s for s in SUPPLIERS if s != self.scopes[approval_id].supplier_id)
        self.read_quote(other)
        self.read_budget()
        self.keys += 1
        self.send(
            self.planned(approval_id, f"P-{self.keys}", supplier=other), split=False, waive=False
        )

    @rule(data=st.data())
    def cite_inputs_read_too_late(self, data: st.DataObject) -> None:
        """Send a write citing the current versions, read them only after it arrived,
        then commit it. Reads served after a write arrived do not count (0004)."""
        approval_id = data.draw(st.sampled_from(self.approvals))
        supplier = self.scopes[approval_id].supplier_id
        with psycopg.connect(self.db.admin) as conn:
            rows = conn.execute(
                "SELECT key, max(version) FROM sim.fact_version WHERE key = ANY(%s) GROUP BY key",
                ([f"quote/{supplier}/W-100", "budget/CC-OPS"],),
            ).fetchall()
        self.keys += 1
        order = self.planned(approval_id, f"P-{self.keys}", {str(k): int(v) for k, v in rows})
        answer = self.sim.receive_write(order, caller=GW, vt=self.vt)
        if not isinstance(answer, WriteReceived):
            self.check_answer(order, answer)
            return
        self.read_quote(supplier)
        self.read_budget()
        self.in_flight[answer.received_seq] = InFlight(order, answer.deadline_vt, False)
        self.finish(answer.received_seq)

    def planned(
        self,
        approval_id: str,
        key: str,
        basis: dict[str, int] | None = None,
        supplier: str | None = None,
    ) -> SubmitPurchaseOrder:
        """An order within the approval's scope, unless another supplier is named, citing
        the latest versions read."""
        supplier = self.scopes[approval_id].supplier_id if supplier is None else supplier
        if basis is None:
            basis = {
                fact: max(self.served[fact])
                for fact in (f"quote/{supplier}/W-100", "budget/CC-OPS")
                if fact in self.served
            }
        return SubmitPurchaseOrder(
            intent_id="REQ-1",
            approval_id=approval_id,
            basis=basis,
            idempotency_key=key,
            supplier_id=supplier,
            sku="W-100",
            quantity=100,
            unit_price_minor=540,
            currency="USD",
            cost_center="CC-OPS",
        )

    @rule(data=st.data(), split=st.booleans(), waive=st.booleans())
    def write(self, data: st.DataObject, split: bool, waive: bool) -> None:
        """Any write at all: undeclared, unobserved or stale inputs, unknown approvals."""
        supplier = data.draw(st.sampled_from(SUPPLIERS))
        quote_key, budget_key = f"quote/{supplier}/W-100", "budget/CC-OPS"
        basis: dict[str, int] = {}
        for key in (quote_key, budget_key):
            choices = [*sorted(self.served.get(key, set())), 0, 1, 2]
            version = data.draw(st.sampled_from(choices))
            if version:
                basis[key] = version
        order = SubmitPurchaseOrder(
            intent_id="REQ-1",
            approval_id=data.draw(st.sampled_from([*self.approvals, "APR-0999"])),
            basis=basis,
            idempotency_key=data.draw(st.sampled_from(KEYS)),
            supplier_id=supplier,
            sku="W-100",
            quantity=100,
            unit_price_minor=data.draw(st.sampled_from([540, 567, 575])),
            currency="USD",
            cost_center="CC-OPS",
        )
        self.send(order, split=split, waive=waive)

    def send(self, order: SubmitPurchaseOrder, *, split: bool, waive: bool) -> None:
        if split:
            answer = self.sim.receive_write(order, caller=GW, vt=self.vt, waive_deadline=waive)
            if isinstance(answer, WriteReceived):
                self.in_flight[answer.received_seq] = InFlight(order, answer.deadline_vt, waive)
                return
        else:
            answer = self.sim.submit_purchase_order(order, caller=GW, vt=self.vt)
        self.check_answer(order, answer)

    @precondition(lambda self: bool(self.in_flight))
    @rule(data=st.data())
    def commit(self, data: st.DataObject) -> None:
        self.finish(data.draw(st.sampled_from(sorted(self.in_flight))))

    def finish(self, received_seq: int) -> None:
        """Commit a write in flight. Only a write whose deadline holds can abort."""
        flight = self.in_flight.pop(received_seq)
        answer = self.sim.commit_received(received_seq, vt=self.vt)
        if isinstance(answer, WriteAborted):
            assert not flight.waived, "a write whose deadline a fault waived aborted"
            assert self.vt > flight.deadline_vt
            return
        assert not isinstance(answer, WriteInProgress)
        assert not (isinstance(answer, WriteCommitted) and answer.replay)
        self.check_answer(flight.order, answer)

    @rule()
    def status(self) -> None:
        answer = self.sim.get_order_status(GetOrderStatus(intent_id="REQ-1"), caller=GW, vt=self.vt)
        if self.contract.has(Capability.STATUS):
            assert isinstance(answer, StatusAnswer) and answer.as_of_vt == self.vt
            with psycopg.connect(self.db.admin) as conn:
                rows = conn.execute("SELECT effect_id FROM sim.effect ORDER BY commit_seq")
                assert [effect.effect_id for effect in answer.effects] == [
                    row[0] for row in rows.fetchall()
                ]
        else:
            assert isinstance(answer, Refusal) and answer.reason is RefusalReason.UNSUPPORTED

    def check_answer(self, order: SubmitPurchaseOrder, answer: object) -> None:
        """Answers that follow from the contract alone, whatever the inputs."""
        keys = self.contract.has(Capability.IDEMPOTENCY_KEY) and order.idempotency_key is not None
        if isinstance(answer, WriteInProgress) or (
            isinstance(answer, WriteCommitted) and answer.replay
        ):
            assert keys
        elif isinstance(answer, WriteRejected):
            assert answer.reason is WriteRejectReason.PRECONDITION_FAILED
            assert self.contract.has(Capability.CONDITIONAL_WRITE)
        else:
            assert isinstance(answer, WriteCommitted)
            if order.approval_id in self.scopes:
                self.used.add(order.approval_id)


# The checker ---------------------------------------------------------------------------


def _rows(conn: Connection, query: LiteralString) -> list[tuple[Any, ...]]:
    return conn.execute(query).fetchall()


class _History:
    """The facts as the evaluator sees them for one effect (0004, property 3)."""

    def __init__(
        self,
        versions: Mapping[str, list[tuple[int, int, FactValue]]],
        reads: set[tuple[str, int, int]],
        received: int,
        commit: int,
    ) -> None:
        self.versions = versions
        self.reads = reads
        self.received = received
        self.commit = commit

    def value_at(self, key: str, version: int) -> FactValue | None:
        return next(
            (value for number, _, value in self.versions.get(key, []) if number == version), None
        )

    def current(self, key: str) -> tuple[int, FactValue] | None:
        before = [
            (number, value)
            for number, seq, value in self.versions.get(key, [])
            if seq < self.commit
        ]
        return before[-1] if before else None

    def was_served(self, key: str, version: int) -> bool:
        return any(k == key and v == version and seq < self.received for k, v, seq in self.reads)


def check_ground_truth(conn: Connection) -> None:
    """What must hold in any run's sim tables, re-derived from the rows alone."""
    run = _rows(conn, "SELECT capabilities, commit_deadline_ms, last_seq, last_vt FROM sim.run")[0]
    capabilities = set(run[0])
    events = _rows(conn, "SELECT seq, vt FROM sim.event ORDER BY seq")
    assert (
        [seq for seq, _ in events] == list(range(1, len(events) + 1)) == list(range(1, run[2] + 1))
    )
    assert [vt for _, vt in events] == sorted(vt for _, vt in events)
    last_vt = events[-1][1]
    assert run[3] == last_vt

    writes = {
        row[0]: row
        for row in _rows(
            conn,
            "SELECT received_seq, received_vt, state, deadline_vt, deadline_waived, outcome_vt,"
            " effect_id, key_honoured, idempotency_key FROM sim.write",
        )
    }
    effects = _rows(
        conn,
        "SELECT effect_id, args, approval_id, basis, idempotency_key, received_seq, commit_seq,"
        " committed_vt, intent_id FROM sim.effect ORDER BY commit_seq",
    )
    for (
        _received,
        received_vt,
        state,
        deadline,
        waived,
        outcome_vt,
        _effect_id,
        honoured,
        key,
    ) in writes.values():
        assert deadline == received_vt + run[1]
        if state == "received" and not waived:
            assert deadline >= last_vt, "a write in flight past its deadline was not aborted"
        if state == "aborted":
            assert outcome_vt > deadline and not waived
        if state in ("in_progress", "replayed"):
            assert honoured and key is not None
    for effect_id, _, _, _, key, received, _, committed_vt, _ in effects:
        write = writes[received]
        assert (write[2], write[6]) == ("committed", effect_id)
        assert committed_vt <= write[3] or write[4], "an effect committed after its deadline"
        if "idempotency_key" not in capabilities:
            assert key is None
    keys = [effect[4] for effect in effects if effect[4] is not None]
    assert len(keys) == len(set(keys)), "a key committed twice"

    budget = _rows(
        conn,
        "SELECT version, (value ->> 'committed_minor')::bigint, cause, cause_effect_id FROM"
        " sim.fact_version WHERE key = 'budget/CC-OPS' ORDER BY version",
    )
    amounts = {effect[0]: order_amount_minor(effect[1]) for effect in effects}
    assert [row[3] for row in budget[1:]] == [effect[0] for effect in effects]
    assert budget[-1][1] == budget[0][1] + sum(amounts.values())

    approvals = _rows(conn, "SELECT approval_id, status, expires_at, decided_vt FROM sim.approval")
    approval_events: dict[str, list[tuple[str, int]]] = {}
    for approval_id, kind, seq in _rows(
        conn, "SELECT approval_id, kind, seq FROM sim.approval_event ORDER BY seq"
    ):
        approval_events.setdefault(approval_id, []).append((kind, seq))
    expected_kinds = {
        "pending": ["requested"],
        "approved": ["requested", "approved"],
        "denied": ["requested", "denied"],
        "revoked": ["requested", "approved", "revoked"],
    }
    for approval_id, status, expires_at, decided_vt in approvals:
        assert [kind for kind, _ in approval_events[approval_id]] == expected_kinds[status]
        if status in ("approved", "revoked"):
            assert expires_at == decided_vt + TTL

    if "conditional_write" in capabilities:
        _check_c3_effects(conn, effects, approval_events)


def _check_c3_effects(
    conn: Connection,
    effects: list[tuple[Any, ...]],
    approval_events: Mapping[str, list[tuple[str, int]]],
) -> None:
    """At C3 every effect passes properties 2 and 3, judged from history (0004)."""
    approvals = {
        row[0]: row
        for row in _rows(
            conn, "SELECT approval_id, intent_id, operation, scope, expires_at FROM sim.approval"
        )
    }
    versions: dict[str, list[tuple[int, int, FactValue]]] = {}
    for key, version, seq, value in _rows(
        conn,
        "SELECT key, version, valid_from_seq, value FROM sim.fact_version ORDER BY key, version",
    ):
        versions.setdefault(key, []).append((version, seq, value))
    reads = {
        (key, version, seq)
        for seq, key, version in _rows(conn, "SELECT seq, key, version FROM sim.read_log")
    }
    cited: set[str] = set()
    for (
        effect_id,
        args,
        approval_id,
        basis,
        _,
        received,
        commit,
        committed_vt,
        intent_id,
    ) in effects:
        approval = approvals.get(approval_id)
        assert approval is not None, f"{effect_id} cites no approval"
        history = approval_events[approval_id]
        assert any(kind == "approved" and seq < commit for kind, seq in history), effect_id
        assert not any(kind == "revoked" and seq < commit for kind, seq in history), effect_id
        assert committed_vt < approval[4], f"{effect_id} committed after its approval expired"
        assert (
            scope_mismatches(
                approval_intent_id=approval[1],
                approval_operation=Operation(approval[2]),
                scope=approval[3],
                intent_id=intent_id,
                operation=Operation.SUBMIT_PURCHASE_ORDER,
                args=args,
            )
            == ()
        ), effect_id
        assert approval_id not in cited, f"{effect_id} cites a used approval"
        cited.add(approval_id)
        checks = check_basis(
            Operation.SUBMIT_PURCHASE_ORDER,
            args,
            cast(dict[str, int], basis),
            _History(versions, reads, received, commit),
        )
        assert all(check.passed for check in checks), (effect_id, checks)


@pytest.fixture
def sim_machine(
    admin_dsn: str, sim_template: str, db_prefix: str, dev_data: dict[str, Any]
) -> Iterator[None]:
    """Point the machines at a run database cloned from the template while one runs."""
    name = f"{db_prefix}_sim_machine"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(sim_template)
            )
        )
    admin = make_conninfo(admin_dsn, dbname=name)
    SimMachine.database = MachineDatabase(
        admin=admin, sim=make_conninfo(admin, user="ff_sim"), data=_scenario(dev_data)
    )
    yield
    SimMachine.database = None
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


# Every step reaches the database, so the machines run fewer examples than in-memory ones;
# the nightly profile scales them up with everything else.
SIM_MACHINE_SETTINGS = settings(
    max_examples=max(20, settings().max_examples // 10), stateful_step_count=40
)


@SIM_MACHINE_SETTINGS
class SimC0(SimMachine):
    contract = Contract.C0


@SIM_MACHINE_SETTINGS
class SimC1(SimMachine):
    contract = Contract.C1


@SIM_MACHINE_SETTINGS
class SimC2(SimMachine):
    contract = Contract.C2


@SIM_MACHINE_SETTINGS
class SimC3(SimMachine):
    contract = Contract.C3


def _on_postgres(case: type[Any]) -> type[Any]:
    return cast(type[Any], pytest.mark.pg(pytest.mark.usefixtures("sim_machine")(case)))


TestSimC0 = _on_postgres(SimC0.TestCase)
TestSimC1 = _on_postgres(SimC1.TestCase)
TestSimC2 = _on_postgres(SimC2.TestCase)
TestSimC3 = _on_postgres(SimC3.TestCase)
