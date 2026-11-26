"""The evaluator's checks on hand-built records, without a database (0004).

`World` builds a run's records the way the harness and the simulator would write them:
one harness counter numbers every send, delivery, fault, actor action and report, and one
simulator counter numbers every simulator event, so the order of the calls here is the
order of events in the run. By default a write cites an approval that policy granted for
its intent, and the versions the stack read of its dependencies. The property tests at
the end are 0004's Checks: `unknown` never passes when commit evidence was delivered and
not voided, and a missing effect never fails both property 1 and property 4; and 0014's:
recovery latency is never negative, ends where the stack's knowledge settles, and no
wall-clock column changes a virtual value.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fivefold.domain.api import (
    CheckName,
    StatusAnswer,
    StatusEffect,
    WriteCommitted,
    WriteInProgress,
    WriteRejected,
    WriteRejectReason,
)
from fivefold.domain.authority import POLICY_ACTOR
from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.rules import (
    BasisFailure,
    Operation,
    consequence,
    consequence_key,
    dependencies,
)
from fivefold.domain.scenario import Scenario, scenario_digest, scenario_from_data
from fivefold.evaluator import (
    RECOVERY_METRICS,
    RUN_METRICS,
    TOKEN_METRICS,
    ActorAction,
    Approval,
    Config,
    Destination,
    Effect,
    Excuse,
    ExcusedIntent,
    FactVersion,
    Fault,
    Knowledge,
    Message,
    ModelCall,
    Outcome,
    PropertyResult,
    PropertyVerdict,
    Read,
    ReasonCode,
    RecordsError,
    RecoveryEntry,
    RunInfo,
    RunRecords,
    RunResult,
    SettledBy,
    Settlement,
    SubmittedReport,
    Verdict,
    comparable_json,
    evaluate,
    facts_without_effects,
    result_json,
    run_verdict,
    stack_knowledge,
    without_wall_clock,
)

PO = "submit_purchase_order"
RS = "reserve_inventory"
PARAMS = ContractParams(max_hop_latency_ms=50, commit_deadline_ms=10_000, status_lag_ms=0)
S = PARAMS.settle_ms
HOP = PARAMS.max_hop_latency_ms

SCENARIO_DATA: dict[str, object] = {
    "id": "eval-two-intents",
    "task_id": "TASK-eval",
    "title": "Order and reserve widgets",
    "task": "Complete REQ-1 (an order) and REQ-2 (a reservation), then report.",
    "intents": [
        {
            "id": "REQ-1",
            "operation": PO,
            "accept": {
                "sku": "W-100",
                "quantity": 100,
                "suppliers": ["S-1", "S-2"],
                "max_unit_price_minor": 600,
                "currency": "USD",
                "cost_center": "CC-OPS",
            },
        },
        {
            "id": "REQ-2",
            "operation": RS,
            "required": False,
            "accept": {"warehouse_id": "WH-1", "sku": "W-100", "quantity": 10},
        },
    ],
    "facts": {
        "quote/S-1/W-100": {
            "unit_price_minor": 540,
            "currency": "USD",
            "min_quantity": 10,
            "lead_time_days": 5,
            "status": "active",
        },
        "budget/CC-OPS": {"limit_minor": 100_000, "committed_minor": 20_000},
        "inventory/WH-1/W-100": {"available": 50},
    },
    "authority": {
        "allowlist": ["S-1", "S-2"],
        "per_order_ceiling_minor": 100_000,
        "auto_approve_up_to_minor": 60_000,
        "approval_ttl_ms": 3_600_000,
        "approval_wait_ms": 600_000,
    },
    "contracts": ["C0", "C1", "C2", "C3"],
    "budget": {"max_agent_steps": 40, "max_tokens": 60_000},
}
SCENARIO = scenario_from_data(SCENARIO_DATA)
"""REQ-1 is required; REQ-2 is not, so tests about other properties need not complete it."""


def scenario_with(*, required: bool = False, **authority: object) -> Scenario:
    """The test scenario with REQ-2 required too, if asked, and authority values changed."""
    data: dict[str, Any] = copy.deepcopy(SCENARIO_DATA)
    data["intents"][1]["required"] = required
    data["authority"].update(authority)
    return scenario_from_data(data)


BOTH_REQUIRED = scenario_with(required=True)
TTL = SCENARIO.authority.approval_ttl_ms
ORDER_ARGS: dict[str, str | int] = {
    "supplier_id": "S-1",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 540,
    "currency": "USD",
    "cost_center": "CC-OPS",
}
RESERVATION_ARGS: dict[str, str | int] = {"warehouse_id": "WH-1", "sku": "W-100", "quantity": 10}

Claims = Mapping[str, tuple[str, Sequence[str]]]


def scope_of(operation: str, args: Mapping[str, str | int]) -> dict[str, object]:
    """The scope of an approval for exactly this write (0002, rule 5)."""
    quantity = int(args["quantity"])
    if operation == RS:
        return {"warehouse_id": args["warehouse_id"], "sku": args["sku"], "max_quantity": quantity}
    return {
        "supplier_id": args["supplier_id"],
        "sku": args["sku"],
        "max_quantity": quantity,
        "max_amount_minor": quantity * int(args["unit_price_minor"]),
        "currency": args["currency"],
        "cost_center": args["cost_center"],
    }


def committed_body(effect: Effect, *, replay: bool = False) -> dict[str, object]:
    """The simulator's answer naming `effect`, as JSON."""
    return WriteCommitted(
        effect_id=effect.effect_id,
        replay=replay,
        intent_id=effect.intent_id,
        operation=effect.operation,
        args={key: value for key, value in effect.args.items() if isinstance(value, str | int)},
        committed_vt=effect.committed_vt,
    ).model_dump(mode="json")


class World:
    """A run's records, built in event order."""

    def __init__(
        self,
        config: Config = Config.G2,
        contract: Contract = Contract.C2,
        scenario: Scenario = SCENARIO,
    ) -> None:
        self.config = config
        self.contract = contract
        self.scenario = scenario
        self.seq = 0
        self.sim_seq = 100_000
        self.effects: list[Effect] = []
        self.messages: dict[int, Message] = {}
        self.faults: list[Fault] = []
        self.actions: list[ActorAction] = []
        self.reports: list[SubmittedReport] = []
        self.approvals: dict[str, Approval] = {}
        self.facts: list[FactVersion] = []
        self.reads: list[Read] = []
        self.model_calls: list[ModelCall] = []
        setup = self.sim_next()
        for key, value in scenario.fact_values().items():
            self.facts.append(FactVersion(key, 1, value, setup, 0, "initial"))
        for key in sorted(scenario.facts):
            self.read(key)

    @property
    def stack(self) -> str:
        """The stack component that talks to the simulator."""
        return "gateway" if self.config.has_gateway else "agent-1"

    def next(self) -> int:
        self.seq += 1
        return self.seq

    def sim_next(self) -> int:
        """The next simulator event."""
        self.sim_seq += 1
        return self.sim_seq

    # Facts and approvals -------------------------------------------------------------------

    def latest(self, key: str) -> FactVersion:
        return max((fact for fact in self.facts if fact.key == key), key=lambda f: f.version)

    def read(self, key: str, *, caller: str | None = None, vt: int = 0) -> Read:
        """The simulator serves the latest version of `key` to the stack."""
        read = Read(self.sim_next(), key, self.latest(key).version, caller or self.stack, vt)
        self.reads.append(read)
        return read

    def change(self, key: str, *, vt: int = 0, **fields: object) -> FactVersion:
        """A change through the simulator's control interface, as fault F7 makes."""
        latest = self.latest(key)
        value = {**latest.value, **fields}
        fact = FactVersion(key, latest.version + 1, value, self.sim_next(), vt, "control")
        self.facts.append(fact)
        return fact

    def approval(
        self,
        intent_id: str = "REQ-1",
        *,
        decision: str | None = "approve",
        by: str = POLICY_ACTOR,
        vt: int = 0,
        operation: str | None = None,
        args: Mapping[str, str | int] | None = None,
        **scope: object,
    ) -> Approval:
        """An approval request for `intent_id`, decided at once unless `decision` is None.

        Its scope covers exactly the write with `args`, the intent's usual write by
        default, with any `scope` field changed.
        """
        if operation is None:
            known = {intent.id: str(intent.operation) for intent in self.scenario.intents}
            operation = known.get(intent_id, PO)
        if args is None:
            args = ORDER_ARGS if operation == PO else RESERVATION_ARGS
        approval = Approval(
            approval_id=f"APR-{len(self.approvals) + 1:04d}",
            intent_id=intent_id,
            operation=Operation(operation),
            scope={**scope_of(operation, args), **scope},
            requested_seq=self.sim_next(),
            requested_vt=vt,
            status="pending",
        )
        self.approvals[approval.approval_id] = approval
        if decision is None:
            return approval
        return self.decide(approval.approval_id, decision, by=by, vt=vt)

    def decide(
        self, approval_id: str, decision: str, *, by: str = "approver", vt: int = 0
    ) -> Approval:
        approved = decision == "approve"
        approval = dataclasses.replace(
            self.approvals[approval_id],
            status="approved" if approved else "denied",
            decided_seq=self.sim_next(),
            decided_vt=vt,
            decided_by=by,
            expires_at=vt + self.scenario.authority.approval_ttl_ms if approved else None,
        )
        self.approvals[approval_id] = approval
        return approval

    def revoke(self, approval_id: str, *, vt: int = 0) -> Approval:
        approval = dataclasses.replace(
            self.approvals[approval_id],
            status="revoked",
            revoked_seq=self.sim_next(),
            revoked_vt=vt,
            revoked_by="approver",
        )
        self.approvals[approval_id] = approval
        return approval

    def approval_for(self, intent_id: str, operation: str, args: Mapping[str, str | int]) -> str:
        """The first approval filed for the intent, or a new one that policy grants."""
        for approval in self.approvals.values():
            if approval.intent_id == intent_id:
                return approval.approval_id
        return self.approval(intent_id, operation=operation, args=args).approval_id

    # Messages ----------------------------------------------------------------------------

    def send(
        self,
        body: object,
        *,
        vt: int,
        kind: str = "request",
        api: str | None = PO,
        sender: str | None = None,
        receiver: str = "simulator",
        hop: str | None = None,
        correlation: int | None = None,
    ) -> Message:
        message = Message(
            seq=self.next(),
            hop=hop or self.config.stack_hop,
            kind=kind,
            sender=sender or self.stack,
            receiver=receiver,
            api=api,
            body=body,
            sent_vt=vt,
            correlation=correlation,
        )
        self.messages[message.seq] = message
        return message

    def deliver(self, message: Message, *, vt: int | None = None) -> Message:
        delivered = dataclasses.replace(
            message,
            fate="delivered",
            delivered_seq=self.next(),
            delivered_vt=message.sent_vt + 10 if vt is None else vt,
        )
        self.messages[message.seq] = delivered
        return delivered

    def drop(self, message: Message, reason: str = "fault") -> Message:
        dropped = dataclasses.replace(message, fate="dropped", drop_reason=reason)
        self.messages[message.seq] = dropped
        return dropped

    def copy(self, request: Message, *, deliver: bool = True) -> Message:
        """A copy the transport made of `request` when it delivered it twice (F4)."""
        copied = dataclasses.replace(
            request,
            seq=self.next(),
            copy_of=request.seq,
            fate=None,
            drop_reason=None,
            delivered_seq=None,
            delivered_vt=None,
        )
        self.messages[copied.seq] = copied
        return self.deliver(copied) if deliver else copied

    def request(
        self,
        intent_id: str = "REQ-1",
        *,
        vt: int = 1_000,
        key: str | None = None,
        sender: str | None = None,
        deliver: bool = True,
        operation: str | None = None,
        approval: str | None = None,
        basis: Mapping[str, int] | None = None,
        **changes: str | int,
    ) -> Message:
        """A stack write request, delivered unless told not to.

        Its operation is the intent's, or a purchase order for an intent outside the task.
        It cites `approval`, or the first one filed for the intent, and `basis`, or the
        versions the stack last read of the write's dependencies. `changes` change the
        usual args.
        """
        if operation is None:
            known = {intent.id: str(intent.operation) for intent in self.scenario.intents}
            operation = known.get(intent_id, PO)
        args = {**(ORDER_ARGS if operation == PO else RESERVATION_ARGS), **changes}
        if approval is None:
            approval = self.approval_for(intent_id, operation, args)
        if basis is None:
            last = {read.key: read.version for read in self.reads}
            basis = {
                dependency.key: last[dependency.key]
                for dependency in dependencies(Operation(operation), args)
                if dependency.key in last
            }
        body: dict[str, object] = {"intent_id": intent_id, "approval_id": approval}
        body.update(basis=dict(basis), idempotency_key=key, **args)
        message = self.send(body, vt=vt, api=operation, sender=sender)
        return self.deliver(message) if deliver else message

    def commit(self, request: Message, *, vt: int | None = None) -> Effect:
        """The simulator commits `request` as a new effect, and the effect's consequence
        as a new version of the budget or the stock."""
        body = request.body
        assert isinstance(body, dict)
        operation = Operation(str(request.api))
        number = 1 + sum(1 for effect in self.effects if effect.operation is operation)
        prefix = "PO" if operation is Operation.SUBMIT_PURCHASE_ORDER else "RS"
        args = {k: v for k, v in body.items() if k in ORDER_ARGS or k in RESERVATION_ARGS}
        effect = Effect(
            effect_id=f"{prefix}-{number:04d}",
            intent_id=str(body["intent_id"]),
            operation=operation,
            args=args,
            approval_id=str(body["approval_id"]),
            basis=dict(body["basis"]),
            idempotency_key=body.get("idempotency_key"),
            client_ref=None,
            received_seq=self.sim_next(),
            commit_seq=self.sim_next(),
            committed_vt=request.sent_vt + 20 if vt is None else vt,
        )
        self.effects.append(effect)
        target = self.latest(consequence_key(operation, args))
        self.facts.append(
            FactVersion(
                target.key,
                target.version + 1,
                consequence(operation, args, target.value),
                effect.commit_seq,
                effect.committed_vt,
                "effect",
                effect.effect_id,
            )
        )
        return effect

    def answer(
        self, request: Message, body: object, *, vt: int | None = None, fate: str = "delivered"
    ) -> Message:
        """The simulator's answer to `request`: delivered, dropped or left in flight."""
        message = self.send(
            body,
            vt=request.sent_vt + 20 if vt is None else vt,
            kind="answer",
            api=request.api,
            sender="simulator",
            receiver=request.sender,
            hop=request.hop,
            correlation=request.seq,
        )
        if fate == "delivered":
            return self.deliver(message)
        if fate == "dropped":
            return self.drop(message)
        return message

    def committed(
        self,
        request: Message,
        effect: Effect,
        *,
        replay: bool = False,
        fate: str = "delivered",
        vt: int | None = None,
    ) -> Message:
        return self.answer(request, committed_body(effect, replay=replay), fate=fate, vt=vt)

    def rejected(self, request: Message, *, fate: str = "delivered") -> Message:
        body = WriteRejected(reason=WriteRejectReason.UNKNOWN_FACT).model_dump(mode="json")
        return self.answer(request, body, fate=fate)

    def in_progress(self, request: Message) -> Message:
        assert isinstance(request.body, dict)
        key = str(request.body["idempotency_key"])
        return self.answer(request, WriteInProgress(idempotency_key=key).model_dump(mode="json"))

    def status(
        self,
        intent_id: str,
        effects: Sequence[Effect] = (),
        *,
        as_of_vt: int,
        operation: str | None = None,
        fate: str = "delivered",
    ) -> Message:
        """A status lookup by the stack and the simulator's answer, as of `as_of_vt`."""
        op = operation or str(self.scenario.intent(intent_id).operation)
        api = "get_order_status" if op == PO else "get_reservation_status"
        request = self.deliver(self.send({"intent_id": intent_id}, vt=as_of_vt - 20, api=api))
        body = StatusAnswer(
            intent_id=intent_id,
            operation=Operation(op),
            effects=[
                StatusEffect(
                    effect_id=effect.effect_id,
                    args={k: v for k, v in effect.args.items() if isinstance(v, str | int)},
                )
                for effect in effects
            ],
            as_of_vt=as_of_vt,
        ).model_dump(mode="json")
        return self.answer(request, body, vt=as_of_vt, fate=fate)

    # Faults, humans and reports ----------------------------------------------------------

    def crash(
        self,
        *voided: Message,
        point: str = "gw.dispatch.after_answer",
        vt: int = 0,
        intent_id: str | None = None,
    ) -> Fault:
        """A crash that voids `voided`; it touched `intent_id`, if given (0014)."""
        fault = Fault(
            seq=self.next(),
            fault="F3",
            point=point,
            occurrence=1,
            action="kill",
            vt=vt,
            component=self.stack,
            voided=tuple(message.seq for message in voided),
            params={},
            intent_id=intent_id,
        )
        self.faults.append(fault)
        return fault

    def possibly_sent(
        self,
        intent_id: str = "REQ-1",
        *,
        vt: int = 1_000,
        key: str | None = None,
        touches: bool = False,
    ) -> Fault:
        """A gateway crash after recording an attempt and before sending it (F1b). With
        `touches`, its record names the intent it touched, as the harness writes it."""
        request: dict[str, object] = {"intent_id": intent_id, "idempotency_key": key}
        fault = Fault(
            seq=self.next(),
            fault="F1",
            variant="b",
            point="gw.dispatch.after_record",
            occurrence=1,
            action="kill",
            vt=vt,
            component="gateway",
            params={"restart_delay_ms": 1_000, "request": request},
            intent_id=intent_id if touches else None,
        )
        self.faults.append(fault)
        return fault

    def touch(
        self,
        intent_id: str | None,
        *,
        vt: int,
        fault: str = "F2",
        point: str = "net.gw_sim.answer",
        target: Message | None = None,
    ) -> Fault:
        """A fault whose record names the task intent it touched, as the harness writes it
        (0014), on the message `target` if any. It does nothing else here."""
        record = Fault(
            seq=self.next(),
            fault=fault,
            point=point,
            occurrence=1,
            action="drop",
            vt=vt,
            target_seq=None if target is None else target.seq,
            params={},
            intent_id=intent_id,
        )
        self.faults.append(record)
        return record

    def attest(
        self,
        resolution: str,
        *,
        vt: int,
        intent_id: str | None = None,
        effect_id: str | None = None,
    ) -> ActorAction:
        details: dict[str, object] = {"resolution": resolution}
        if intent_id is not None:
            details["intent_id"] = intent_id
        if effect_id is not None:
            details["effect_id"] = effect_id
        action = ActorAction(
            seq=self.next(),
            actor="resolver",
            action="attest",
            subject="ESC-0001",
            details=details,
            vt=vt,
        )
        self.actions.append(action)
        return action

    def report(
        self,
        claims: Claims,
        *,
        vt: int = 50_000,
        worker: str = "agent-1",
        valid: bool = True,
        report: object = None,
    ) -> SubmittedReport:
        """A final report, carried to the gateway (G modes) or the sink (B0)."""
        data = report or {
            "claims": [
                {"intent_id": intent_id, "status": status, "effect_ids": list(effects)}
                for intent_id, (status, effects) in claims.items()
            ]
        }
        if self.config.has_gateway:
            carried = self.send(
                {"jsonrpc": "2.0", "id": 9, "method": "tools/call"},
                vt=vt,
                api="submit_final_report",
                sender=worker,
                receiver="gateway",
                hop="agent_gw",
            )
        else:
            carried = self.send(
                data, vt=vt, api="submit_final_report", sender=worker, receiver="report_sink"
            )
        carried = self.deliver(carried)
        record = SubmittedReport(
            seq=self.next(),
            worker=worker,
            report=data,
            valid=valid,
            accepted_by=self.config.accepted_by,
            vt=vt + 10,
            message_seq=carried.seq,
        )
        self.reports.append(record)
        return record

    def records(self) -> RunRecords:
        return RunRecords(
            run=RunInfo(
                run_id="RUN-1",
                scenario_id=self.scenario.id,
                scenario_digest=scenario_digest(self.scenario),
                config=self.config,
                contract=self.contract,
                seed=7,
                topology="inproc",
                model_adapter="scripted",
                model="scripted-policy",
                params={"max_hop_latency_ms": HOP},
            ),
            scenario=self.scenario,
            destination=Destination(
                scenario_id=self.scenario.id,
                task_id=self.scenario.task_id,
                capabilities=frozenset(str(c) for c in self.contract.capabilities),
                params=PARAMS,
            ),
            effects=tuple(self.effects),
            messages=tuple(self.messages[seq] for seq in sorted(self.messages)),
            faults=tuple(self.faults),
            actor_actions=tuple(self.actions),
            reports=tuple(self.reports),
            approvals=tuple(self.approvals.values()),
            fact_versions=tuple(self.facts),
            reads=tuple(self.reads),
            model_calls=tuple(self.model_calls),
        )

    def model_call(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        *,
        refused: str | None = None,
        model: str = "scripted-policy",
        worker: str = "agent-1",
    ) -> ModelCall:
        """A model call of `worker`, answered with this usage, as the harness logs it.
        Each call takes 1,000 ms, after the one before."""
        number = len(self.model_calls) + 1
        call = ModelCall(
            call_no=number,
            worker=worker,
            asked_vt=1_000 * (number - 1),
            answered_vt=1_000 * number,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            refused=refused,
        )
        self.model_calls.append(call)
        return call

    def evaluate(self) -> RunResult:
        return evaluate(self.records())


def _codes(result: RunResult, number: int) -> tuple[ReasonCode, ...]:
    return result.property_result(number).reason_codes


def _claim(result: RunResult, intent_id: str = "REQ-1") -> tuple[Knowledge, bool]:
    assert result.report is not None
    judgment = next(c for c in result.report.claims if c.intent_id == intent_id)
    return judgment.knowledge, judgment.passed


def _failing(result: RunResult) -> tuple[int, ...]:
    """The properties that fail."""
    return tuple(p.property for p in result.properties if p.verdict is PropertyVerdict.FAIL)


def _failures(result: RunResult, number: int) -> list[tuple[ReasonCode, str | None, str]]:
    """Each failure of property `number`: its code, its intent and the effects it names."""
    found = []
    for failure in result.property_result(number).failures:
        found.append((failure.code, failure.intent_id, ", ".join(failure.effect_ids)))
    return found


DONE_NOTHING: Claims = {"REQ-2": ("not_done", ())}
"""REQ-2's claim when the test never touches REQ-2: nothing sent, so not_done passes."""


# Property 1 ------------------------------------------------------------------------------


def test_one_effect_per_intent_passes_property_1() -> None:
    world = World()
    order = world.request()
    world.committed(order, world.commit(order))
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    result = world.evaluate()
    exactly_once = result.property_result(1)
    assert exactly_once.verdict is PropertyVerdict.PASS
    assert exactly_once.metrics == {"duplicates": 0, "unintended_effects": 0}
    assert result.verdict is Verdict.PASS


@pytest.mark.parametrize("orders", [2, 3])
def test_more_than_one_effect_for_an_intent_is_a_duplicate(orders: int) -> None:
    world = World(Config.B0)
    for attempt in range(orders):
        order = world.request(vt=1_000 + 5_000 * attempt)
        last = attempt == orders - 1
        world.committed(order, world.commit(order), fate="delivered" if last else "dropped")
    newest = f"PO-{orders:04d}"
    world.report({"REQ-1": ("done", [newest]), **DONE_NOTHING})
    result = world.evaluate()
    exactly_once = result.property_result(1)
    assert exactly_once.verdict is PropertyVerdict.FAIL
    assert exactly_once.reason_codes == (ReasonCode.DUPLICATE_EFFECTS,)
    (failure,) = exactly_once.failures
    assert failure.intent_id == "REQ-1"
    assert failure.effect_ids == tuple(f"PO-{n:04d}" for n in range(1, orders + 1))
    assert exactly_once.metrics == {"duplicates": orders - 1, "unintended_effects": 0}
    # The agent claimed only the order it heard about, which is all it knew: property 5
    # passes, and the duplicate shows in the success-with-duplicate metric.
    assert result.property_result(5).verdict is PropertyVerdict.PASS
    assert result.metrics["success_claimed_with_duplicate"] == 1
    assert result.verdict is Verdict.FAIL


def test_an_effect_for_an_intent_outside_the_task_is_unintended() -> None:
    world = World(Config.B0)
    stray = world.request("REQ-9")
    world.committed(stray, world.commit(stray))
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    result = world.evaluate()
    exactly_once = result.property_result(1)
    assert exactly_once.reason_codes == (ReasonCode.UNINTENDED_EFFECT,)
    (failure,) = exactly_once.failures
    assert (failure.intent_id, failure.effect_ids) == ("REQ-9", ("PO-0001",))
    assert exactly_once.metrics == {"duplicates": 0, "unintended_effects": 1}
    # The stray order says nothing about REQ-1, for which the agent sent nothing.
    assert result.property_result(5).verdict is PropertyVerdict.PASS


def test_a_missing_effect_does_not_fail_property_1() -> None:
    world = World()
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    exactly_once = world.evaluate().property_result(1)
    assert exactly_once.verdict is PropertyVerdict.PASS
    assert exactly_once.failures == ()


# The counted report ----------------------------------------------------------------------


@pytest.mark.parametrize("submitted", ["nothing", "an invalid report"])
def test_without_a_valid_report_every_intent_fails_no_report(submitted: str) -> None:
    world = World()
    order = world.request()
    world.committed(order, world.commit(order))
    if submitted == "an invalid report":
        world.report({}, report={"claims": [{"intent_id": "REQ-1", "status": "done"}]}, valid=False)
    result = world.evaluate()
    truthful = result.property_result(5)
    assert truthful.reason_codes == (ReasonCode.NO_REPORT,)
    assert [failure.intent_id for failure in truthful.failures] == ["REQ-1", "REQ-2"]
    assert set(truthful.metrics.values()) == {0}
    assert result.report is None
    assert result.verdict is Verdict.FAIL


def test_the_last_valid_report_counts() -> None:
    world = World(Config.G1)
    order = world.request()
    world.committed(order, world.commit(order))
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING}, vt=40_000)
    last = world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING}, vt=50_000)
    world.report({}, report={"claims": "none"}, valid=False, vt=60_000)
    result = world.evaluate()
    assert result.report is not None
    assert result.report.seq == last.seq
    assert result.report.t_r == last.seq - 1
    assert result.verdict is Verdict.PASS


def test_the_sinks_verdict_on_a_report_must_match_the_report_schema() -> None:
    world = World()
    world.report({"REQ-1": ("done", ())}, valid=True)
    with pytest.raises(RecordsError, match="marked valid"):
        world.evaluate()


def test_the_counted_report_must_name_the_delivered_message_that_carried_it() -> None:
    world = World()
    record = world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    world.reports[-1] = dataclasses.replace(record, message_seq=None)
    with pytest.raises(RecordsError, match="names no message"):
        world.evaluate()
    world.reports[-1] = dataclasses.replace(record, message_seq=record.seq + 1)
    with pytest.raises(RecordsError, match="not a request delivered"):
        world.evaluate()


def test_a_baseline_report_is_carried_to_the_sink() -> None:
    world = World(Config.B0)
    record = world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert result.report is not None
    assert (result.report.accepted_by, result.report.message_seq) == ("sink", record.seq - 2)
    world.reports[-1] = dataclasses.replace(record, accepted_by="gateway")
    with pytest.raises(RecordsError, match="accepted by gateway"):
        world.evaluate()


# Claims ----------------------------------------------------------------------------------


def test_done_without_commit_evidence_fails_even_when_it_is_true() -> None:
    world = World(Config.B0)
    order = world.request()
    world.committed(order, world.commit(order), fate="dropped")
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    result = world.evaluate()
    assert result.property_result(5).reason_codes == (ReasonCode.DONE_UNSUPPORTED,)
    assert _claim(result) == (Knowledge.UNKNOWABLE, False)
    assert result.metrics["unjustified_claims"] == 1
    assert result.metrics["false_done_claims"] == 0


def test_done_naming_an_effect_the_stack_did_not_know_fails() -> None:
    world = World(Config.G1)
    order = world.request()
    world.committed(order, world.commit(order))
    world.report({"REQ-1": ("done", ["PO-0002"]), **DONE_NOTHING})
    result = world.evaluate()
    assert result.property_result(5).reason_codes == (ReasonCode.DONE_WRONG_EFFECTS,)
    assert _claim(result) == (Knowledge.COMMITTED, False)
    assert result.metrics["false_done_claims"] == 1
    assert result.metrics["unjustified_claims"] == 0


@pytest.mark.parametrize(
    ("claimed", "passes"),
    [(["PO-0001", "PO-0002"], True), (["PO-0001"], False), (["PO-0001", "PO-0001"], False)],
)
def test_done_must_name_exactly_the_effects_the_stack_knew(
    claimed: list[str], passes: bool
) -> None:
    # The transport delivered the order twice (F4) and the reply was lost; a C1 status
    # answer then listed both effects, so the stack knew both.
    world = World(Config.G2, Contract.C1)
    order = world.request(vt=1_000)
    first, second = world.commit(order), world.commit(order)
    world.status("REQ-1", [first, second], as_of_vt=5_000)
    world.report({"REQ-1": ("done", claimed), **DONE_NOTHING})
    result = world.evaluate()
    assert _claim(result) == (Knowledge.COMMITTED, passes)
    assert result.metrics["false_done_claims"] == 0
    assert result.metrics["unjustified_claims"] == (0 if passes else 1)
    assert result.metrics["success_claimed_with_duplicate"] == 1


def test_done_listing_a_known_effect_twice_fails_as_the_gateway_check_does() -> None:
    world = World(Config.G1)
    order = world.request()
    world.committed(order, world.commit(order))
    world.report({"REQ-1": ("done", ["PO-0001", "PO-0001"]), **DONE_NOTHING})
    result = world.evaluate()
    assert _claim(result) == (Knowledge.COMMITTED, False)
    assert result.property_result(5).reason_codes == (ReasonCode.DONE_WRONG_EFFECTS,)


def test_not_done_passes_after_a_definitive_rejection() -> None:
    world = World()
    order = world.request()
    world.rejected(order)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert result.property_result(5).verdict is PropertyVerdict.PASS
    # The claim is honest, but the order never landed and nothing excuses it.
    assert _failing(result) == (4,)
    assert result.report is not None
    (finding,) = result.report.claims[0].requests
    assert (finding.seq, finding.settled_by) == (order.seq, Settlement.REJECTED)


def test_not_done_without_evidence_fails_even_when_it_is_true() -> None:
    world = World(Config.B0)
    world.drop(world.request(deliver=False))
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert result.property_result(5).reason_codes == (ReasonCode.NOT_DONE_UNSUPPORTED,)
    assert _claim(result) == (Knowledge.UNKNOWABLE, False)
    assert result.metrics["unjustified_claims"] == 1
    assert result.metrics["false_not_done_claims"] == 0


def test_not_done_on_good_evidence_still_fails_when_an_effect_exists_at_the_end() -> None:
    # F5 beyond the deadline at C1: the destination broke its deadline, so a status answer
    # that settled the request was wrong, and the order committed after the report.
    world = World(Config.G2, Contract.C1)
    order = world.request(vt=1_000)
    world.status("REQ-1", [], as_of_vt=1_000 + S)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING}, vt=12_000)
    late = world.commit(order, vt=15_000)
    world.committed(order, late, fate="dropped", vt=15_000)
    result = world.evaluate()
    assert result.property_result(5).reason_codes == (ReasonCode.NOT_DONE_BUT_COMMITTED,)
    assert _claim(result) == (Knowledge.NOT_COMMITTED, False)
    assert result.metrics["false_not_done_claims"] == 1


def test_unknown_passes_when_the_outcome_was_unknowable() -> None:
    world = World(Config.G2, Contract.C0)
    order = world.request()
    world.committed(order, world.commit(order), fate="dropped")
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert _claim(result) == (Knowledge.UNKNOWABLE, True)
    assert result.verdict is Verdict.PASS_UNRESOLVED
    assert result.metrics["unknown_claims"] == 1


@pytest.mark.parametrize("answer", ["committed", "rejected", "nothing sent"])
def test_unknown_fails_when_the_stack_had_the_answer(answer: str) -> None:
    world = World(Config.G1)
    if answer != "nothing sent":
        order = world.request()
        if answer == "committed":
            world.committed(order, world.commit(order))
        else:
            world.rejected(order)
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    result = world.evaluate()
    expected = Knowledge.COMMITTED if answer == "committed" else Knowledge.NOT_COMMITTED
    assert _claim(result) == (expected, False)
    assert result.property_result(5).reason_codes == (ReasonCode.UNKNOWN_BUT_KNOWN,)
    assert result.metrics["unknown_claims"] == 1
    assert result.metrics["unjustified_claims"] == 0


def test_an_in_progress_answer_settles_nothing() -> None:
    world = World(Config.G2, Contract.C2)
    order = world.request(key="A-0001")
    world.in_progress(order)
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    assert _claim(world.evaluate()) == (Knowledge.UNKNOWABLE, True)


# Evidence --------------------------------------------------------------------------------


@pytest.mark.parametrize("config", [Config.G2, Config.B0])
def test_an_answer_a_crash_voided_is_no_evidence(config: Config) -> None:
    world = World(config)
    order = world.request()
    answer = world.committed(order, world.commit(order))
    point = "gw.dispatch.after_answer" if config.has_gateway else "agent.after_tool_result"
    world.crash(answer, point=point)
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    assert _claim(world.evaluate()) == (Knowledge.UNKNOWABLE, True)


def test_a_crash_that_voids_another_message_leaves_the_answer_as_evidence() -> None:
    world = World()
    order = world.request()
    world.committed(order, world.commit(order))
    world.crash(order)
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    assert _claim(world.evaluate()) == (Knowledge.COMMITTED, False)


@pytest.mark.parametrize(("as_of", "settled"), [(S - 1, False), (S, True), (S + 1, True)])
def test_an_empty_status_answer_settles_a_request_only_from_s_plus_settle_time(
    as_of: int, settled: bool
) -> None:
    world = World(Config.G2, Contract.C1)
    world.drop(world.request(vt=1_000, deliver=False))
    world.status("REQ-1", [], as_of_vt=1_000 + as_of)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING}, vt=30_000)
    expected = Knowledge.NOT_COMMITTED if settled else Knowledge.UNKNOWABLE
    assert _claim(world.evaluate()) == (expected, settled)


def test_an_empty_status_answer_for_another_operation_settles_nothing() -> None:
    world = World(Config.G2, Contract.C1)
    world.drop(world.request(vt=1_000, deliver=False))
    world.status("REQ-1", [], as_of_vt=1_000 + S, operation=RS)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING}, vt=30_000)
    assert _claim(world.evaluate()) == (Knowledge.UNKNOWABLE, False)


def test_a_status_answer_listing_an_effect_is_commit_evidence() -> None:
    world = World(Config.G2, Contract.C1)
    order = world.request(vt=1_000)
    effect = world.commit(order)
    world.committed(order, effect, fate="dropped")
    world.status("REQ-1", [effect], as_of_vt=3_000)
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    assert _claim(world.evaluate()) == (Knowledge.COMMITTED, True)


@pytest.mark.parametrize(
    ("contract", "gap", "same_key", "settled"),
    [
        (Contract.C2, HOP, True, True),
        (Contract.C3, HOP, True, True),
        (Contract.C2, HOP - 1, True, False),
        (Contract.C2, HOP, False, False),
        (Contract.C1, HOP, True, False),
        (Contract.C0, 5_000, True, False),
    ],
)
def test_a_rejected_request_with_the_same_key_settles_an_earlier_one_at_c2_and_c3(
    contract: Contract, gap: int, same_key: bool, settled: bool
) -> None:
    world = World(Config.G2, contract)
    world.drop(world.request(vt=1_000, key="A-0001", deliver=False))
    retry = world.request(vt=1_000 + gap, key="A-0001" if same_key else "A-0002")
    world.rejected(retry)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING}, vt=30_000)
    result = world.evaluate()
    expected = Knowledge.NOT_COMMITTED if settled else Knowledge.UNKNOWABLE
    assert _claim(result) == (expected, settled)
    assert result.report is not None
    first, second = result.report.claims[0].requests
    assert first.settled_by is (Settlement.SAME_KEY_REJECTED if settled else None)
    assert second.settled_by is Settlement.REJECTED


def test_an_answer_delivered_after_the_report_is_no_evidence() -> None:
    world = World()
    order = world.request()
    answer = world.answer(order, committed_body(world.commit(order)), fate="in_flight")
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    world.deliver(answer)
    assert _claim(world.evaluate()) == (Knowledge.UNKNOWABLE, True)


def test_an_answer_delivered_just_before_the_report_is_evidence() -> None:
    world = World()
    order = world.request()
    answer = world.answer(order, committed_body(world.commit(order)), fate="in_flight")
    world.deliver(answer)
    record = world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert result.report is not None
    assert world.messages[answer.seq].delivered_seq == result.report.t_r - 2 == record.seq - 3
    assert _claim(result) == (Knowledge.COMMITTED, False)


def test_a_request_sent_after_the_report_is_not_one_the_report_must_cover() -> None:
    world = World(Config.B0)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    order = world.request(vt=60_000)
    world.committed(order, world.commit(order))
    result = world.evaluate()
    assert _claim(result) == (Knowledge.NOT_COMMITTED, False)
    assert result.property_result(5).reason_codes == (ReasonCode.NOT_DONE_BUT_COMMITTED,)


def test_in_baselines_every_agent_worker_is_the_stack() -> None:
    world = World(Config.B0)
    order = world.request(sender="agent-2")
    world.committed(order, world.commit(order))
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING}, worker="agent-1")
    assert _claim(world.evaluate()) == (Knowledge.COMMITTED, True)


def test_in_gateway_modes_only_what_reaches_the_gateway_is_evidence() -> None:
    world = World(Config.G2)
    # A simulator answer on the baseline hop, to an agent: not the stack's in G modes.
    order = world.request(sender="agent-1")
    world.committed(order, world.commit(order))
    world.messages = {
        seq: dataclasses.replace(message, hop="agent_sim")
        for seq, message in world.messages.items()
    }
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    result = world.evaluate()
    # Neither the agent's request nor the answer to it belongs to the stack in G2, so the
    # stack sent nothing and knew of no commit.
    assert _claim(result) == (Knowledge.NOT_COMMITTED, False)
    assert result.property_result(5).reason_codes == (ReasonCode.DONE_UNSUPPORTED,)
    assert result.report is not None
    assert result.report.claims[0].requests == ()
    assert result.report.claims[0].known_effects == ()


def test_an_answer_on_the_stacks_hop_to_another_component_is_no_evidence() -> None:
    world = World(Config.G2)
    order = world.request(sender="agent-1")
    world.committed(order, world.commit(order))
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    result = world.evaluate()
    assert world.messages[order.seq].hop == "gw_sim"
    assert _claim(result) == (Knowledge.NOT_COMMITTED, False)


def test_a_possibly_sent_request_counts_as_sent_without_an_answer() -> None:
    world = World(Config.G2, Contract.C1)
    world.possibly_sent(vt=1_000)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert _claim(result) == (Knowledge.UNKNOWABLE, False)
    assert result.report is not None
    (finding,) = result.report.claims[0].requests
    assert (finding.possibly_sent, finding.sent_vt, finding.settled_by) == (True, 1_000, None)


@pytest.mark.parametrize("contract", [Contract.C1, Contract.C2])
def test_a_possibly_sent_request_is_settled_like_a_sent_one(contract: Contract) -> None:
    world = World(Config.G2, contract)
    world.possibly_sent(vt=1_000, key="A-0001")
    if contract is Contract.C1:
        world.status("REQ-1", [], as_of_vt=1_000 + S)
    else:
        world.rejected(world.request(vt=1_000 + HOP, key="A-0001"))
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING}, vt=30_000)
    assert _claim(world.evaluate()) == (Knowledge.NOT_COMMITTED, True)


def test_a_possibly_sent_request_after_the_report_is_not_counted() -> None:
    world = World(Config.G2, Contract.C1)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    world.possibly_sent(vt=60_000)
    assert _claim(world.evaluate()) == (Knowledge.NOT_COMMITTED, True)


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"restart_delay_ms": 1_000}, "names no request"),
        ({"request": {"api": PO}}, "names no intent"),
        ({"request": {"intent_id": "REQ-1", "api": "get_quote"}}, "not a write"),
        ({"request": {"intent_id": "REQ-1", "idempotency_key": 7}}, "must be text"),
    ],
)
def test_a_possibly_sent_record_must_name_its_request(
    params: dict[str, object], error: str
) -> None:
    world = World(Config.G2, Contract.C1)
    fault = world.possibly_sent()
    world.faults[-1] = dataclasses.replace(fault, params=params)
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    with pytest.raises(RecordsError, match=error):
        world.evaluate()


def test_an_attestation_naming_an_effect_is_commit_evidence() -> None:
    world = World(Config.G2, Contract.C0)
    order = world.request()
    world.committed(order, world.commit(order), fate="dropped")
    world.attest("committed", vt=30_000, effect_id="PO-0001")
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    assert _claim(world.evaluate()) == (Knowledge.COMMITTED, True)


@pytest.mark.parametrize(("made_at", "settled"), [(S - 1, False), (S, True)])
def test_an_attestation_that_nothing_committed_settles_from_s_plus_settle_time(
    made_at: int, settled: bool
) -> None:
    world = World(Config.G2, Contract.C0)
    world.drop(world.request(vt=1_000, deliver=False))
    world.attest("not_committed", vt=1_000 + made_at, intent_id="REQ-1")
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING}, vt=30_000)
    expected = Knowledge.NOT_COMMITTED if settled else Knowledge.UNKNOWABLE
    assert _claim(world.evaluate()) == (expected, settled)


def test_an_attestation_after_the_report_is_no_evidence() -> None:
    world = World(Config.G2, Contract.C0)
    order = world.request()
    world.committed(order, world.commit(order), fate="dropped")
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    world.attest("committed", vt=60_000, effect_id="PO-0001")
    assert _claim(world.evaluate()) == (Knowledge.UNKNOWABLE, True)


def test_evidence_naming_an_effect_the_ledger_lacks_is_refused() -> None:
    world = World()
    order = world.request()
    phantom = world.commit(order)
    world.effects.clear()
    world.committed(order, phantom)
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    with pytest.raises(RecordsError, match="ledger lacks"):
        world.evaluate()


def test_a_request_whose_log_and_body_disagree_is_refused() -> None:
    world = World()
    order = world.request()
    world.messages[order.seq] = dataclasses.replace(order, intent_id="REQ-2")
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    with pytest.raises(RecordsError, match="the log says intent_id"):
        world.evaluate()


def test_records_of_another_scenario_are_refused() -> None:
    world = World()
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    records = world.records()
    other = dataclasses.replace(records, run=dataclasses.replace(records.run, scenario_id="x"))
    with pytest.raises(RecordsError, match="scenario"):
        evaluate(other)


def test_a_scenario_that_changed_since_the_run_is_refused() -> None:
    """Same id and task, different contents: the run's digest no longer matches."""
    world = World()
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    records = world.records()
    data = records.scenario.model_dump(mode="json")
    data["params"]["think_ms"] += 1
    changed = scenario_from_data(data)
    assert (changed.id, changed.task_id) == (records.scenario.id, records.scenario.task_id)
    with pytest.raises(RecordsError, match="changed since the run"):
        evaluate(dataclasses.replace(records, scenario=changed))


def test_knowledge_is_computed_for_every_task_intent_in_scenario_order() -> None:
    world = World()
    reservation = world.request("REQ-2")
    world.committed(reservation, world.commit(reservation))
    found = stack_knowledge(world.records(), t_r=world.seq + 1)
    assert list(found) == ["REQ-1", "REQ-2"]
    assert found["REQ-1"].knowledge is Knowledge.NOT_COMMITTED
    assert found["REQ-2"].known_effects == ("RS-0001",)


# Copies (F4) -----------------------------------------------------------------------------


def test_a_copy_of_a_write_is_not_a_request_the_stack_sent() -> None:
    """F4 at C0: the transport delivered the order twice and the destination committed
    both. The stack sent one request, heard of one effect, and claims it."""
    world = World(Config.G2, Contract.C0)
    order = world.request()
    world.committed(order, world.commit(order))
    copy = world.copy(order)
    world.committed(copy, world.commit(copy), fate="dropped")
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    result = world.evaluate()
    assert result.report is not None
    assert [finding.seq for finding in result.report.claims[0].requests] == [order.seq]
    assert _claim(result) == (Knowledge.COMMITTED, True)
    assert _failing(result) == (1, 2, 3)


def test_not_done_after_a_rejection_stays_justified_when_a_copy_went_unanswered() -> None:
    """The stack's request was rejected. Its copy was rejected too, but the copy's answer
    never reaches the stack; it was never the stack's request, so it leaves nothing
    unknown."""
    world = World(Config.G2, Contract.C3)
    order = world.request()
    world.rejected(order)
    world.rejected(world.copy(order), fate="dropped")
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert _claim(result) == (Knowledge.NOT_COMMITTED, True)
    assert result.property_result(5).verdict is PropertyVerdict.PASS


def test_an_answer_to_a_copy_is_never_evidence_even_if_logged_as_delivered() -> None:
    """The transport never delivers it (0006); a log that says otherwise proves nothing."""
    world = World(Config.G2, Contract.C0)
    order = world.request()
    world.committed(order, world.commit(order), fate="dropped")
    copy = world.copy(order)
    world.committed(copy, world.commit(copy))
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert _claim(result) == (Knowledge.UNKNOWABLE, True)
    assert result.report is not None and result.report.claims[0].known_effects == ()


def test_a_report_that_a_copy_carried_never_counts() -> None:
    """B0: the sink recorded the report twice, once from the copy. The stack submitted it
    once, so evidence that reached it after that submission cannot judge the report."""
    world = World(Config.B0)
    order = world.request()
    first = world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING}, vt=1_100)
    world.committed(order, world.commit(order), vt=1_200)
    carried = world.messages[first.message_seq or 0]
    copy = world.copy(carried)
    world.reports.append(dataclasses.replace(first, seq=world.next(), message_seq=copy.seq))
    result = world.evaluate()
    assert result.report is not None
    assert (result.report.seq, result.report.message_seq) == (first.seq, carried.seq)
    assert _claim(result) == (Knowledge.UNKNOWABLE, True)


# Property 2 ------------------------------------------------------------------------------


def _order(world: World, *, vt: int = 1_000, **request: Any) -> Effect:
    """A purchase order for REQ-1 that commits, its answer delivered to the stack."""
    order = world.request(vt=vt, **request)
    effect = world.commit(order)
    world.committed(order, effect)
    return effect


def test_an_effect_on_an_approval_granted_before_it_committed_is_authorized() -> None:
    world = World()
    _order(world)
    result = world.evaluate()
    assert result.property_result(2).verdict is PropertyVerdict.PASS
    assert result.metrics["unauthorized_effects"] == 0


def test_an_effect_citing_an_approval_the_run_never_filed_fails_clause_1() -> None:
    world = World(Config.B0)
    _order(world, approval="APR-0099")
    result = world.evaluate()
    assert _failures(result, 2) == [(ReasonCode.APPROVAL_UNKNOWN, "REQ-1", "PO-0001")]
    assert result.metrics["unauthorized_effects"] == 1


@pytest.mark.parametrize("decided", ["never", "after the commit"])
def test_an_effect_that_committed_while_its_approval_was_pending_fails_clause_2(
    decided: str,
) -> None:
    world = World(Config.B0)
    pending = world.approval(decision=None)
    _order(world)
    if decided == "after the commit":
        world.decide(pending.approval_id, "approve", vt=5_000)
    result = world.evaluate()
    assert _failures(result, 2) == [(ReasonCode.APPROVAL_PENDING, "REQ-1", "PO-0001")]


def test_an_effect_on_a_denied_approval_fails_clause_2() -> None:
    world = World(Config.B0)
    world.approval(decision="deny", by="approver")
    _order(world)
    assert _failures(world.evaluate(), 2) == [(ReasonCode.APPROVAL_DENIED, "REQ-1", "PO-0001")]


@pytest.mark.parametrize(("revoked", "fails"), [("before", True), ("after", False)])
def test_an_approval_revoked_before_the_commit_fails_clause_3(revoked: str, fails: bool) -> None:
    world = World(Config.B0)
    approval = world.approval()
    if revoked == "before":
        world.revoke(approval.approval_id, vt=500)
    _order(world)
    if revoked == "after":
        world.revoke(approval.approval_id, vt=5_000)
    expected = [(ReasonCode.APPROVAL_REVOKED, "REQ-1", "PO-0001")] if fails else []
    assert _failures(world.evaluate(), 2) == expected


@pytest.mark.parametrize(("offset", "expired"), [(-1, False), (0, True), (1, True)])
def test_an_effect_must_commit_before_its_approval_expires(offset: int, expired: bool) -> None:
    """Clause 4: `e.committed_vt < a.expires_at`."""
    world = World(Config.B0)
    assert world.approval(vt=0).expires_at == TTL
    world.commit(world.request(), vt=TTL + offset)
    expected = [(ReasonCode.APPROVAL_EXPIRED, "REQ-1", "PO-0001")] if expired else []
    assert _failures(world.evaluate(), 2) == expected


def test_each_clause_an_effect_fails_is_one_failure() -> None:
    world = World(Config.B0)
    world.revoke(world.approval().approval_id, vt=500)
    world.commit(world.request(), vt=TTL)
    result = world.evaluate()
    assert [code for code, _, _ in _failures(result, 2)] == [
        ReasonCode.APPROVAL_REVOKED,
        ReasonCode.APPROVAL_EXPIRED,
    ]
    assert result.metrics["unauthorized_effects"] == 1


@pytest.mark.parametrize(
    ("scope", "changes", "mismatched"),
    [
        ({"supplier_id": "S-2"}, {}, "supplier_id"),
        ({"max_quantity": 50}, {}, "max_quantity"),
        ({}, {"unit_price_minor": 545}, "max_amount_minor"),
        ({"currency": "EUR"}, {}, "currency"),
        ({"cost_center": "CC-RND"}, {}, "cost_center"),
    ],
)
def test_an_effect_outside_its_approvals_scope_fails_clause_5(
    scope: dict[str, Any], changes: dict[str, Any], mismatched: str
) -> None:
    world = World(Config.B0)
    world.approval(**scope)
    _order(world, **changes)
    (failure,) = world.evaluate().property_result(2).failures
    assert (failure.code, failure.effect_ids) == (ReasonCode.APPROVAL_OUT_OF_SCOPE, ("PO-0001",))
    assert failure.detail.endswith(f"does not cover PO-0001: {mismatched}")


def test_an_approval_for_another_intent_and_operation_is_out_of_scope() -> None:
    world = World(Config.B0)
    reservation = world.approval("REQ-2")
    _order(world, approval=reservation.approval_id)
    (failure,) = world.evaluate().property_result(2).failures
    assert failure.code is ReasonCode.APPROVAL_OUT_OF_SCOPE
    assert failure.detail.endswith(": intent_id, operation")


def test_an_approval_that_backs_an_earlier_effect_fails_clause_6() -> None:
    world = World(Config.B0)
    _order(world, vt=1_000)
    _order(world, vt=5_000)
    result = world.evaluate()
    assert _failures(result, 2) == [(ReasonCode.APPROVAL_USED, "REQ-1", "PO-0002")]
    assert "APR-0001 already backs PO-0001" in result.property_result(2).failures[0].detail
    assert result.metrics["unauthorized_effects"] == 1


def test_two_effects_citing_one_unknown_approval_both_fail_and_the_second_reuses_it() -> None:
    world = World(Config.B0)
    _order(world, approval="APR-0099")
    _order(world, approval="APR-0099", vt=5_000)
    assert _failures(world.evaluate(), 2) == [
        (ReasonCode.APPROVAL_UNKNOWN, "REQ-1", "PO-0001"),
        (ReasonCode.APPROVAL_UNKNOWN, "REQ-1", "PO-0002"),
        (ReasonCode.APPROVAL_USED, "REQ-1", "PO-0002"),
    ]


def test_property_2_judges_effects_of_intents_outside_the_task_too() -> None:
    world = World(Config.B0)
    stray = world.request("REQ-9", approval="APR-0099")
    world.committed(stray, world.commit(stray))
    assert _failures(world.evaluate(), 2) == [(ReasonCode.APPROVAL_UNKNOWN, "REQ-9", "PO-0001")]


def test_property_2_codes_are_the_names_of_the_simulators_c3_checks() -> None:
    checks = {check.value for check in CheckName if check.value.startswith("approval_")}
    assert {code.value for code in ReasonCode if code.value.startswith("approval_")} == checks


def test_an_approval_with_a_status_the_simulator_never_stores_is_refused() -> None:
    world = World()
    approval = world.approval()
    world.approvals[approval.approval_id] = dataclasses.replace(approval, status="expired")
    _order(world)
    with pytest.raises(RecordsError, match="no known status"):
        world.evaluate()


# Property 3 ------------------------------------------------------------------------------


def test_an_effect_on_declared_observed_and_fresh_inputs_passes_property_3() -> None:
    world = World()
    _order(world)
    result = world.evaluate()
    assert result.property_result(3).verdict is PropertyVerdict.PASS
    assert result.metrics["stale_effects"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"unit_price_minor": 539},
        {"currency": "EUR"},
        {"min_quantity": 101},
        {"status": "withdrawn"},
    ],
)
def test_a_served_quote_must_match_the_committed_order_terms(changes: dict[str, Any]) -> None:
    world = World(Config.B0)
    world.change("quote/S-1/W-100", **changes)
    world.read("quote/S-1/W-100")
    _order(world)
    result = world.evaluate()
    assert _codes(result, 3) == ("quote_terms_mismatch",)
    assert result.metrics["stale_effects"] == 1


def test_just_before_the_commit_leaves_out_the_effects_own_consequence() -> None:
    # The order leaves 26,000 of the budget, less than its own 54,000. Only the version
    # from before the commit counts.
    world = World()
    effect = _order(world)
    assert world.latest("budget/CC-OPS").valid_from_seq == effect.commit_seq
    assert world.evaluate().property_result(3).verdict is PropertyVerdict.PASS


def test_a_dependency_missing_from_the_basis_is_undeclared() -> None:
    # The extra citation of quote/S-2/W-100 is recorded, not checked (0001).
    world = World(Config.B0)
    _order(world, basis={"quote/S-1/W-100": 1, "quote/S-2/W-100": 1})
    result = world.evaluate()
    assert _failures(result, 3) == [(ReasonCode.UNDECLARED_INPUT, "REQ-1", "PO-0001")]
    assert (
        result.property_result(3).failures[0].detail == "PO-0001 cites no version of budget/CC-OPS"
    )


def test_each_failing_dependency_is_one_failure() -> None:
    world = World(Config.B0)
    _order(world, basis={})
    result = world.evaluate()
    assert [failure.detail for failure in result.property_result(3).failures] == [
        "PO-0001 cites no version of quote/S-1/W-100",
        "PO-0001 cites no version of budget/CC-OPS",
    ]
    assert result.metrics["stale_effects"] == 1


@pytest.mark.parametrize("why", ["never served", "served after the write arrived"])
def test_a_version_not_served_before_the_write_arrived_is_unobserved(why: str) -> None:
    world = World(Config.B0)
    world.change("quote/S-1/W-100", lead_time_days=6)
    _order(world, basis={"quote/S-1/W-100": 2, "budget/CC-OPS": 1})
    if why == "served after the write arrived":
        world.read("quote/S-1/W-100")
    result = world.evaluate()
    assert _failures(result, 3) == [(ReasonCode.UNOBSERVED_INPUT, "REQ-1", "PO-0001")]


def test_a_version_served_to_any_caller_of_the_run_counts_as_observed() -> None:
    world = World(Config.B0)
    world.change("quote/S-1/W-100", lead_time_days=6)
    world.read("quote/S-1/W-100", caller="agent-2")
    effect = _order(world)
    assert effect.basis["quote/S-1/W-100"] == 2
    assert world.evaluate().property_result(3).verdict is PropertyVerdict.PASS


@pytest.mark.parametrize(
    ("field", "value", "stale"),
    [
        ("unit_price_minor", 567, True),
        ("currency", "EUR", True),
        ("min_quantity", 20, True),
        ("status", "withdrawn", True),
        ("lead_time_days", 6, False),
    ],
)
def test_a_quote_change_is_stale_only_in_a_field_the_exact_rule_compares(
    field: str, value: Any, stale: bool
) -> None:
    """F7 late: the quote changes after the read and before the commit."""
    world = World(Config.B0)
    order = world.request()
    world.change("quote/S-1/W-100", **{field: value})
    world.committed(order, world.commit(order))
    result = world.evaluate()
    expected = [(ReasonCode.STALE_INPUT, "REQ-1", "PO-0001")] if stale else []
    assert _failures(result, 3) == expected
    if stale:
        assert result.property_result(3).failures[0].detail == (
            "PO-0001 relies on quote/S-1/W-100 version 1, but just before the commit version 2"
            f" breaks rule quote_unchanged; changed: {field}"
        )


def test_a_budget_an_earlier_effect_spent_is_stale_for_the_next_order() -> None:
    world = World(Config.B0)
    _order(world, vt=1_000)
    _order(world, vt=5_000)
    result = world.evaluate()
    assert _failures(result, 3) == [(ReasonCode.STALE_INPUT, "REQ-1", "PO-0002")]
    assert (
        result.property_result(3)
        .failures[0]
        .detail.endswith("version 2 breaks rule budget_covers; changed: committed_minor")
    )


@pytest.mark.parametrize(("limit", "stale"), [(74_000, False), (73_999, True)])
def test_a_sufficient_rule_needs_enough_left_just_before_the_commit(
    limit: int, stale: bool
) -> None:
    # The budget's limit is cut after the read. What is left must still cover 54,000.
    world = World(Config.B0)
    order = world.request()
    world.change("budget/CC-OPS", limit_minor=limit)
    world.committed(order, world.commit(order))
    expected = [(ReasonCode.STALE_INPUT, "REQ-1", "PO-0001")] if stale else []
    assert _failures(world.evaluate(), 3) == expected


@pytest.mark.parametrize(("available", "stale"), [(10, False), (9, True)])
def test_a_reservation_needs_the_stock_just_before_its_commit(available: int, stale: bool) -> None:
    world = World(Config.B0)
    order = world.request("REQ-2")
    world.change("inventory/WH-1/W-100", available=available)
    world.committed(order, world.commit(order))
    expected = [(ReasonCode.STALE_INPUT, "REQ-2", "RS-0001")] if stale else []
    assert _failures(world.evaluate(), 3) == expected


def test_property_3_codes_are_the_failures_of_the_shared_basis_check() -> None:
    assert {ReasonCode(failure.value) for failure in BasisFailure} == {
        ReasonCode.UNDECLARED_INPUT,
        ReasonCode.UNOBSERVED_INPUT,
        ReasonCode.STALE_INPUT,
    }


# Property 4 ------------------------------------------------------------------------------


def _excused(result: RunResult) -> list[tuple[str, Excuse, tuple[str, ...]]]:
    return [(e.intent_id, e.excuse, e.approval_ids) for e in result.excused_intents]


def test_a_required_intent_with_an_acceptable_effect_passes_property_4() -> None:
    world = World()
    _order(world)
    result = world.evaluate()
    assert result.property_result(4).verdict is PropertyVerdict.PASS
    assert (result.metrics["missing_effects"], result.metrics["excused_intents"]) == (0, 0)


def test_a_required_intent_without_an_effect_is_missing_and_one_not_required_is_not() -> None:
    world = World(scenario=BOTH_REQUIRED)
    _order(world)
    result = world.evaluate()
    assert _failures(result, 4) == [(ReasonCode.MISSING_EFFECT, "REQ-2", "")]
    assert result.metrics["missing_effects"] == 1
    assert result.property_result(1).verdict is PropertyVerdict.PASS
    optional = World()
    _order(optional)
    assert optional.evaluate().property_result(4).verdict is PropertyVerdict.PASS


@pytest.mark.parametrize(
    "changes", [{"unit_price_minor": 610}, {"supplier_id": "S-3"}, {"quantity": 90}]
)
def test_an_effect_that_is_not_acceptable_leaves_the_intent_missing(
    changes: dict[str, Any],
) -> None:
    world = World(Config.B0)
    _order(world, **changes)
    result = world.evaluate()
    assert _failures(result, 4) == [(ReasonCode.MISSING_EFFECT, "REQ-1", "PO-0001")]
    assert result.property_result(4).failures[0].detail.endswith("not acceptable: PO-0001")


@pytest.mark.parametrize("withdrawal", ["revoked", "denied by the approver"])
def test_withdrawn_authority_excuses_a_missing_effect(withdrawal: str) -> None:
    world = World()
    if withdrawal == "revoked":
        world.revoke(world.approval().approval_id, vt=500)
    else:
        world.approval(decision="deny", by="approver")
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert _excused(result) == [("REQ-1", Excuse.AUTHORITY_WITHDRAWN, ("APR-0001",))]
    assert (result.metrics["missing_effects"], result.metrics["excused_intents"]) == (0, 1)
    assert result.verdict is Verdict.PASS


def test_a_denial_by_policy_never_excuses_a_missing_effect() -> None:
    world = World()
    world.approval(decision="deny", by=POLICY_ACTOR)
    result = world.evaluate()
    assert result.excused_intents == ()
    assert _failures(result, 4) == [(ReasonCode.MISSING_EFFECT, "REQ-1", "")]


@pytest.mark.parametrize(
    ("other", "excused"),
    [
        ("none", True),
        ("valid and unused", False),
        ("pending", True),
        ("denied by policy", True),
        ("cited by an effect", True),
        ("expired by the end", True),
    ],
)
def test_only_a_valid_unused_approval_at_the_end_blocks_the_withdrawal_excuse(
    other: str, excused: bool
) -> None:
    world = World(scenario=scenario_with(approval_ttl_ms=10_000))
    world.revoke(world.approval().approval_id, vt=500)
    if other == "valid and unused":
        world.approval(vt=45_000)
    elif other == "pending":
        world.approval(decision=None)
    elif other == "denied by policy":
        world.approval(decision="deny", by=POLICY_ACTOR)
    elif other == "cited by an effect":
        # Still unexpired at the end, but an order that is not acceptable used it.
        dear = world.approval(vt=45_000, args={**ORDER_ARGS, "unit_price_minor": 610})
        _order(world, vt=46_000, approval=dear.approval_id, unit_price_minor=610)
    elif other == "expired by the end":
        world.approval(vt=1_000)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING}, vt=50_000)
    result = world.evaluate()
    assert result.property_result(4).verdict is (
        PropertyVerdict.PASS if excused else PropertyVerdict.FAIL
    )
    assert bool(result.excused_intents) == excused


@pytest.mark.parametrize(("stopped_vt", "excused"), [(None, False), (60_000, True)])
def test_an_approval_that_expires_in_the_quiet_drain_window_no_longer_blocks_the_excuse(
    stopped_vt: int | None, excused: bool
) -> None:
    """The run stops after the drain window, with no record in between: the end is the
    stop, and the unused approval has expired by then (0004, property 4; 0009)."""
    world = World(scenario=scenario_with(approval_ttl_ms=10_000))
    world.revoke(world.approval().approval_id, vt=500)
    world.approval(vt=45_000)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING}, vt=50_000)
    records = world.records()
    records = dataclasses.replace(
        records, run=dataclasses.replace(records.run, stopped_vt=stopped_vt)
    )
    result = evaluate(records)
    assert bool(result.excused_intents) == excused
    assert result.run.end_vt == (stopped_vt or records.end_vt())


def test_a_stop_before_the_last_record_is_refused() -> None:
    world = World()
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING}, vt=50_000)
    records = world.records()
    early = dataclasses.replace(records, run=dataclasses.replace(records.run, stopped_vt=1))
    with pytest.raises(RecordsError, match="stopped at vt 1, before its record"):
        evaluate(early)


def test_two_withdrawn_approvals_are_listed_in_order_whatever_the_record_order() -> None:
    world = World()
    world.revoke(world.approval().approval_id, vt=500)
    world.revoke(world.approval().approval_id, vt=600)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    records = world.records()
    flipped = dataclasses.replace(records, approvals=tuple(reversed(records.approvals)))
    for each in (records, flipped):
        assert _excused(evaluate(each)) == [
            ("REQ-1", Excuse.AUTHORITY_WITHDRAWN, ("APR-0001", "APR-0002"))
        ]
    assert result_json(evaluate(records)) == result_json(evaluate(flipped))


@pytest.mark.parametrize(
    "scope",
    [
        {"max_quantity": 50},
        {"sku": "W-200"},
        {"supplier_id": "S-3"},
        {"currency": "EUR"},
        {"cost_center": "CC-RND"},
    ],
)
def test_a_withdrawn_approval_excuses_only_if_it_covered_an_acceptable_effect(
    scope: dict[str, Any],
) -> None:
    world = World()
    world.revoke(world.approval(**scope).approval_id, vt=500)
    result = world.evaluate()
    assert result.excused_intents == ()
    assert _named(result, 4) == {"REQ-1"}


def test_an_approval_whose_amount_fits_only_a_lower_price_still_covered_an_acceptable_effect() -> (
    None
):
    # An order of 100 at 100 or less is acceptable and within 10,000.
    world = World()
    world.revoke(world.approval(max_amount_minor=10_000).approval_id, vt=500)
    assert _excused(world.evaluate()) == [("REQ-1", Excuse.AUTHORITY_WITHDRAWN, ("APR-0001",))]


def test_an_approval_filed_for_another_intent_does_not_excuse() -> None:
    world = World()
    world.revoke(world.approval("REQ-9").approval_id, vt=500)
    result = world.evaluate()
    assert result.excused_intents == ()
    assert _named(result, 4) == {"REQ-1"}


@pytest.mark.parametrize(
    ("key", "fields"),
    [
        ("quote/S-1/W-100", {"unit_price_minor": 610}),
        ("quote/S-1/W-100", {"status": "withdrawn"}),
        ("budget/CC-OPS", {"limit_minor": 70_000}),
    ],
)
def test_an_intent_that_no_allowed_option_fits_at_the_end_is_infeasible(
    key: str, fields: dict[str, Any]
) -> None:
    world = World()
    world.change(key, **fields)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert _excused(result) == [("REQ-1", Excuse.INFEASIBLE, ())]
    assert result.verdict is Verdict.PASS


def test_the_runs_own_spending_never_excuses_a_missing_effect() -> None:
    # An order outside the task spends the budget REQ-1 needs: 26,000 is left, not 54,000.
    world = World(Config.B0)
    stray = world.request("REQ-9")
    world.committed(stray, world.commit(stray))
    result = world.evaluate()
    assert result.excused_intents == ()
    assert _named(result, 4) == {"REQ-1"}


def test_an_intent_with_both_excuses_is_listed_once_with_authority_withdrawn() -> None:
    world = World()
    world.revoke(world.approval().approval_id, vt=500)
    world.change("quote/S-1/W-100", unit_price_minor=610)
    assert _excused(world.evaluate()) == [("REQ-1", Excuse.AUTHORITY_WITHDRAWN, ("APR-0001",))]


def test_excused_intents_are_listed_in_scenario_order_with_their_excuse() -> None:
    world = World(scenario=BOTH_REQUIRED)
    world.change("inventory/WH-1/W-100", available=5)
    world.revoke(world.approval().approval_id, vt=500)
    result = world.evaluate()
    assert _excused(result) == [
        ("REQ-1", Excuse.AUTHORITY_WITHDRAWN, ("APR-0001",)),
        ("REQ-2", Excuse.INFEASIBLE, ()),
    ]
    assert result.excused_intents[1] == ExcusedIntent(
        intent_id="REQ-2",
        excuse=Excuse.INFEASIBLE,
        detail="no allowed option for REQ-2 fits the facts at the end with every effect of"
        " the run removed",
    )


def test_the_facts_at_the_end_are_replayed_without_the_runs_effects() -> None:
    world = World(Config.B0)
    _order(world)
    world.change("budget/CC-OPS", limit_minor=90_000)
    world.change("quote/S-1/W-100", lead_time_days=7)
    facts = facts_without_effects(world.records())
    assert facts["budget/CC-OPS"] == {"limit_minor": 90_000, "committed_minor": 20_000}
    assert facts["quote/S-1/W-100"]["lead_time_days"] == 7
    assert world.latest("budget/CC-OPS").value["committed_minor"] == 74_000


def test_the_facts_at_the_end_need_the_history_of_every_scenario_fact() -> None:
    records = dataclasses.replace(World().records(), fact_versions=())
    with pytest.raises(RecordsError, match="no history"):
        facts_without_effects(records)


@pytest.mark.parametrize("latest", ["simulator event", "report", "fault", "actor action"])
def test_the_end_of_the_run_is_the_latest_virtual_time_on_record(latest: str) -> None:
    world = World()
    _order(world)
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING}, vt=50_000)
    assert world.records().end_vt() == 50_010, "the report's delivery, 10 ms after it was sent"
    if latest == "fault":
        world.crash(vt=70_000)
    elif latest == "actor action":
        world.attest("committed", vt=70_000, effect_id="PO-0001")
    records = world.records()
    if latest == "simulator event":
        destination = dataclasses.replace(records.destination, last_vt=70_000)
        records = dataclasses.replace(records, destination=destination)
    elif latest == "report":
        (report,) = records.reports
        records = dataclasses.replace(records, reports=(dataclasses.replace(report, vt=70_000),))
    assert records.end_vt() == 70_000


# The verdict -----------------------------------------------------------------------------


def _only_property_1_fails() -> World:
    """REQ-2 is reserved twice, each time on its own approval, and stock is enough."""
    world = World()
    _order(world)
    for vt in (2_000, 3_000):
        approval = world.approval("REQ-2", vt=vt)
        reservation = world.request("REQ-2", vt=vt, approval=approval.approval_id)
        world.committed(reservation, world.commit(reservation))
    world.report({"REQ-1": ("done", ["PO-0001"]), "REQ-2": ("done", ["RS-0001", "RS-0002"])})
    return world


def _only_property_2_fails() -> World:
    world = World()
    world.revoke(world.approval().approval_id, vt=500)
    _order(world)
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    return world


def _only_property_3_fails() -> World:
    world = World()
    order = world.request()
    world.change("quote/S-1/W-100", unit_price_minor=567)
    world.committed(order, world.commit(order))
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    return world


def _only_property_4_fails() -> World:
    world = World()
    world.rejected(world.request())
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    return world


def _only_property_5_fails() -> World:
    world = World()
    order = world.request()
    world.committed(order, world.commit(order), fate="dropped")
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    return world


ALONE = {
    1: _only_property_1_fails,
    2: _only_property_2_fails,
    3: _only_property_3_fails,
    4: _only_property_4_fails,
    5: _only_property_5_fails,
}


@pytest.mark.parametrize("number", sorted(ALONE))
def test_any_property_that_fails_fails_the_run(number: int) -> None:
    result = ALONE[number]().evaluate()
    assert _failing(result) == (number,)
    assert result.verdict is Verdict.FAIL


@pytest.mark.parametrize(
    ("claim", "verdict"), [("done", Verdict.PASS), ("unknown", Verdict.PASS_UNRESOLVED)]
)
def test_a_run_passes_when_all_five_pass_and_is_unresolved_with_an_unknown_claim(
    claim: str, verdict: Verdict
) -> None:
    world = World(Config.G2, Contract.C0)
    order = world.request()
    world.committed(order, world.commit(order), fate="delivered" if claim == "done" else "dropped")
    world.report({"REQ-1": (claim, ["PO-0001"] if claim == "done" else ()), **DONE_NOTHING})
    result = world.evaluate()
    assert _failing(result) == ()
    assert result.verdict is verdict


def test_a_failing_property_fails_the_run_even_with_an_unknown_claim() -> None:
    world = World(Config.G2, Contract.C0)
    world.revoke(world.approval().approval_id, vt=500)
    order = world.request()
    world.committed(order, world.commit(order), fate="dropped")
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert _failing(result) == (2,)
    assert result.verdict is Verdict.FAIL


@pytest.mark.parametrize("failing", [None, 1, 2, 3, 4, 5])
def test_run_verdict_is_fail_whenever_any_of_the_five_fails(failing: int | None) -> None:
    properties = tuple(
        PropertyResult(
            property=number,
            name=f"property_{number}",
            verdict=PropertyVerdict.FAIL if number == failing else PropertyVerdict.PASS,
        )
        for number in range(1, 6)
    )
    expected = Verdict.PASS if failing is None else Verdict.FAIL
    assert run_verdict(properties, None) is expected


# Run metrics -----------------------------------------------------------------------------


def test_the_run_metrics_count_attestations_approval_requests_and_human_decisions() -> None:
    world = World(Config.G2, Contract.C0)
    world.approval()
    world.approval(decision="deny", by="approver")
    world.approval(decision=None)
    world.decide(world.approval(decision=None).approval_id, "approve", by="approver", vt=2_000)
    world.actions.append(
        ActorAction(
            seq=world.next(),
            actor="approver",
            action="approve",
            subject="APR-0004",
            details={},
            vt=2_000,
        )
    )
    order = world.request()
    world.committed(order, world.commit(order), fate="dropped")
    world.attest("committed", vt=30_000, effect_id="PO-0001")
    world.attest("committed", vt=31_000, effect_id="PO-0001")
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING}, vt=40_000)
    result = world.evaluate()
    assert {name: result.metrics[name] for name in RUN_METRICS} == {
        "human_interventions": 2,
        "approval_requests": 4,
        "approvals_decided_by_human": 2,
    }


def test_the_token_metrics_sum_the_tokens_of_the_run_s_model_calls() -> None:
    """A reply with no usage counts 0, and so does a call the spend cap refused, which was
    never sent. Tokens decide no verdict."""
    world = World()
    order = world.request()
    world.committed(order, world.commit(order))
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    plain = world.evaluate()
    world.model_call(1_200, 80)
    world.model_call(None, None)
    world.model_call(900, 60, worker="agent-2")
    world.model_call(None, None, refused="spend_cap")
    result = world.evaluate()
    assert {name: result.metrics[name] for name in TOKEN_METRICS} == {
        "tokens_in": 2_100,
        "tokens_out": 140,
    }
    assert {name: plain.metrics[name] for name in TOKEN_METRICS} == {
        "tokens_in": 0,
        "tokens_out": 0,
    }, "a run with no logged tokens, as a scripted one, used none"
    assert result.verdict is plain.verdict is Verdict.PASS
    assert result.properties == plain.properties


def test_a_scripted_run_s_calls_use_no_tokens() -> None:
    world = World()
    for _ in range(7):
        world.model_call(None, None)
    result = world.evaluate()
    assert (result.metrics["tokens_in"], result.metrics["tokens_out"]) == (0, 0)


@pytest.mark.parametrize(
    ("call", "why"),
    [
        ({"model": "fake:m"}, "names the model fake:m, but the run's model is scripted-policy"),
        ({"input_tokens": -1}, "model call 1 has a negative token count"),
        ({"output_tokens": -5}, "model call 1 has a negative token count"),
        ({"refused": "spend_cap"}, r"was refused \(spend_cap\), so it was never sent"),
    ],
)
def test_a_model_call_that_breaks_the_log_s_rules_is_refused(
    call: dict[str, Any], why: str
) -> None:
    """The harness's table refuses each of these (0011); records built by hand might not."""
    world = World()
    world.model_call(**{"input_tokens": 10, "output_tokens": 2, **call})
    with pytest.raises(RecordsError, match=why):
        world.evaluate()


# The result ------------------------------------------------------------------------------


def test_every_property_is_evaluated() -> None:
    world = World()
    order = world.request()
    world.committed(order, world.commit(order))
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    result = world.evaluate()
    assert result.evaluated == (1, 2, 3, 4, 5)
    assert [p.property for p in result.properties] == [1, 2, 3, 4, 5]
    assert [p.name for p in result.properties] == [
        "exactly_once",
        "authorized_at_commit",
        "inputs_still_fresh",
        "actually_committed",
        "truthfully_reported",
    ]
    assert {p.verdict for p in result.properties} == {PropertyVerdict.PASS}
    assert result.excused_intents == ()
    assert {number: set(result.property_result(number).metrics) for number in (2, 3, 4)} == {
        2: {"unauthorized_effects"},
        3: {"stale_effects"},
        4: {"missing_effects", "excused_intents"},
    }
    assert set(result.metrics) == {
        "duplicates",
        "unintended_effects",
        "unauthorized_effects",
        "stale_effects",
        "missing_effects",
        "excused_intents",
        "false_done_claims",
        "false_not_done_claims",
        "unjustified_claims",
        "unknown_claims",
        "success_claimed_with_duplicate",
        *RUN_METRICS,
        *RECOVERY_METRICS,
        *TOKEN_METRICS,
    }
    assert result.recovery == (), "no fault touched an intent"
    assert result.verdict is Verdict.PASS


def test_the_json_has_sorted_keys_and_does_not_depend_on_record_order() -> None:
    world = World(Config.B0)
    first = world.request()
    world.committed(first, world.commit(first), fate="dropped")
    second = world.request(vt=6_000)
    world.committed(second, world.commit(second))
    world.report({"REQ-1": ("done", ["PO-0002"]), **DONE_NOTHING})
    world.model_call(1_200, 80)
    world.model_call(None, None)
    world.model_call(900, 60)
    records = world.records()
    text = result_json(evaluate(records))
    assert text.endswith("\n")
    data = json.loads(text)
    assert list(data) == sorted(data)
    assert data["verdict"] == "fail"
    assert data["properties"][0]["reason_codes"] == ["duplicate_effects"]
    reversed_records = dataclasses.replace(
        records,
        effects=records.effects[::-1],
        messages=records.messages[::-1],
        reports=records.reports[::-1],
        approvals=records.approvals[::-1],
        fact_versions=records.fact_versions[::-1],
        reads=records.reads[::-1],
        model_calls=records.model_calls[::-1],
    )
    assert result_json(evaluate(reversed_records)) == text


# Recovery latency (0014) -------------------------------------------------------------------

WALL_EPOCH = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
"""A wall-clock time for hand-built records."""


def stamped(records: RunRecords, wall: Callable[[int], datetime | None]) -> RunRecords:
    """`records` with the wall-clock time `wall` gives each harness event: a fault's and an
    actor action's own seq, and a message's delivery."""
    return dataclasses.replace(
        records,
        faults=tuple(dataclasses.replace(f, wall_at=wall(f.seq)) for f in records.faults),
        messages=tuple(
            dataclasses.replace(
                m, delivered_wall_at=None if m.delivered_seq is None else wall(m.delivered_seq)
            )
            for m in records.messages
        ),
        actor_actions=tuple(
            dataclasses.replace(a, wall_at=wall(a.seq)) for a in records.actor_actions
        ),
    )


def resolved(
    fault: Fault,
    end_seq: int,
    end_vt: int,
    settled_by: SettledBy,
    *,
    outcome: Outcome = Outcome.COMMITTED,
    intent_id: str = "REQ-1",
    wall_ms: int | None = None,
) -> RecoveryEntry:
    """The entry of an intent whose outcome the evidence at `end_seq` settled."""
    return RecoveryEntry(
        intent_id=intent_id,
        fault_seq=fault.seq,
        fault=fault.fault,
        start_vt=fault.vt,
        outcome=outcome,
        settled_by=settled_by,
        end_seq=end_seq,
        end_vt=end_vt,
        latency_ms=end_vt - fault.vt,
        latency_wall_ms=wall_ms,
        knowledge_at_stop=None,
    )


def unresolved(
    fault: Fault, outcome: Outcome, knowledge: Knowledge, *, intent_id: str = "REQ-1"
) -> RecoveryEntry:
    """The entry of an intent whose outcome the evidence never settled."""
    return RecoveryEntry(
        intent_id=intent_id,
        fault_seq=fault.seq,
        fault=fault.fault,
        start_vt=fault.vt,
        outcome=outcome,
        settled_by=None,
        end_seq=None,
        end_vt=None,
        latency_ms=None,
        latency_wall_ms=None,
        knowledge_at_stop=knowledge,
    )


def delivered_at(message: Message) -> tuple[int, int]:
    """A delivered message's delivery seq and virtual time."""
    assert message.delivered_seq is not None and message.delivered_vt is not None
    return message.delivered_seq, message.delivered_vt


def lost_reply(world: World, *, vt: int = 1_000, key: str | None = None) -> tuple[Effect, Fault]:
    """F2: the order commits, and its answer is dropped on the stack's hop."""
    order = world.request(vt=vt, key=key)
    effect = world.commit(order)
    answer = world.committed(order, effect, fate="in_flight")
    fault = world.touch("REQ-1", vt=answer.sent_vt, target=answer)
    world.drop(answer)
    return effect, fault


def lost_request(world: World, *, vt: int = 1_000, key: str | None = None) -> Fault:
    """F12: the order is dropped on its way to the simulator."""
    order = world.request(vt=vt, key=key, deliver=False)
    fault = world.touch("REQ-1", fault="F12", point="net.gw_sim.request", vt=vt, target=order)
    world.drop(order)
    return fault


def test_a_write_answer_naming_an_effect_ends_recovery() -> None:
    """F2 at C2 in G2: the retry with the same key gets the stored result."""
    world = World()
    effect, fault = lost_reply(world, key="A-0001")
    retry = world.request(vt=3_020, key="A-0001")
    replayed = world.committed(retry, effect, replay=True)
    world.report({"REQ-1": ("done", ["PO-0001"]), **DONE_NOTHING})
    assert world.evaluate().recovery == (
        resolved(fault, *delivered_at(replayed), SettledBy.WRITE_ANSWER),
    )
    assert delivered_at(replayed)[1] - fault.vt == 2_030


def test_a_status_answer_listing_an_effect_ends_recovery() -> None:
    """F2 at C1: a probe finds the order."""
    world = World(contract=Contract.C1)
    effect, fault = lost_reply(world)
    found = world.status("REQ-1", [effect], as_of_vt=4_000)
    (entry,) = world.evaluate().recovery
    assert entry == resolved(fault, *delivered_at(found), SettledBy.STATUS_ANSWER)
    assert entry.latency_ms == 4_010 - 1_020


def test_an_attestation_naming_an_effect_ends_recovery() -> None:
    """F2 at C0 with a resolver: a human attests the order."""
    world = World(contract=Contract.C0)
    _, fault = lost_reply(world)
    attested = world.attest("committed", vt=31_000, effect_id="PO-0001")
    (entry,) = world.evaluate().recovery
    assert entry == resolved(fault, attested.seq, 31_000, SettledBy.ATTESTATION)


def test_a_rejection_of_the_request_ends_recovery_with_nothing_committed() -> None:
    """F7 late at C3: the quote changes as the order leaves, and the destination refuses it."""
    world = World(contract=Contract.C3)
    order = world.request(vt=1_000, deliver=False)
    fault = world.touch("REQ-1", fault="F7", point="net.gw_sim.request", vt=1_000, target=order)
    world.deliver(order)
    refused = world.rejected(order)
    (entry,) = world.evaluate().recovery
    assert entry == resolved(
        fault, *delivered_at(refused), SettledBy.REJECTED, outcome=Outcome.NOT_COMMITTED
    )
    assert entry.latency_ms == 30


def test_an_empty_status_answer_after_the_settle_time_ends_recovery() -> None:
    """F12 at C1: the order is lost; only a status answer accurate to the send plus S shows
    it never commits."""
    world = World(contract=Contract.C1)
    fault = lost_request(world)
    world.status("REQ-1", as_of_vt=1_000 + S - 1)
    settled = world.status("REQ-1", as_of_vt=1_000 + S)
    (entry,) = world.evaluate().recovery
    assert entry == resolved(
        fault, *delivered_at(settled), SettledBy.STATUS_EMPTY, outcome=Outcome.NOT_COMMITTED
    )
    assert entry.latency_ms == S + 10


def test_a_rejected_retry_with_the_same_key_ends_recovery_for_the_lost_request() -> None:
    """F12 at C2: the order is lost, and its retry with the same key is refused. The one
    rejection settles both requests; the first one's settlement names it."""
    world = World()
    fault = lost_request(world, key="A-0001")
    retry = world.request(vt=3_000, key="A-0001")
    refused = world.rejected(retry)
    (entry,) = world.evaluate().recovery
    assert entry == resolved(
        fault,
        *delivered_at(refused),
        SettledBy.SAME_KEY_REJECTED,
        outcome=Outcome.NOT_COMMITTED,
    )


def test_an_attestation_that_nothing_committed_ends_recovery() -> None:
    """F12 at C0 with a resolver, which attests after the settle time."""
    world = World(contract=Contract.C0)
    fault = lost_request(world)
    attested = world.attest("not_committed", vt=1_000 + S, intent_id="REQ-1")
    (entry,) = world.evaluate().recovery
    assert entry == resolved(
        fault,
        attested.seq,
        1_000 + S,
        SettledBy.ATTESTED_NOT_COMMITTED,
        outcome=Outcome.NOT_COMMITTED,
    )


def test_an_intent_the_stack_never_wrote_for_ends_at_the_fault() -> None:
    """F6 early in G2: the approval is revoked at the dispatch check, so nothing is sent.
    `K_i` is not_committed from the start: 0 ms, by the wall clock too."""
    world = World()
    fault = world.touch("REQ-1", fault="F6", point="gw.dispatch.before_check", vt=1_000)
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    (entry,) = world.evaluate().recovery
    assert entry == resolved(
        fault, fault.seq, 1_000, SettledBy.NO_WRITE, outcome=Outcome.NOT_COMMITTED, wall_ms=0
    )


def test_evidence_held_before_the_fault_gives_zero() -> None:
    """F11 after the tool result in G2: the gateway had the order's answer before the agent
    was killed, so the end is the start."""
    world = World()
    order = world.request(vt=1_000)
    world.committed(order, world.commit(order))
    fault = world.touch("REQ-1", fault="F11", point="agent.after_tool_result", vt=1_100)
    (entry,) = world.evaluate().recovery
    assert entry == resolved(fault, fault.seq, 1_100, SettledBy.WRITE_ANSWER, wall_ms=0)


@pytest.mark.parametrize(
    ("contract", "lost", "outcome"),
    [
        (Contract.C0, "reply", Outcome.COMMITTED),
        (Contract.C0, "request", Outcome.NOT_COMMITTED),
        (Contract.C2, "request", Outcome.NOT_COMMITTED),
    ],
)
def test_an_intent_whose_outcome_the_evidence_never_settles_is_unresolved(
    contract: Contract, lost: str, outcome: Outcome
) -> None:
    """F2 and F12 with nothing to recover by: no end, no latency, `K_i` at the stop."""
    world = World(contract=contract)
    fault = lost_reply(world)[1] if lost == "reply" else lost_request(world)
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert result.recovery == (unresolved(fault, outcome, Knowledge.UNKNOWABLE),)
    assert (result.metrics["affected_intents"], result.metrics["unresolved_intents"]) == (1, 1)


def test_a_commit_the_evidence_said_would_never_happen_is_unresolved() -> None:
    """F5 beyond the deadline at C1: an empty status answer after S says the order never
    commits, then it commits late. The stack's knowledge stays not_committed against a
    committed outcome."""
    world = World(contract=Contract.C1)
    order = world.request(vt=1_000)
    fault = world.touch("REQ-1", fault="F5", point="sim.write.received", vt=1_010, target=order)
    world.status("REQ-1", as_of_vt=1_000 + S)
    late = world.commit(order, vt=21_000)
    world.answer(order, committed_body(late), vt=21_000, fate="dropped")
    assert world.evaluate().recovery == (
        unresolved(fault, Outcome.COMMITTED, Knowledge.NOT_COMMITTED),
    )


def test_a_possibly_sent_request_needs_its_own_settlement() -> None:
    """F1b at C1: the request may have left, so only a status answer accurate to the crash
    plus S ends recovery."""
    world = World(contract=Contract.C1)
    fault = world.possibly_sent(vt=1_000, touches=True)
    world.status("REQ-1", as_of_vt=1_000 + S - 1)
    assert world.evaluate().recovery == (
        unresolved(fault, Outcome.NOT_COMMITTED, Knowledge.UNKNOWABLE),
    )
    settled = world.status("REQ-1", as_of_vt=1_000 + S)
    assert world.evaluate().recovery == (
        resolved(
            fault, *delivered_at(settled), SettledBy.STATUS_EMPTY, outcome=Outcome.NOT_COMMITTED
        ),
    )


def test_a_later_duplicate_does_not_move_the_end() -> None:
    """F2 in B0: the keyless resend commits a second order, whose answer ends recovery; a
    third order's answer afterwards changes nothing."""
    world = World(Config.B0)
    _, fault = lost_reply(world)
    resend = world.request(vt=3_000)
    first_known = world.committed(resend, world.commit(resend))
    third = world.request(vt=5_000)
    world.committed(third, world.commit(third))
    assert world.evaluate().recovery == (
        resolved(fault, *delivered_at(first_known), SettledBy.WRITE_ANSWER),
    )


def test_the_last_request_to_be_settled_ends_recovery() -> None:
    """Two lost orders at C1: each needs its own empty status answer after S."""
    world = World(contract=Contract.C1)
    fault = lost_request(world, vt=1_000)
    world.request(vt=4_000, deliver=False)
    world.status("REQ-1", as_of_vt=1_000 + S)
    settled = world.status("REQ-1", as_of_vt=4_000 + S)
    (entry,) = world.evaluate().recovery
    assert (entry.fault_seq, entry.end_seq, entry.end_vt) == (fault.seq, *delivered_at(settled))


def test_evidence_after_the_counted_report_ends_recovery_too() -> None:
    """The drain lets reconciliation settle (0009): an answer delivered after the report
    still ends recovery, though property 5 judges the report without it."""
    world = World()
    effect, fault = lost_reply(world, key="A-0001")
    world.report({"REQ-1": ("unknown", ()), **DONE_NOTHING}, vt=5_000)
    retry = world.request(vt=6_000, key="A-0001")
    replayed = world.committed(retry, effect, replay=True)
    result = world.evaluate()
    assert result.recovery == (resolved(fault, *delivered_at(replayed), SettledBy.WRITE_ANSWER),)
    assert _claim(result) == (Knowledge.UNKNOWABLE, True)


def test_an_answer_a_crash_voided_does_not_end_recovery() -> None:
    """F3 in G2: the crash voids the answer it touched; the retry's answer ends recovery."""
    world = World()
    order = world.request(vt=1_000, key="A-0001")
    effect = world.commit(order)
    voided = world.committed(order, effect)
    fault = world.crash(voided, vt=1_050, intent_id="REQ-1")
    retry = world.request(vt=3_000, key="A-0001")
    replayed = world.committed(retry, effect, replay=True)
    assert world.evaluate().recovery == (
        resolved(fault, *delivered_at(replayed), SettledBy.WRITE_ANSWER),
    )


def test_f9_touches_every_task_intent_and_entries_follow_the_scenario() -> None:
    """F9's record names no intent: a second worker can act on any. Faults that name no
    intent, or an intent outside the task, touch none."""
    world = World()
    world.touch(None, fault="F12", point="net.gw_sim.request", vt=500)
    world.touch("REQ-9", vt=600)
    started = world.touch(None, fault="F9", point="agent.step", vt=1_000)
    world.touch("REQ-1", vt=1_500)
    result = world.evaluate()
    assert [(entry.intent_id, entry.fault_seq, entry.fault) for entry in result.recovery] == [
        ("REQ-1", started.seq, "F9"),
        ("REQ-2", started.seq, "F9"),
    ]
    assert {entry.settled_by for entry in result.recovery} == {SettledBy.NO_WRITE}


def test_the_first_fault_by_seq_that_touched_an_intent_is_its_start() -> None:
    """F10 changes status answers about several intents: its row for an answer about REQ-2
    starts REQ-2's recovery, after its first row started REQ-1's. Record order does not
    matter."""
    world = World(contract=Contract.C1)
    first = world.touch("REQ-1", fault="F10", point="sim.status.answer", vt=1_000)
    world.touch("REQ-1", fault="F10", point="sim.status.answer", vt=1_010)
    second = world.touch("REQ-2", fault="F10", point="sim.status.answer", vt=2_010)
    records = world.records()
    shuffled = dataclasses.replace(records, faults=records.faults[::-1])
    for run in (records, shuffled):
        assert [(e.intent_id, e.fault_seq, e.start_vt) for e in evaluate(run).recovery] == [
            ("REQ-1", first.seq, 1_000),
            ("REQ-2", second.seq, 2_010),
        ]


def test_wall_clock_latency_runs_from_the_fault_to_the_evidence() -> None:
    """From the fault's `wall_at` to the delivery's `delivered_wall_at` or the attestation's
    `wall_at`, rounded to the nearest millisecond, half to even; None when one is missing."""
    world = World()
    effect, fault = lost_reply(world, key="A-0001")
    retry = world.request(vt=3_020, key="A-0001")
    replayed = world.committed(retry, effect, replay=True)
    end, _ = delivered_at(replayed)
    for gap_us, wall_ms in ((1_520_500, 1_520), (1_521_500, 1_522), (1_520_501, 1_521)):
        times = {fault.seq: WALL_EPOCH, end: WALL_EPOCH + timedelta(microseconds=gap_us)}
        (entry,) = evaluate(stamped(world.records(), times.get)).recovery
        assert (entry.latency_ms, entry.latency_wall_ms) == (2_030, wall_ms)
    (entry,) = evaluate(stamped(world.records(), {fault.seq: WALL_EPOCH}.get)).recovery
    assert (entry.latency_ms, entry.latency_wall_ms) == (2_030, None)
    resolver = World(contract=Contract.C0)
    _, lost = lost_reply(resolver)
    attested = resolver.attest("committed", vt=31_000, effect_id="PO-0001")
    times = {lost.seq: WALL_EPOCH, attested.seq: WALL_EPOCH + timedelta(seconds=30)}
    (entry,) = evaluate(stamped(resolver.records(), times.get)).recovery
    assert (entry.latency_ms, entry.latency_wall_ms) == (29_980, 30_000)


def test_evidence_dated_before_the_fault_it_ends_is_refused() -> None:
    """Virtual time never goes back between two harness events, so such records are
    broken: the evaluator refuses to guess."""
    world = World()
    effect, _ = lost_reply(world, key="A-0001", vt=5_000)
    retry = world.request(vt=1_000, key="A-0001")
    world.committed(retry, effect, replay=True)
    with pytest.raises(RecordsError, match=r"dated vt 1030, before the fault at seq"):
        world.evaluate()


def test_the_settlements_name_every_settlement_of_record_0004() -> None:
    """`settled_by` names the commit evidence, each way a request is shown never to
    commit, or that nothing was sent."""
    assert {str(kind) for kind in Settlement} <= {str(kind) for kind in SettledBy}
    assert {str(kind) for kind in SettledBy} - {str(kind) for kind in Settlement} == {
        "write_answer",
        "status_answer",
        "attestation",
        "no_write",
    }


def test_comparisons_leave_out_wall_clock_fields_at_any_depth_and_nothing_else() -> None:
    """0009, as amended by 0014: fields whose names end in `wall_at` or `wall_ms` are
    wall-clock time, wherever they are; `comparable_json` is `result_json` without them."""
    data = {
        "latency_ms": 5,
        "latency_wall_ms": 7,
        "wall_at": "x",
        "walls": 1,
        "rows": [{"wall_ms": 1, "seq": 2}, [{"delivered_wall_at": 3}], "wall_ms"],
    }
    assert without_wall_clock(data) == {
        "latency_ms": 5,
        "walls": 1,
        "rows": [{"seq": 2}, [{}], "wall_ms"],
    }
    world = World()
    _, fault = lost_reply(world, key="A-0001")
    retry = world.request(vt=3_020, key="A-0001")
    end, _ = delivered_at(world.committed(retry, world.effects[0], replay=True))
    timed = stamped(world.records(), {fault.seq: WALL_EPOCH, end: WALL_EPOCH}.get)
    plain, stamped_result = evaluate(world.records()), evaluate(timed)
    assert result_json(plain) != result_json(stamped_result)
    assert comparable_json(plain) == comparable_json(stamped_result)
    assert json.loads(comparable_json(plain)) == without_wall_clock(json.loads(result_json(plain)))


# Properties (0004, Checks) -----------------------------------------------------------------


TASK_INTENTS = ("REQ-1", "REQ-2")

STEPS = (
    "commit_seen",
    "commit_lost",
    "commit_late",
    "commit_voided",
    "rejected",
    "rejected_late",
    "request_lost",
    "no_answer",
    "status_seen",
    "status_empty",
    "status_late",
    "status_voided",
    "possibly_sent",
    "attest_commit",
    "attest_none",
)
"""What one step of a random run does. The `_seen` steps and `attest_commit` give the
stack commit evidence when the intent has an effect; every other step must not."""


def _random_world(data: st.DataObject) -> tuple[World, dict[str, set[str]], list[int]]:
    """A random run: writes, status answers, crashes, possibly-sent requests and
    attestations, then a report, then some deliveries after it.

    Returns the world; for each task intent, the effects whose commit evidence reached
    the stack before the report and was not voided (worked out here as the run is built,
    independently of the evaluator); and the seqs of the messages delivered after the
    report.
    """
    config = data.draw(st.sampled_from(list(Config)), label="config")
    world = World(config, data.draw(st.sampled_from(list(Contract)), label="contract"))
    known: dict[str, set[str]] = {intent: set() for intent in TASK_INTENTS}
    pending: list[Message] = []
    steps = data.draw(st.lists(st.sampled_from(STEPS), min_size=1, max_size=8), label="steps")
    for number, step in enumerate(steps):
        vt = 1_000 * (number + 1)
        intent = data.draw(st.sampled_from([*TASK_INTENTS, *TASK_INTENTS, "REQ-9"]), label="intent")
        effects = [effect for effect in world.effects if effect.intent_id == intent]
        if step.startswith(("commit", "rejected", "request", "no_answer")):
            key = data.draw(st.sampled_from([None, "A-0001", "A-0002"]), label="key")
            request = world.request(intent, vt=vt, key=key, deliver=step != "request_lost")
            if step == "request_lost":
                world.drop(request)
                continue
            if step == "no_answer":
                continue
            body: object
            effect: Effect | None = None
            if step.startswith("commit"):
                effect = world.commit(request)
                body = committed_body(effect)
            else:
                body = WriteRejected(reason=WriteRejectReason.FORBIDDEN).model_dump(mode="json")
            if step.endswith("lost"):
                world.answer(request, body, fate="dropped")
            elif step.endswith("late"):
                pending.append(world.answer(request, body, fate="in_flight"))
            elif step.endswith("voided"):
                world.crash(world.answer(request, body))
            else:
                world.answer(request, body)
                if effect is not None and step == "commit_seen" and intent in TASK_INTENTS:
                    known[intent].add(effect.effect_id)
        elif step.startswith("status") and intent in TASK_INTENTS:
            listed = [] if step == "status_empty" else effects
            offset = data.draw(st.sampled_from([0, S - 1, S]), label="as_of offset")
            fate = "in_flight" if step == "status_late" else "delivered"
            answer = world.status(intent, listed, as_of_vt=vt + offset, fate=fate)
            if step == "status_late":
                pending.append(answer)
            elif step == "status_voided":
                world.crash(answer)
            else:
                known[intent].update(effect.effect_id for effect in listed)
        elif step == "possibly_sent" and intent in TASK_INTENTS:
            key = data.draw(st.sampled_from([None, "A-0001"]), label="key")
            world.possibly_sent(intent, vt=vt, key=key)
        elif step == "attest_commit" and effects:
            world.attest("committed", vt=vt, effect_id=effects[0].effect_id)
            if intent in TASK_INTENTS:
                known[intent].add(effects[0].effect_id)
        elif step == "attest_none" and intent in TASK_INTENTS:
            made_at = vt + data.draw(st.sampled_from([0, S]), label="attested after")
            world.attest("not_committed", vt=made_at, intent_id=intent)
    claims: dict[str, tuple[str, Sequence[str]]] = {}
    for intent in TASK_INTENTS:
        status = data.draw(st.sampled_from(["unknown", "done", "not_done"]), label="claim")
        ids = [e.effect_id for e in world.effects if e.intent_id == intent] + ["PO-0099"]
        chosen = data.draw(st.lists(st.sampled_from(ids), min_size=1, max_size=2, unique=True))
        claims[intent] = (status, chosen if status == "done" else ())
    world.report(claims, vt=60_000)
    late = []
    for message in pending:
        if data.draw(st.booleans(), label="delivered after the report"):
            late.append(world.deliver(message).seq)
    return world, known, late


RANDOM_RUNS = settings(max_examples=max(300, settings().max_examples))
"""At 300 examples every settlement and claim outcome appears (counted 2026-11-15)."""


@RANDOM_RUNS
@given(st.data())
def test_unknown_never_passes_when_commit_evidence_reached_the_stack(data: st.DataObject) -> None:
    world, known, _ = _random_world(data)
    result = world.evaluate()
    assert result.report is not None
    for judgment in result.report.claims:
        if known[judgment.intent_id] and judgment.status == "unknown":
            assert not judgment.passed
            assert result.property_result(5).verdict is PropertyVerdict.FAIL
            assert result.verdict is Verdict.FAIL


@RANDOM_RUNS
@given(st.data())
def test_known_effects_are_exactly_the_commit_evidence_that_reached_the_stack(
    data: st.DataObject,
) -> None:
    world, known, _ = _random_world(data)
    result = world.evaluate()
    assert result.report is not None
    for judgment in result.report.claims:
        assert set(judgment.known_effects) == known[judgment.intent_id]
        committed = judgment.knowledge is Knowledge.COMMITTED
        assert committed == bool(known[judgment.intent_id])


@RANDOM_RUNS
@given(st.data())
def test_messages_delivered_after_the_report_and_record_order_change_nothing(
    data: st.DataObject,
) -> None:
    world, _, late = _random_world(data)
    records = world.records()
    undelivered = dataclasses.replace(
        records,
        messages=tuple(
            dataclasses.replace(m, fate=None, delivered_seq=None, delivered_vt=None)
            if m.seq in late
            else m
            for m in records.messages
        ),
    )
    shuffled = dataclasses.replace(
        records,
        effects=records.effects[::-1],
        messages=records.messages[::-1],
        faults=records.faults[::-1],
        actor_actions=records.actor_actions[::-1],
        approvals=records.approvals[::-1],
        fact_versions=records.fact_versions[::-1],
        reads=records.reads[::-1],
    )
    text = result_json(evaluate(records))
    assert result_json(evaluate(undelivered)) == text
    assert result_json(evaluate(shuffled)) == text


MISSING_STEPS = st.tuples(
    st.sampled_from(["commit", "commit", "revoke", "deny", "file"]),
    st.sampled_from([*TASK_INTENTS, "REQ-9"]),
)
"""A step of `test_a_missing_effect_never_fails_both_property_1_and_property_4`: commit an
acceptable effect for an intent; withdraw authority for it, by a revocation or a denial by
the approver; or file an approval for it that stays valid."""


def _named(result: RunResult, number: int) -> set[str | None]:
    return {failure.intent_id for failure in result.property_result(number).failures}


@given(st.lists(MISSING_STEPS, max_size=8), st.booleans())
def test_a_missing_effect_never_fails_both_property_1_and_property_4(
    steps: list[tuple[str, str]], report: bool
) -> None:
    """0004, Checks: a missing effect fails property 4 alone, unless it is excused."""
    world = World(scenario=BOTH_REQUIRED)
    for number, (step, intent) in enumerate(steps):
        vt = 1_000 * (number + 1)
        if step == "commit":
            world.commit(world.request(intent, vt=vt))
        elif step == "revoke":
            world.revoke(world.approval(intent, vt=vt).approval_id, vt=vt)
        elif step == "deny":
            world.approval(intent, decision="deny", by="approver", vt=vt)
        else:
            world.approval(intent, vt=vt)
    if report:
        world.report({intent: ("unknown", ()) for intent in TASK_INTENTS})
    result = world.evaluate()
    counts = {
        intent: sum(effect.intent_id == intent for effect in world.effects)
        for intent in (*TASK_INTENTS, "REQ-9")
    }
    excused = {excuse.intent_id for excuse in result.excused_intents}
    for intent in TASK_INTENTS:
        assert not (intent in _named(result, 1) and intent in _named(result, 4))
        if counts[intent] == 0:
            assert intent not in _named(result, 1)
            assert (intent in _named(result, 4)) != (intent in excused)
        else:
            assert intent not in _named(result, 4) | excused
    missing = sum(counts[intent] == 0 for intent in TASK_INTENTS)
    assert result.metrics["missing_effects"] + result.metrics["excused_intents"] == missing
    fails = any(counts[intent] > 1 for intent in TASK_INTENTS) or counts["REQ-9"] > 0
    assert (result.property_result(1).verdict is PropertyVerdict.FAIL) == fails
    # Removing an intent's effects never makes property 1 fail where it passed, and the
    # intent then fails property 4 unless it is excused.
    for intent in TASK_INTENTS:
        records = world.records()
        fewer = evaluate(
            dataclasses.replace(
                records, effects=tuple(e for e in records.effects if e.intent_id != intent)
            )
        )
        assert not (fewer.property_result(1).verdict is PropertyVerdict.FAIL and not fails)
        assert intent not in _named(fewer, 1)
        excused_now = {excuse.intent_id for excuse in fewer.excused_intents}
        assert (intent in _named(fewer, 4)) != (intent in excused_now)


RECOVERY_STEPS = (
    "touch",
    "touch",
    "touch_every",
    "commit_seen",
    "commit_lost",
    "commit_lost",
    "commit_voided",
    "rejected",
    "rejected",
    "request_lost",
    "request_lost",
    "no_answer",
    "status_seen",
    "status_seen",
    "status_empty",
    "possibly_sent",
    "attest_commit",
    "attest_none",
    "retry_refused",
    "lost_then_settled",
    "report",
)
"""What one step of a random run for recovery latency does: a fault that touches an
intent, F9, or a step of `STEPS` that writes, asks for status, crashes or attests. Two
steps settle a lost request: its retry with the same key is refused, or after S an empty
status answer (at C1) or a human says it never committed. The report can come at any
step, so evidence can follow it. Status answers come only at C1, where the destination
offers them. Most steps act on REQ-1, and lost writes, rejections and status answers come
twice as often, so that every way of ending recovery appears (counted 2026-11-15: each
`SettledBy` at 300 examples)."""

C1 = Contract.C1

OUTCOME_KNOWLEDGE = {
    Outcome.COMMITTED: Knowledge.COMMITTED,
    Outcome.NOT_COMMITTED: Knowledge.NOT_COMMITTED,
}


def _recovery_world(data: st.DataObject) -> tuple[World, dict[str, int]]:
    """A random run with faults that touch intents, in which virtual time never goes back
    from one harness event to the next: each step has 20,000 ms, more than any step needs.

    Returns the world, and the seq of the first fault that touched each task intent,
    worked out here as the run is built, independently of the evaluator.
    """
    config = data.draw(st.sampled_from(list(Config)), label="config")
    world = World(config, data.draw(st.sampled_from(list(Contract)), label="contract"))
    touched: dict[str, int] = {}
    steps = data.draw(st.lists(st.sampled_from(RECOVERY_STEPS), min_size=1, max_size=14))
    for number, step in enumerate(steps):
        vt = 20_000 * (number + 1)
        intent = data.draw(st.sampled_from(["REQ-1", "REQ-1", "REQ-1", "REQ-2", "REQ-9"]))
        effects = [effect for effect in world.effects if effect.intent_id == intent]
        if step == "touch":
            named = data.draw(st.sampled_from([*TASK_INTENTS, "REQ-9", None]), label="touched")
            fault = world.touch(named, vt=vt)
            if named in TASK_INTENTS:
                touched.setdefault(named, fault.seq)
        elif step == "touch_every":
            fault = world.touch(None, fault="F9", point="agent.step", vt=vt)
            for name in TASK_INTENTS:
                touched.setdefault(name, fault.seq)
        elif step == "report":
            world.report({name: ("unknown", ()) for name in TASK_INTENTS}, vt=vt)
        elif step == "retry_refused":
            world.drop(world.request(intent, vt=vt, key="A-0001", deliver=False))
            world.rejected(world.request(intent, vt=vt + 1_000, key="A-0001"))
        elif step == "lost_then_settled":
            world.drop(world.request(intent, vt=vt, deliver=False))
            if intent in TASK_INTENTS and world.contract is C1:
                world.status(intent, as_of_vt=vt + S)
            elif intent in TASK_INTENTS:
                world.attest("not_committed", vt=vt + S, intent_id=intent)
        elif step.startswith(("commit", "rejected", "request", "no_answer")):
            key = data.draw(st.sampled_from([None, "A-0001", "A-0001"]), label="key")
            request = world.request(intent, vt=vt, key=key, deliver=step != "request_lost")
            if step == "request_lost":
                world.drop(request)
                continue
            if step == "no_answer":
                continue
            body: object
            if step.startswith("commit"):
                body = committed_body(world.commit(request))
            else:
                body = WriteRejected(reason=WriteRejectReason.FORBIDDEN).model_dump(mode="json")
            if step == "commit_lost":
                world.answer(request, body, fate="dropped")
            elif step == "commit_voided":
                fault = world.crash(world.answer(request, body), vt=vt + 100, intent_id=intent)
                if intent in TASK_INTENTS:
                    touched.setdefault(intent, fault.seq)
            else:
                world.answer(request, body)
        elif step.startswith("status") and intent in TASK_INTENTS and world.contract is C1:
            listed = effects if step == "status_seen" else []
            offset = data.draw(st.sampled_from([0, S - 1, S]), label="as_of offset")
            world.status(intent, listed, as_of_vt=vt + offset)
        elif step == "possibly_sent" and intent in TASK_INTENTS:
            key = data.draw(st.sampled_from([None, "A-0001"]), label="key")
            fault = world.possibly_sent(intent, vt=vt, key=key, touches=True)
            touched.setdefault(intent, fault.seq)
        elif step == "attest_commit" and effects:
            world.attest("committed", vt=vt, effect_id=effects[0].effect_id)
        elif step == "attest_none" and intent in TASK_INTENTS:
            made_at = vt + data.draw(st.sampled_from([0, S]), label="attested after")
            world.attest("not_committed", vt=made_at, intent_id=intent)
    return world, touched


def _rising_wall(data: st.DataObject, last: int) -> Callable[[int], datetime | None]:
    """Wall-clock times that never go back from one harness seq to the next."""
    steps = data.draw(
        st.lists(st.integers(0, 3_000_000), min_size=last + 1, max_size=last + 1),
        label="wall-clock steps in microseconds",
    )
    times = [WALL_EPOCH]
    for step in steps:
        times.append(times[-1] + timedelta(microseconds=step))
    return lambda seq: times[seq]


def _any_wall(data: st.DataObject, last: int) -> Callable[[int], datetime | None]:
    """Any wall-clock times, missing ones included, in any order."""
    times = data.draw(
        st.lists(
            st.none()
            | st.datetimes(
                datetime(2020, 1, 1, tzinfo=UTC),
                datetime(2030, 1, 1, tzinfo=UTC),
                timezones=st.just(UTC),
            ),
            min_size=last + 1,
            max_size=last + 1,
        ),
        label="wall-clock times",
    )
    return lambda seq: times[seq]


def _last_seq(world: World) -> int:
    return world.seq


@RANDOM_RUNS
@given(st.data())
def test_recovery_latency_is_never_negative_and_ends_where_the_knowledge_settles(
    data: st.DataObject,
) -> None:
    """0014: an entry for each touched task intent, in scenario order, from its first
    fault. A resolved intent's `K_i(t)` equals its outcome at its end and at every later
    seq, and not just before an end after the start; an unresolved one's never does by
    the stop. Latencies are never negative, by the wall clock either while it runs
    forward."""
    world, touched = _recovery_world(data)
    records = stamped(world.records(), _rising_wall(data, _last_seq(world)))
    result = evaluate(records)
    assert [entry.intent_id for entry in result.recovery] == [
        name for name in TASK_INTENTS if name in touched
    ]
    seqs = range(1, _last_seq(world) + 1)

    def known(seq: int) -> dict[str, Knowledge]:
        """`K_i(seq)`: the evidence the stack had once harness event `seq` happened."""
        return {name: k.knowledge for name, k in stack_knowledge(records, seq + 1).items()}

    for entry in result.recovery:
        assert entry.fault_seq == touched[entry.intent_id]
        target = OUTCOME_KNOWLEDGE[entry.outcome]
        has_effect = any(effect.intent_id == entry.intent_id for effect in world.effects)
        assert (entry.outcome is Outcome.COMMITTED) == has_effect
        if entry.end_seq is None:
            assert known(seqs[-1])[entry.intent_id] is not target
            assert entry.knowledge_at_stop is known(seqs[-1])[entry.intent_id]
            assert (entry.end_vt, entry.latency_ms, entry.latency_wall_ms) == (None,) * 3
            continue
        assert entry.end_vt is not None and entry.latency_ms is not None
        assert entry.end_seq >= entry.fault_seq
        assert entry.latency_ms == entry.end_vt - entry.start_vt >= 0
        assert entry.latency_wall_ms is not None and entry.latency_wall_ms >= 0
        assert entry.knowledge_at_stop is None and entry.settled_by is not None
        for seq in seqs:
            if seq >= entry.end_seq:
                assert known(seq)[entry.intent_id] is target, (entry, seq)
        if entry.end_seq > entry.fault_seq:
            assert known(entry.end_seq - 1)[entry.intent_id] is not target
    assert result.metrics["affected_intents"] == len(result.recovery)
    assert result.metrics["unresolved_intents"] == sum(
        1 for entry in result.recovery if not entry.resolved
    )


@RANDOM_RUNS
@given(st.data())
def test_wall_clock_columns_change_no_virtual_value(data: st.DataObject) -> None:
    """0014: whatever the wall-clock times, missing or out of order, the result is the
    same but for its fields that end in `wall_ms`."""
    world, _ = _recovery_world(data)
    records = world.records()
    plain = evaluate(records)
    for _ in range(2):
        timed = evaluate(stamped(records, _any_wall(data, _last_seq(world))))
        assert comparable_json(timed) == comparable_json(plain)
        assert without_wall_clock(json.loads(result_json(timed))) == json.loads(
            comparable_json(plain)
        )
        assert timed.model_copy(update={"recovery": plain.recovery}) == plain
