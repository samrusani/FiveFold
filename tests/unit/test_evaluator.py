"""The evaluator's checks on hand-built records, without a database (0004).

`World` builds a run's records the way the harness and the simulator would write them:
one harness counter numbers every send, delivery, fault, actor action and report, so the
order of the calls here is the order of events in the run. The property tests at the end
are 0004's Checks: `unknown` never passes when commit evidence was delivered and not
voided, and a missing effect never fails property 1.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fivefold.domain.api import (
    StatusAnswer,
    StatusEffect,
    WriteCommitted,
    WriteInProgress,
    WriteRejected,
    WriteRejectReason,
)
from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.rules import Operation
from fivefold.domain.scenario import Scenario, scenario_from_data
from fivefold.evaluator import (
    ActorAction,
    Config,
    Destination,
    Effect,
    Fault,
    Knowledge,
    Message,
    PropertyVerdict,
    ReasonCode,
    RecordsError,
    RunInfo,
    RunRecords,
    RunResult,
    Settlement,
    SubmittedReport,
    Verdict,
    evaluate,
    result_json,
    stack_knowledge,
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

    @property
    def stack(self) -> str:
        """The stack component that talks to the simulator."""
        return "gateway" if self.config.has_gateway else "agent-1"

    def next(self) -> int:
        self.seq += 1
        return self.seq

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

    def request(
        self,
        intent_id: str = "REQ-1",
        *,
        vt: int = 1_000,
        key: str | None = None,
        sender: str | None = None,
        deliver: bool = True,
        operation: str | None = None,
    ) -> Message:
        """A stack write request, delivered unless told not to. Its operation is the
        intent's, or a purchase order for an intent outside the task."""
        if operation is None:
            known = {intent.id: str(intent.operation) for intent in self.scenario.intents}
            operation = known.get(intent_id, PO)
        args = ORDER_ARGS if operation == PO else RESERVATION_ARGS
        body: dict[str, object] = {"intent_id": intent_id, "approval_id": "APR-0001"}
        body.update(basis={}, idempotency_key=key, **args)
        message = self.send(body, vt=vt, api=operation, sender=sender)
        return self.deliver(message) if deliver else message

    def commit(self, request: Message, *, vt: int | None = None) -> Effect:
        """The simulator commits `request` as a new effect."""
        body = request.body
        assert isinstance(body, dict)
        operation = Operation(str(request.api))
        number = 1 + sum(1 for effect in self.effects if effect.operation is operation)
        prefix = "PO" if operation is Operation.SUBMIT_PURCHASE_ORDER else "RS"
        self.sim_seq += 2
        effect = Effect(
            effect_id=f"{prefix}-{number:04d}",
            intent_id=str(body["intent_id"]),
            operation=operation,
            args={k: v for k, v in body.items() if k in ORDER_ARGS or k in RESERVATION_ARGS},
            approval_id="APR-0001",
            basis={},
            idempotency_key=body.get("idempotency_key"),
            client_ref=None,
            received_seq=self.sim_seq - 1,
            commit_seq=self.sim_seq,
            committed_vt=request.sent_vt + 20 if vt is None else vt,
        )
        self.effects.append(effect)
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
        self, *voided: Message, point: str = "gw.dispatch.after_answer", vt: int = 0
    ) -> Fault:
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
        )
        self.faults.append(fault)
        return fault

    def possibly_sent(
        self, intent_id: str = "REQ-1", *, vt: int = 1_000, key: str | None = None
    ) -> Fault:
        """A gateway crash after recording an attempt and before sending it (F1b)."""
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
        )
        self.faults.append(fault)
        return fault

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
                scenario_digest="sha256:test",
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
        )

    def evaluate(self) -> RunResult:
        return evaluate(self.records())


def _codes(result: RunResult, number: int) -> tuple[ReasonCode, ...]:
    return result.property_result(number).reason_codes


def _claim(result: RunResult, intent_id: str = "REQ-1") -> tuple[Knowledge, bool]:
    assert result.report is not None
    judgment = next(c for c in result.report.claims if c.intent_id == intent_id)
    return judgment.knowledge, judgment.passed


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
    assert result.verdict is Verdict.PASS
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


def test_knowledge_is_computed_for_every_task_intent_in_scenario_order() -> None:
    world = World()
    reservation = world.request("REQ-2")
    world.committed(reservation, world.commit(reservation))
    found = stack_knowledge(world.records(), t_r=world.seq + 1)
    assert list(found) == ["REQ-1", "REQ-2"]
    assert found["REQ-1"].knowledge is Knowledge.NOT_COMMITTED
    assert found["REQ-2"].known_effects == ("RS-0001",)


# The result ------------------------------------------------------------------------------


def test_properties_2_to_4_are_marked_not_evaluated() -> None:
    world = World()
    world.report({"REQ-1": ("not_done", ()), **DONE_NOTHING})
    result = world.evaluate()
    assert result.evaluated == (1, 5)
    assert [p.property for p in result.properties] == [1, 2, 3, 4, 5]
    for number in (2, 3, 4):
        assert result.property_result(number).verdict is PropertyVerdict.NOT_EVALUATED
        assert result.property_result(number).metrics == {}
    assert result.excused_intents is None
    assert set(result.metrics) == {
        "duplicates",
        "unintended_effects",
        "false_done_claims",
        "false_not_done_claims",
        "unjustified_claims",
        "unknown_claims",
        "success_claimed_with_duplicate",
    }


def test_the_json_has_sorted_keys_and_does_not_depend_on_record_order() -> None:
    world = World(Config.B0)
    first = world.request()
    world.committed(first, world.commit(first), fate="dropped")
    second = world.request(vt=6_000)
    world.committed(second, world.commit(second))
    world.report({"REQ-1": ("done", ["PO-0002"]), **DONE_NOTHING})
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
    )
    assert result_json(evaluate(reversed_records)) == text


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
    )
    text = result_json(evaluate(records))
    assert result_json(evaluate(undelivered)) == text
    assert result_json(evaluate(shuffled)) == text


@given(st.lists(st.sampled_from([*TASK_INTENTS, "REQ-9"]), max_size=8), st.booleans())
def test_a_missing_effect_never_fails_property_1(intents: list[str], report: bool) -> None:
    world = World()
    for step, intent in enumerate(intents):
        world.commit(world.request(intent, vt=1_000 * (step + 1)))
    if report:
        world.report({intent: ("unknown", ()) for intent in TASK_INTENTS})
    result = world.evaluate()
    exactly_once = result.property_result(1)
    counts = {intent: intents.count(intent) for intent in [*TASK_INTENTS, "REQ-9"]}
    named = {failure.intent_id for failure in exactly_once.failures}
    for intent in TASK_INTENTS:
        if counts[intent] == 0:
            assert intent not in named
    fails = any(counts[intent] > 1 for intent in TASK_INTENTS) or counts["REQ-9"] > 0
    assert (exactly_once.verdict is PropertyVerdict.FAIL) == fails
    # Removing an intent's effects never makes property 1 fail where it passed.
    for intent in TASK_INTENTS:
        records = world.records()
        fewer = dataclasses.replace(
            records, effects=tuple(e for e in records.effects if e.intent_id != intent)
        )
        verdict = evaluate(fewer).property_result(1).verdict
        assert not (verdict is PropertyVerdict.FAIL and not fails)
