"""The procurement simulator: reads, approvals, writes and status (0002, 0004, 0005).

`Simulator` serves one run. It connects as `ff_sim`, the only role that writes the `sim`
schema (0008). Every call takes the virtual time `vt` and, for API calls, the caller's
identity. It never reads a clock or a random source (0009). Each call is one database
transaction that first locks the run's row, so the simulator handles one request at a
time and one gap-free sequence orders its events (0004). Nothing here waits: a caller
that wants to wait, such as B0's `get_approval_status(wait_ms)`, waits in the harness on
virtual-time timers.

Every call starts by aborting the writes in flight whose commit deadline has passed
(0005). The harness's split path, `receive_write` then `commit_received`, lets a fault
delay a commit (F5). `Control` is the harness's interface for scripted humans and fault
plans: decide, revoke, change a fact.

Use a simulator from one thread at a time, as the deterministic scheduler does (0009).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import TracebackType

import psycopg
from psycopg.types.json import Jsonb
from pydantic import JsonValue, ValidationError

from fivefold.domain.api import (
    SCOPE_MODELS,
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
    Refusal,
    RefusalReason,
    RequestApproval,
    ReserveInventory,
    StatusAnswer,
    StatusEffect,
    SubmitPurchaseOrder,
    Write,
    WriteAborted,
    WriteAnswer,
    WriteCommitted,
    WriteInProgress,
    WriteReceived,
    WriteRejected,
    WriteRejectReason,
)
from fivefold.domain.authority import (
    POLICY_ACTOR,
    ApprovalStatus,
    Decision,
    PolicyDecision,
    scope_mismatches,
)
from fivefold.domain.contracts import Capability, ContractParams
from fivefold.domain.rules import (
    SUPPLIERS_KEY,
    FactKind,
    Operation,
    budget_key,
    check_basis,
    consequence,
    consequence_key,
    fact_kind,
    inventory_key,
    quote_key,
)
from fivefold.domain.scenario import FACT_MODELS, Authority, Scenario
from fivefold.sim.config import SimConfig
from fivefold.sim.ledger import (
    ApprovalRow,
    Connection,
    EffectRow,
    History,
    JsonObject,
    Ledger,
    NewWrite,
    WriteRow,
    json_object,
)

SIM_ROLE = "ff_sim"
"""The database role the simulator connects as (0008)."""

SIM_ACTOR = "simulator"
"""The actor of events the simulator causes itself: setup and deadline aborts."""

_STATUS_CHECKS: Mapping[ApprovalStatus, CheckName] = {
    ApprovalStatus.PENDING: CheckName.APPROVAL_PENDING,
    ApprovalStatus.DENIED: CheckName.APPROVAL_DENIED,
    ApprovalStatus.REVOKED: CheckName.APPROVAL_REVOKED,
    ApprovalStatus.EXPIRED: CheckName.APPROVAL_EXPIRED,
}


class SimulatorError(Exception):
    """The simulator was used in a way the run does not allow."""


class WrongRole(SimulatorError):
    """The connection's role is not `ff_sim`."""


class NotSetUp(SimulatorError):
    """The run database has no simulator run yet."""


class AlreadySetUp(SimulatorError):
    """The run database already holds a simulator run."""


class ClockError(SimulatorError):
    """A call's virtual time is earlier than the run's last event."""


class ControlError(SimulatorError):
    """A control call that does not apply, such as revoking a pending approval."""


def _connect(conninfo: str) -> Connection:
    conn = psycopg.connect(conninfo, autocommit=True)
    try:
        row = conn.execute("SELECT current_user").fetchone()
        if row is None or row[0] != SIM_ROLE:
            raise WrongRole(f"the simulator connects as {SIM_ROLE}, not {row}")
    except BaseException:
        conn.close()
        raise
    return conn


def _check_vt(vt: int) -> None:
    if isinstance(vt, bool) or not isinstance(vt, int) or vt < 0:
        raise ValueError(f"virtual time is a non-negative integer, not {vt!r}")


def _check_name(name: str, what: str) -> None:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError(f"{what} is named by non-empty text, not {name!r}")


def supplier_directory(scenario: Scenario) -> JsonObject:
    """The `suppliers` fact: every supplier with a quote or on the allowlist, in id order."""
    quoted = {key.split("/")[1] for key in scenario.facts if fact_kind(key) is FactKind.QUOTE}
    allowlist = set(scenario.authority.allowlist)
    entries: list[JsonValue] = [
        {"supplier_id": supplier_id, "allowlisted": supplier_id in allowlist}
        for supplier_id in sorted(quoted | allowlist)
    ]
    return {"suppliers": entries}


def setup_run(conninfo: str, scenario: Scenario, config: SimConfig, *, vt: int = 0) -> None:
    """Set up the simulator's run: its configuration and the scenario's facts at version 1.

    Every initial fact, and the derived supplier directory, is valid from the setup
    event. A run database holds one run, so a second setup is refused.
    """
    _check_vt(vt)
    conn = _connect(conninfo)
    try:
        with conn.transaction():
            if conn.execute("SELECT 1 FROM sim.run").fetchone() is not None:
                raise AlreadySetUp("this run database already holds a simulator run")
            params = config.params
            conn.execute(
                "INSERT INTO sim.run (scenario_id, task_id, capabilities, max_hop_latency_ms,"
                " commit_deadline_ms, status_lag_ms, credentials, authority)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    scenario.id,
                    scenario.task_id,
                    sorted(str(capability) for capability in config.capabilities),
                    params.max_hop_latency_ms,
                    params.commit_deadline_ms,
                    params.status_lag_ms,
                    sorted(config.credentials),
                    Jsonb(scenario.authority.model_dump(mode="json")),
                ),
            )
            ledger = Ledger(conn, vt)
            seq = ledger.event("setup", SIM_ACTOR, scenario.id)
            facts: dict[str, Mapping[str, object]] = dict(scenario.fact_values())
            facts[SUPPLIERS_KEY] = supplier_directory(scenario)
            for key in sorted(facts):
                ledger.add_fact_version(key, 1, facts[key], seq, cause="initial")
    finally:
        conn.close()


class Simulator:
    """The simulator of one run. Close it, or use it as a context manager."""

    def __init__(self, conninfo: str) -> None:
        """Open a run that `setup_run` has set up."""
        self._conn = _connect(conninfo)
        try:
            row = self._conn.execute(
                "SELECT scenario_id, task_id, capabilities, max_hop_latency_ms,"
                " commit_deadline_ms, status_lag_ms, credentials, authority FROM sim.run"
            ).fetchone()
            if row is None:
                raise NotSetUp("the run database has no simulator run; call setup_run first")
            self.scenario_id = str(row[0])
            self.task_id = str(row[1])
            self.config = SimConfig(
                capabilities=frozenset(Capability(name) for name in row[2]),
                credentials=frozenset(str(name) for name in row[6]),
                params=ContractParams(
                    max_hop_latency_ms=int(row[3]),
                    commit_deadline_ms=int(row[4]),
                    status_lag_ms=int(row[5]),
                ),
            )
            self.authority = Authority.model_validate(row[7])
        except BaseException:
            self._conn.close()
            raise
        self.control = Control(self)

    @classmethod
    def setup(
        cls, conninfo: str, scenario: Scenario, config: SimConfig, *, vt: int = 0
    ) -> Simulator:
        """Set up the run (`setup_run`) and open it."""
        setup_run(conninfo, scenario, config, vt=vt)
        return cls(conninfo)

    def close(self) -> None:
        """Close the connection. Closing twice does nothing."""
        self._conn.close()

    def __enter__(self) -> Simulator:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # Transactions ----------------------------------------------------------------------

    @contextmanager
    def _transaction(self, vt: int) -> Iterator[Ledger]:
        """One transaction at virtual time `vt`, holding the run's row lock.

        Writes in flight past their commit deadline are aborted first (0005).
        """
        _check_vt(vt)
        with self._conn.transaction():
            row = self._conn.execute("SELECT last_vt FROM sim.run FOR UPDATE").fetchone()
            if row is None:
                raise NotSetUp("the run database has no simulator run")
            if vt < int(row[0]):
                raise ClockError(f"virtual time {vt} is before the last event, at {row[0]}")
            ledger = Ledger(self._conn, vt)
            for overdue in ledger.overdue_writes():
                seq = ledger.event("write_aborted", SIM_ACTOR, overdue.intent_id)
                ledger.set_write_outcome(overdue.received_seq, "aborted", outcome_seq=seq)
                ledger.aborted.append(overdue.received_seq)
            yield ledger

    def _allowed(self, caller: str) -> bool:
        _check_name(caller, "a caller")
        return caller in self.config.credentials

    @staticmethod
    def _forbidden(caller: str) -> Refusal:
        return Refusal(
            reason=RefusalReason.FORBIDDEN, detail=f"{caller} holds no credential for this run"
        )

    # Reads -----------------------------------------------------------------------------

    def get_quote(self, request: GetQuote, *, caller: str, vt: int) -> FactRead | Refusal:
        """Serve a supplier's quote for a sku."""
        return self._read(quote_key(request.supplier_id, request.sku), caller=caller, vt=vt)

    def list_suppliers(self, request: ListSuppliers, *, caller: str, vt: int) -> FactRead | Refusal:
        """Serve the supplier directory."""
        return self._read(SUPPLIERS_KEY, caller=caller, vt=vt)

    def get_budget(self, request: GetBudget, *, caller: str, vt: int) -> FactRead | Refusal:
        """Serve a cost center's budget."""
        return self._read(budget_key(request.cost_center), caller=caller, vt=vt)

    def get_inventory(self, request: GetInventory, *, caller: str, vt: int) -> FactRead | Refusal:
        """Serve a sku's stock in a warehouse."""
        return self._read(inventory_key(request.warehouse_id, request.sku), caller=caller, vt=vt)

    def _read(self, key: str, *, caller: str, vt: int) -> FactRead | Refusal:
        """Serve a fact's latest version, and log the read with its caller (0004)."""
        if not self._allowed(caller):
            return self._forbidden(caller)
        with self._transaction(vt) as ledger:
            latest = ledger.latest_fact(key)
            if latest is None:
                return Refusal(reason=RefusalReason.NOT_FOUND, detail=f"no fact {key}")
            version, value = latest
            seq = ledger.event("read", caller, key)
            ledger.log_read(seq, key, version, caller)
            return FactRead(key=key, version=version, data=value)

    # Approvals -------------------------------------------------------------------------

    def request_approval(
        self, request: RequestApproval, *, caller: str, vt: int
    ) -> ApprovalAnswer | Refusal:
        """File an approval request, which policy decides at once or queues (0002, rule 6).

        A request whose client request key was used before returns the original
        approval as it stands now, and files nothing (rule 4).
        """
        if not self._allowed(caller):
            return self._forbidden(caller)
        with self._transaction(vt) as ledger:
            if request.client_request_key is not None:
                original = ledger.approval_by_request_key(request.client_request_key)
                if original is not None:
                    ledger.event("approval_repeated", caller, original.approval_id)
                    return self._approval_answer(original, vt, repeated=True)
            approval_id = f"APR-{ledger.next_number('apr'):04d}"
            seq = ledger.event("approval_requested", caller, approval_id)
            scope = request.scope.model_dump()
            ledger.insert_approval(
                approval_id,
                intent_id=request.intent_id,
                operation=request.operation,
                scope=scope,
                client_request_key=request.client_request_key,
                requester=caller,
                requested_seq=seq,
            )
            ledger.add_approval_event(seq, approval_id, "requested", caller)
            decision = self.authority.policy_decision(request.operation, scope)
            if decision is not PolicyDecision.QUEUE:
                self._decide(ledger, approval_id, Decision(decision.value), actor=POLICY_ACTOR)
            return self._approval_answer(self._approval(ledger, approval_id), vt)

    def get_approval_status(
        self, request: GetApprovalStatus, *, caller: str, vt: int
    ) -> ApprovalAnswer | Refusal:
        """Report an approval's status now. The simulator never waits (see the module)."""
        if not self._allowed(caller):
            return self._forbidden(caller)
        with self._transaction(vt) as ledger:
            row = ledger.approval(request.approval_id)
            if row is None:
                return Refusal(
                    reason=RefusalReason.NOT_FOUND, detail=f"no approval {request.approval_id}"
                )
            ledger.event("approval_status", caller, row.approval_id)
            return self._approval_answer(row, vt)

    def _decide(self, ledger: Ledger, approval_id: str, decision: Decision, *, actor: str) -> None:
        """Decide a pending approval. An approval expires `approval_ttl_ms` later (rule 8)."""
        if decision is Decision.APPROVE:
            seq = ledger.event("approval_approved", actor, approval_id)
            ledger.add_approval_event(seq, approval_id, "approved", actor)
            expires_at = ledger.vt + self.authority.approval_ttl_ms
            ledger.set_decision(
                approval_id, "approved", seq=seq, actor=actor, expires_at=expires_at
            )
        else:
            seq = ledger.event("approval_denied", actor, approval_id)
            ledger.add_approval_event(seq, approval_id, "denied", actor)
            ledger.set_decision(approval_id, "denied", seq=seq, actor=actor, expires_at=None)

    @staticmethod
    def _approval(ledger: Ledger, approval_id: str) -> ApprovalRow:
        row = ledger.approval(approval_id)
        if row is None:
            raise ControlError(f"no approval {approval_id}")
        return row

    @staticmethod
    def _approval_answer(row: ApprovalRow, vt: int, *, repeated: bool = False) -> ApprovalAnswer:
        return ApprovalAnswer(
            approval_id=row.approval_id,
            intent_id=row.intent_id,
            operation=row.operation,
            scope=SCOPE_MODELS[row.operation].model_validate(row.scope),
            status=row.status_at(vt),
            requested_vt=row.requested_vt,
            decided_vt=row.decided_vt,
            decided_by=row.decided_by,
            expires_at=row.expires_at,
            revoked_vt=row.revoked_vt,
            repeated=repeated,
        )

    # Writes ----------------------------------------------------------------------------

    def submit_purchase_order(
        self, request: SubmitPurchaseOrder, *, caller: str, vt: int
    ) -> WriteAnswer:
        """Receive a purchase order and commit it at once, in one transaction."""
        return self._submit(request, caller=caller, vt=vt)

    def reserve_inventory(self, request: ReserveInventory, *, caller: str, vt: int) -> WriteAnswer:
        """Receive a reservation and commit it at once, in one transaction."""
        return self._submit(request, caller=caller, vt=vt)

    def receive_write(
        self, request: Write, *, caller: str, vt: int, waive_deadline: bool = False
    ) -> WriteAnswer | WriteReceived:
        """The harness's split path, first half: the write reaches the destination now.

        A key lookup may answer at once (a replay, or `in_progress`). Otherwise the write
        is in flight until `commit_received`, which must come by its deadline (arrival
        plus D) or the write aborts. `waive_deadline` is fault F5 beyond the deadline, a
        contract violation: the write can then commit later than D (0006).
        """
        if not self._allowed(caller):
            return self._forbidden_write(caller)
        with self._transaction(vt) as ledger:
            received = self._receive(ledger, request, caller, waive_deadline=waive_deadline)
            if not isinstance(received, WriteRow):
                return received
            return WriteReceived(
                received_seq=received.received_seq, deadline_vt=received.deadline_vt
            )

    def commit_received(self, received_seq: int, *, vt: int) -> WriteAnswer | WriteAborted:
        """The harness's split path, second half: commit a write that is in flight.

        Returns `WriteAborted` if the write missed its deadline.
        """
        with self._transaction(vt) as ledger:
            write = ledger.write(received_seq)
            if write is None:
                raise ControlError(f"no write received at seq {received_seq}")
            if write.state == "aborted":
                return WriteAborted(received_seq=received_seq, deadline_vt=write.deadline_vt)
            if write.state != "received":
                raise ControlError(f"write {received_seq} already ended as {write.state}")
            return self._commit(ledger, write)

    def _submit(self, request: Write, *, caller: str, vt: int) -> WriteAnswer:
        if not self._allowed(caller):
            return self._forbidden_write(caller)
        with self._transaction(vt) as ledger:
            received = self._receive(ledger, request, caller, waive_deadline=False)
            if not isinstance(received, WriteRow):
                return received
            return self._commit(ledger, received)

    @staticmethod
    def _forbidden_write(caller: str) -> WriteRejected:
        return WriteRejected(
            reason=WriteRejectReason.FORBIDDEN,
            detail=f"{caller} holds no credential for this run",
        )

    def _receive(
        self, ledger: Ledger, request: Write, caller: str, *, waive_deadline: bool
    ) -> WriteAnswer | WriteRow:
        """Record a write's arrival, then look up its key (0005).

        With the idempotency_key capability, a key that already committed gets the stored
        result, marked as a replay, and a key in flight gets `in_progress`. Otherwise the
        write is in flight, and its row is returned.
        """
        key = request.idempotency_key
        honoured = self.config.has(Capability.IDEMPOTENCY_KEY) and key is not None
        received_seq = ledger.event("write_received", caller, request.intent_id)
        new = NewWrite(
            received_seq=received_seq,
            caller=caller,
            operation=request.operation,
            intent_id=request.intent_id,
            approval_id=request.approval_id,
            args=request.business_args(),
            basis=dict(request.basis),
            idempotency_key=key,
            key_honoured=honoured,
            client_ref=request.client_ref,
            deadline_vt=ledger.vt + self.config.params.commit_deadline_ms,
            deadline_waived=waive_deadline,
        )
        if honoured and key is not None:
            stored = ledger.effect_by_key(key)
            if stored is not None:
                seq = ledger.event("write_replayed", caller, request.intent_id)
                ledger.insert_write(
                    new, state="replayed", outcome_seq=seq, effect_id=stored.effect_id
                )
                return self._committed(stored, replay=True)
            if ledger.key_in_flight(key):
                seq = ledger.event("write_in_progress", caller, request.intent_id)
                ledger.insert_write(new, state="in_progress", outcome_seq=seq)
                return WriteInProgress(idempotency_key=key)
        ledger.insert_write(new)
        write = ledger.write(received_seq)
        if write is None:
            raise SimulatorError(f"write {received_seq} was not recorded")
        return write

    def _commit(self, ledger: Ledger, write: WriteRow) -> WriteAnswer:
        """Decide a write in flight, inside the commit transaction (0005).

        The key lookup already happened on arrival (`_receive`). While this write is in
        flight, any other write with its key got `in_progress`, so no effect can have
        stored the key since. The unique index on `sim.effect.idempotency_key` still
        enforces this inside the commit transaction: a late duplicate fails the
        transaction instead of creating a second effect. At C3 the preconditions come
        next. Then the write commits: one effect, and its consequence as a new version of
        the budget or the stock.
        """
        if self.config.has(Capability.CONDITIONAL_WRITE):
            failed = self._preconditions(ledger, write)
            if failed:
                rejection = WriteRejected(
                    reason=WriteRejectReason.PRECONDITION_FAILED, failed_checks=failed
                )
                return self._reject(ledger, write, rejection)
        target = consequence_key(write.operation, write.args)
        current = ledger.latest_fact(target)
        if current is None:
            rejection = WriteRejected(
                reason=WriteRejectReason.UNKNOWN_FACT, detail=f"no fact {target}"
            )
            return self._reject(ledger, write, rejection)
        seq = ledger.event("write_committed", write.caller, write.intent_id)
        if write.operation is Operation.SUBMIT_PURCHASE_ORDER:
            effect_id = f"PO-{ledger.next_number('po'):04d}"
        else:
            effect_id = f"RS-{ledger.next_number('rs'):04d}"
        ledger.insert_effect(effect_id, write, commit_seq=seq)
        version, value = current
        ledger.add_fact_version(
            target,
            version + 1,
            consequence(write.operation, write.args, value),
            seq,
            cause="effect",
            effect_id=effect_id,
        )
        approval = ledger.approval(write.approval_id)
        if approval is not None and approval.effect_id is None:
            ledger.set_backed_effect(write.approval_id, effect_id)
        ledger.set_write_outcome(
            write.received_seq, "committed", outcome_seq=seq, effect_id=effect_id
        )
        return WriteCommitted(
            effect_id=effect_id,
            replay=False,
            intent_id=write.intent_id,
            operation=write.operation,
            args=write.args,
            committed_vt=ledger.vt,
        )

    def _preconditions(self, ledger: Ledger, write: WriteRow) -> list[FailedCheck]:
        """The conditional write's checks at commit: authority, then each input (0005).

        The approval must be approved, unrevoked and unexpired now, in scope for the write,
        and back no effect yet (0002, rule 11). Each dependency must be declared in the
        basis, served before the write arrived, and satisfy its rule now (0004).
        """
        failed: list[FailedCheck] = []
        approval = ledger.approval(write.approval_id)
        if approval is None:
            failed.append(FailedCheck(check=CheckName.APPROVAL_UNKNOWN))
        else:
            status = approval.status_at(ledger.vt)
            if status is not ApprovalStatus.APPROVED:
                failed.append(FailedCheck(check=_STATUS_CHECKS[status]))
            mismatched = scope_mismatches(
                approval_intent_id=approval.intent_id,
                approval_operation=approval.operation,
                scope=approval.scope,
                intent_id=write.intent_id,
                operation=write.operation,
                args=write.args,
            )
            if mismatched:
                failed.append(
                    FailedCheck(
                        check=CheckName.APPROVAL_OUT_OF_SCOPE, mismatched_fields=list(mismatched)
                    )
                )
            if approval.effect_id is not None:
                failed.append(
                    FailedCheck(check=CheckName.APPROVAL_USED, effect_id=approval.effect_id)
                )
        history = History(ledger, write.received_seq)
        for check in check_basis(write.operation, write.args, write.basis, history):
            if check.failure is not None:
                failed.append(
                    FailedCheck(
                        check=CheckName(check.failure.value),
                        key=check.dependency.key,
                        cited_version=check.cited_version,
                        current_version=check.current_version,
                        changed_fields=list(check.changed_fields),
                    )
                )
        return failed

    @staticmethod
    def _reject(ledger: Ledger, write: WriteRow, rejection: WriteRejected) -> WriteRejected:
        seq = ledger.event("write_rejected", write.caller, write.intent_id)
        ledger.set_write_outcome(
            write.received_seq,
            "rejected",
            outcome_seq=seq,
            rejection=rejection.model_dump(mode="json"),
        )
        return rejection

    @staticmethod
    def _committed(effect: EffectRow, *, replay: bool) -> WriteCommitted:
        return WriteCommitted(
            effect_id=effect.effect_id,
            replay=replay,
            intent_id=effect.intent_id,
            operation=effect.operation,
            args=effect.args,
            committed_vt=effect.committed_vt,
        )

    # Status (C1) -----------------------------------------------------------------------

    def get_order_status(
        self,
        request: GetOrderStatus,
        *,
        caller: str,
        vt: int,
        as_of_vt: int | None = None,
        reflect_vt: int | None = None,
        contract_violation: bool = False,
    ) -> StatusAnswer | Refusal:
        """Which purchase orders exist for an intent (`_status` explains the options)."""
        return self._status(
            Operation.SUBMIT_PURCHASE_ORDER,
            request.intent_id,
            caller=caller,
            vt=vt,
            as_of_vt=as_of_vt,
            reflect_vt=reflect_vt,
            contract_violation=contract_violation,
        )

    def get_reservation_status(
        self,
        request: GetReservationStatus,
        *,
        caller: str,
        vt: int,
        as_of_vt: int | None = None,
        reflect_vt: int | None = None,
        contract_violation: bool = False,
    ) -> StatusAnswer | Refusal:
        """Which reservations exist for an intent (`_status` explains the options)."""
        return self._status(
            Operation.RESERVE_INVENTORY,
            request.intent_id,
            caller=caller,
            vt=vt,
            as_of_vt=as_of_vt,
            reflect_vt=reflect_vt,
            contract_violation=contract_violation,
        )

    def _status(
        self,
        operation: Operation,
        intent_id: str,
        *,
        caller: str,
        vt: int,
        as_of_vt: int | None,
        reflect_vt: int | None,
        contract_violation: bool,
    ) -> StatusAnswer | Refusal:
        """Answer a status lookup, accurate to `as_of_vt` (0005).

        By default the answer is fresh: `as_of_vt` is `vt`. The harness may make it stale,
        down to `vt - status_lag_ms`, as the destination's lag allows (F10, stale within
        L). An answer that lists the effects as of `reflect_vt` while claiming another
        `as_of_vt`, or that is staler than L, breaks the destination's promise; only a
        fault may ask for one, with `contract_violation` (F10, stale beyond L).
        """
        if not self._allowed(caller):
            return self._forbidden(caller)
        if not self.config.has(Capability.STATUS):
            return Refusal(
                reason=RefusalReason.UNSUPPORTED, detail="this destination has no status lookup"
            )
        _check_vt(vt)
        claimed = vt if as_of_vt is None else as_of_vt
        reflected = claimed if reflect_vt is None else reflect_vt
        _check_vt(claimed)
        _check_vt(reflected)
        if claimed > vt or reflected > vt:
            raise ValueError("a status answer cannot reflect the future")
        within_lag = claimed >= vt - self.config.params.status_lag_ms
        if (not within_lag or reflected != claimed) and not contract_violation:
            raise ValueError(
                "this answer breaks the status promise; only a fault may ask for it"
                " (contract_violation=True)"
            )
        with self._transaction(vt) as ledger:
            effects = ledger.effects_for(intent_id, operation, reflected)
            ledger.event("status", caller, intent_id)
            return StatusAnswer(
                intent_id=intent_id,
                operation=operation,
                effects=[
                    StatusEffect(effect_id=effect.effect_id, args=effect.args) for effect in effects
                ],
                as_of_vt=claimed,
            )


class Control:
    """The harness's control interface: scripted humans and fault plans (0002, 0006).

    It is in-process only. Agents never get it: they reach the simulator through the
    harness's transports (B modes) or not at all (G modes).
    """

    def __init__(self, simulator: Simulator) -> None:
        self._sim = simulator

    def decide(
        self, approval_id: str, decision: Decision, *, actor: str, vt: int
    ) -> ApprovalAnswer:
        """The scripted approver decides a queued request (0002, rule 7)."""
        _check_name(actor, "an actor")
        sim = self._sim
        with sim._transaction(vt) as ledger:
            row = sim._approval(ledger, approval_id)
            if row.status != "pending":
                raise ControlError(f"approval {approval_id} is {row.status}, not pending")
            sim._decide(ledger, approval_id, decision, actor=actor)
            return sim._approval_answer(sim._approval(ledger, approval_id), vt)

    def revoke(self, approval_id: str, *, actor: str, vt: int) -> ApprovalAnswer:
        """Revoke an approved approval, for good (0002, rule 8; fault F6)."""
        _check_name(actor, "an actor")
        sim = self._sim
        with sim._transaction(vt) as ledger:
            row = sim._approval(ledger, approval_id)
            if row.status != "approved":
                raise ControlError(f"approval {approval_id} is {row.status}; only approved ones")
            seq = ledger.event("approval_revoked", actor, approval_id)
            ledger.add_approval_event(seq, approval_id, "revoked", actor)
            ledger.set_revocation(approval_id, seq=seq, actor=actor)
            return sim._approval_answer(sim._approval(ledger, approval_id), vt)

    def change_fact(
        self, key: str, changes: Mapping[str, str | int], *, actor: str, vt: int
    ) -> FactRead:
        """Append a version of a quote, budget or stock with some fields changed (fault F7).

        The new version is caused by `actor`. It is not a read, so it is not logged as one.
        """
        _check_name(actor, "an actor")
        try:
            kind = fact_kind(key)
        except ValueError as error:
            raise ControlError(str(error)) from error
        if kind is FactKind.SUPPLIERS:
            raise ControlError("the supplier directory never changes")
        if not changes:
            raise ControlError("a change names at least one field")
        with self._sim._transaction(vt) as ledger:
            latest = ledger.latest_fact(key)
            if latest is None:
                raise ControlError(f"no fact {key}")
            version, current = latest
            unknown = sorted(set(changes) - set(current))
            if unknown:
                raise ControlError(f"fact {key} has no fields {unknown}")
            value: JsonObject = dict(current)
            value.update(changes)
            try:
                FACT_MODELS[kind].model_validate(value)
            except ValidationError as error:
                raise ControlError(f"the change breaks fact {key}: {error}") from error
            if value == current:
                raise ControlError(f"the change leaves fact {key} as it is")
            seq = ledger.event("fact_changed", actor, key)
            ledger.add_fact_version(key, version + 1, value, seq, cause="control", actor=actor)
            return FactRead(key=key, version=version + 1, data=json_object(value))

    def current_fact(self, key: str) -> FactRead | None:
        """The latest version of a fact, read without serving it: no read is logged.

        Fault plans use it to compute a change, such as F7's new price (0006).
        """
        sim = self._sim
        with sim._conn.transaction():
            row = sim._conn.execute("SELECT last_vt FROM sim.run").fetchone()
            vt = 0 if row is None else int(row[0])
            latest = Ledger(sim._conn, vt).latest_fact(key)
        if latest is None:
            return None
        version, value = latest
        return FactRead(key=key, version=version, data=json_object(value))

    def pending_approvals(self) -> list[ApprovalAnswer]:
        """Requests waiting for the scripted approver, in the order they were filed."""
        sim = self._sim
        with sim._conn.transaction():
            row = sim._conn.execute("SELECT last_vt FROM sim.run").fetchone()
            vt = 0 if row is None else int(row[0])
            ledger = Ledger(sim._conn, vt)
            return [sim._approval_answer(item, vt) for item in ledger.pending_approvals()]

    def abort_overdue(self, *, vt: int) -> list[int]:
        """Abort every write in flight past its deadline, as every call does first.

        The harness calls this when the drain window ends (0009), so the ledger shows
        every write that never committed as aborted. Returns their received seqs.
        """
        with self._sim._transaction(vt) as ledger:
            return list(ledger.aborted)
