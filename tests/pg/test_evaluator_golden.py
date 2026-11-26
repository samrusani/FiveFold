"""Golden runs: hand-built ground truth in a run database, with expected results (0004).

Each golden run is written as the owning roles would write it: the simulator's ground
truth through the simulator itself, as ff_sim, and the harness's records by hand, as
ff_harness, numbering every send, delivery, fault, actor action and report on one
counter. The evaluator then reads the run as ff_eval. There is at least one golden run
per reason code (0004, Checks), one for each passing verdict, one for each excuse, and
one whose model calls used tokens.

Each fault row names the task intent it touched, as the harness writes it, so the golden
runs with faults have recovery entries (0014). The recorder stamps a fault, a delivery and
a human's action with a wall-clock time twice its virtual time from a fixed moment, so
each wall-clock latency is fixed too: twice the virtual one.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from fivefold.domain.api import (
    GetBudget,
    GetOrderStatus,
    GetQuote,
    PurchaseOrderScope,
    Refusal,
    RequestApproval,
    SubmitPurchaseOrder,
    WriteCommitted,
    WriteReceived,
)
from fivefold.domain.authority import Decision
from fivefold.domain.contracts import Contract
from fivefold.domain.rules import Operation
from fivefold.domain.scenario import Scenario, scenario_digest, scenario_from_data
from fivefold.evaluator import (
    RUN_METRICS,
    TOKEN_METRICS,
    Config,
    Excuse,
    Knowledge,
    PropertyVerdict,
    ReasonCode,
    RunResult,
    SettledBy,
    Settlement,
    Verdict,
    WrongRole,
    evaluate_run,
    result_json,
)
from fivefold.sim import SimConfig, Simulator

pytestmark = pytest.mark.pg

Connection = psycopg.Connection[TupleRow]
PO = "submit_purchase_order"
SETTLE_MS = 10_050
"""S for dev-001: max_hop_latency 50 plus commit_deadline 10,000."""

WALL_EPOCH = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def wall(vt: int) -> datetime:
    """The wall-clock time the recorder gives an event at `vt`: twice `vt` after a fixed
    moment."""
    return WALL_EPOCH + timedelta(milliseconds=2 * vt)


class Recorder:
    """Writes one run's records to its database, in event order."""

    def __init__(
        self,
        dsn: str,
        data: Mapping[str, object],
        config: Config,
        contract: Contract,
        *,
        adapter: str = "scripted",
        model: str = "scripted-policy",
    ):
        self.dsn = dsn
        self.config = config
        self.model = model
        self.calls = 0
        self.scenario: Scenario = scenario_from_data(data)
        callers = ("gateway",) if config.has_gateway else ("agent-1", "agent-2")
        self.sim = Simulator.setup(
            make_conninfo(dsn, user="ff_sim"),
            self.scenario,
            SimConfig.preset(
                contract, credentials=callers, params=self.scenario.params.contract_params()
            ),
        )
        self.harness: Connection = psycopg.connect(
            make_conninfo(dsn, user="ff_harness"), autocommit=True
        )
        self.harness.execute(
            "INSERT INTO harness.run (run_id, scenario_id, scenario_digest, config, contract,"
            " seed, topology, model_adapter, model, params)"
            " VALUES ('RUN-golden', %s, %s, %s, %s, 7, 'inproc', %s, %s, %s)",
            (
                self.scenario.id,
                scenario_digest(self.scenario),
                str(config),
                str(contract),
                adapter,
                model,
                Jsonb(self.scenario.params.model_dump()),
            ),
        )
        self.seq = 0
        # What the scripted policy does first: read the quote and the budget, and get an
        # approval, which policy grants at once (0008). Neither is logged as a message here.
        self.sim.get_quote(GetQuote(supplier_id="S-1", sku="W-100"), caller=self.stack, vt=0)
        self.sim.get_budget(GetBudget(cost_center="CC-OPS"), caller=self.stack, vt=0)
        scope = PurchaseOrderScope(
            supplier_id="S-1",
            sku="W-100",
            max_quantity=100,
            max_amount_minor=54_000,
            currency="USD",
            cost_center="CC-OPS",
        )
        request = RequestApproval(
            intent_id="REQ-1", operation=Operation.SUBMIT_PURCHASE_ORDER, scope=scope
        )
        self.sim.request_approval(request, caller=self.stack, vt=0)

    @property
    def stack(self) -> str:
        return "gateway" if self.config.has_gateway else "agent-1"

    def close(self) -> None:
        self.sim.close()
        self.harness.close()

    def next(self) -> int:
        self.seq += 1
        return self.seq

    # Harness records -----------------------------------------------------------------------

    def send(
        self,
        body: object,
        *,
        vt: int,
        api: str,
        kind: str = "request",
        sender: str | None = None,
        receiver: str = "simulator",
        hop: str | None = None,
        correlation: int | None = None,
        intent_id: str | None = None,
        key: str | None = None,
        answer_class: str | None = None,
    ) -> int:
        seq = self.next()
        self.harness.execute(
            "INSERT INTO harness.message_log (seq, hop, kind, sender, receiver, api, correlation,"
            " intent_id, idempotency_key, answer_class, body, sent_vt)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                seq,
                hop or self.config.stack_hop,
                kind,
                sender or self.stack,
                receiver,
                api,
                correlation,
                intent_id,
                key,
                answer_class,
                Jsonb(body),
                vt,
            ),
        )
        return seq

    def deliver(self, seq: int, vt: int) -> int:
        delivered = self.next()
        self.harness.execute(
            "UPDATE harness.message_log SET fate = 'delivered', delivered_seq = %s,"
            " delivered_vt = %s, delivered_wall_at = %s WHERE seq = %s",
            (delivered, vt, wall(vt), seq),
        )
        return delivered

    def drop(self, seq: int, reason: str = "fault") -> None:
        self.harness.execute(
            "UPDATE harness.message_log SET fate = 'dropped', drop_reason = %s WHERE seq = %s",
            (reason, seq),
        )

    def fault(
        self,
        fault: str,
        point: str,
        *,
        vt: int,
        action: str,
        target_seq: int | None = None,
        component: str | None = None,
        voided: Sequence[int] = (),
        params: Mapping[str, object] | None = None,
        variant: str | None = None,
        intent_id: str | None = "REQ-1",
    ) -> int:
        """A fault that fired, naming the intent it touched: REQ-1, the task's one intent,
        unless told otherwise (0014)."""
        seq = self.next()
        self.harness.execute(
            "INSERT INTO harness.fault_log (seq, fault, variant, point, occurrence, action,"
            " target_seq, component, voided, params, vt, intent_id, wall_at)"
            " VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                seq,
                fault,
                variant,
                point,
                action,
                target_seq,
                component,
                list(voided),
                Jsonb(dict(params or {})),
                vt,
                intent_id,
                wall(vt),
            ),
        )
        return seq

    def attest(self, details: Mapping[str, object], *, vt: int) -> int:
        seq = self.next()
        self.harness.execute(
            "INSERT INTO harness.actor_log (seq, actor, action, subject, details, vt, wall_at)"
            " VALUES (%s, 'resolver', 'attest', 'ESC-0001', %s, %s, %s)",
            (seq, Jsonb(dict(details)), vt, wall(vt)),
        )
        return seq

    def model_call(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        *,
        asked_vt: int,
        refused: str | None = None,
    ) -> None:
        """A model call of agent-1, answered 1,000 ms later with this usage, as the harness
        logs it: numbered after the run's earlier calls, and on no harness event."""
        self.calls += 1
        self.harness.execute(
            "INSERT INTO harness.model_call (call_no, worker, asked_vt, answered_vt, model,"
            " input_tokens, output_tokens, refused, wall_ms)"
            " VALUES (%s, 'agent-1', %s, %s, %s, %s, %s, %s, 0)",
            (
                self.calls,
                asked_vt,
                asked_vt + 1_000,
                self.model,
                input_tokens,
                output_tokens,
                refused,
            ),
        )

    def approver(self, action: str, approval_id: str, *, vt: int) -> None:
        """The scripted approver decides or revokes an approval through the simulator's
        control interface, as the harness does (0002, rules 7 and 8), and logs it."""
        control = self.sim.control
        if action == "revoke":
            answer = control.revoke(approval_id, actor="approver", vt=vt)
        else:
            answer = control.decide(approval_id, Decision(action), actor="approver", vt=vt)
        self.harness.execute(
            "INSERT INTO harness.actor_log (seq, actor, action, subject, details, vt, wall_at)"
            " VALUES (%s, 'approver', %s, %s, %s, %s, %s)",
            (self.next(), action, approval_id, Jsonb({"status": str(answer.status)}), vt, wall(vt)),
        )

    def approval(self, *, supplier_id: str, unit_price_minor: int, vt: int) -> str:
        """Another approval request for REQ-1, for 100 W-100 from a supplier at a price.
        Returns the approval's id."""
        scope = PurchaseOrderScope(
            supplier_id=supplier_id,
            sku="W-100",
            max_quantity=100,
            max_amount_minor=100 * unit_price_minor,
            currency="USD",
            cost_center="CC-OPS",
        )
        request = RequestApproval(
            intent_id="REQ-1", operation=Operation.SUBMIT_PURCHASE_ORDER, scope=scope
        )
        answer = self.sim.request_approval(request, caller=self.stack, vt=vt)
        assert not isinstance(answer, Refusal)
        return answer.approval_id

    # Calls -----------------------------------------------------------------------------------

    def order(
        self,
        *,
        vt: int,
        intent_id: str = "REQ-1",
        key: str | None = None,
        sender: str | None = None,
        approval_id: str = "APR-0001",
        basis: Mapping[str, int] | None = None,
        supplier_id: str = "S-1",
        unit_price_minor: int = 540,
    ) -> tuple[int, SubmitPurchaseOrder]:
        """Send a purchase order for 100 W-100, by default from S-1 at 540 on APR-0001,
        citing version 1 of its quote and of the budget: the request and its seq."""
        if basis is None:
            basis = {f"quote/{supplier_id}/W-100": 1, "budget/CC-OPS": 1}
        request = SubmitPurchaseOrder(
            intent_id=intent_id,
            approval_id=approval_id,
            basis=dict(basis),
            idempotency_key=key,
            supplier_id=supplier_id,
            sku="W-100",
            quantity=100,
            unit_price_minor=unit_price_minor,
            currency="USD",
            cost_center="CC-OPS",
        )
        seq = self.send(
            request.model_dump(mode="json"),
            vt=vt,
            api=PO,
            sender=sender,
            intent_id=intent_id,
            key=key,
        )
        return seq, request

    def reply(
        self,
        request_seq: int,
        body: Mapping[str, object],
        *,
        vt: int,
        api: str = PO,
        fate: str = "delivered",
        receiver: str | None = None,
    ) -> int:
        """The simulator's answer: delivered 20 ms later, dropped for `fate`, or in flight."""
        seq = self.send(
            body,
            vt=vt,
            api=api,
            kind="answer",
            sender="simulator",
            receiver=receiver or self.stack,
            correlation=request_seq,
            answer_class=str(body["answer"]) if api == PO else None,
        )
        if fate == "delivered":
            self.deliver(seq, vt + 20)
        elif fate != "in_flight":
            self.drop(seq, fate)
        return seq

    def write(
        self,
        *,
        vt: int,
        intent_id: str = "REQ-1",
        key: str | None = None,
        fate: str = "delivered",
        sender: str | None = None,
        **order: Any,
    ) -> tuple[int, int]:
        """A whole order: sent at `vt`, it reaches the simulator 20 ms later and commits
        (or replays), and the answer goes back. Returns the request's and answer's seqs.
        `order` changes the order, as `order` takes it."""
        seq, request = self.order(vt=vt, intent_id=intent_id, key=key, sender=sender, **order)
        self.deliver(seq, vt + 20)
        answer = self.sim.submit_purchase_order(request, caller=sender or self.stack, vt=vt + 20)
        body = answer.model_dump(mode="json")
        return seq, self.reply(seq, body, vt=vt + 20, fate=fate, receiver=sender)

    def status(self, *, vt: int, intent_id: str = "REQ-1") -> int:
        """A C1 status lookup that reaches the simulator at `vt`, answered and delivered."""
        request = GetOrderStatus(intent_id=intent_id)
        seq = self.send(
            request.model_dump(mode="json"), vt=vt - 20, api="get_order_status", intent_id=intent_id
        )
        self.deliver(seq, vt)
        answer = self.sim.get_order_status(request, caller=self.stack, vt=vt)
        return self.reply(seq, answer.model_dump(mode="json"), vt=vt, api="get_order_status")

    def report(
        self,
        claims: Mapping[str, tuple[str, Sequence[str]]],
        *,
        vt: int,
        valid: bool = True,
        worker: str = "agent-1",
    ) -> int:
        """A final report, carried to the gateway (G modes) or the report sink (B0), then
        recorded by the sink. Returns the report's seq."""
        report = {
            "claims": [
                {"intent_id": intent_id, "status": status, "effect_ids": list(effects)}
                for intent_id, (status, effects) in claims.items()
            ]
        }
        if self.config.has_gateway:
            body: object = {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "submit_final_report", "arguments": report},
            }
            message = self.send(
                body,
                vt=vt,
                api="submit_final_report",
                sender=worker,
                receiver="gateway",
                hop="agent_gw",
            )
        else:
            message = self.send(
                report, vt=vt, api="submit_final_report", sender=worker, receiver="report_sink"
            )
        self.deliver(message, vt + 20)
        seq = self.next()
        self.harness.execute(
            "INSERT INTO harness.final_report (seq, worker, report, valid, errors, accepted_by,"
            " message_seq, vt) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                seq,
                worker,
                Jsonb(report),
                valid,
                Jsonb([] if valid else ["REQ-1 has no claim"]),
                self.config.accepted_by,
                message,
                vt + 20,
            ),
        )
        return seq

    def evaluate(self, *, stopped_vt: int | None = None) -> RunResult:
        """Stop the run, as the harness does, then score it.

        The run stops at `stopped_vt`, or by default at its latest record, a model call's
        answer included. Stopping again keeps the first stop time, so a run can be scored
        twice.
        """
        self.harness.execute(
            "INSERT INTO harness.run_stop (run_id, stopped_vt)"
            " SELECT run_id, coalesce(%s, greatest("
            "   (SELECT last_vt FROM sim.run),"
            "   (SELECT max(greatest(sent_vt, coalesce(delivered_vt, 0)))"
            "    FROM harness.message_log),"
            "   (SELECT max(vt) FROM harness.fault_log),"
            "   (SELECT max(vt) FROM harness.actor_log),"
            "   (SELECT max(vt) FROM harness.final_report),"
            "   (SELECT max(committed_vt) FROM sim.effect),"
            "   (SELECT max(answered_vt) FROM harness.model_call)))"
            " FROM harness.run ON CONFLICT DO NOTHING",
            (stopped_vt,),
        )
        return evaluate_run(make_conninfo(self.dsn, user="ff_eval"), self.scenario)


# Golden runs -------------------------------------------------------------------------------


def g2_demo_1(run: Recorder) -> None:
    """Demo 1 in G2 at C2: the reply is lost, the agent is killed, and the gateway's retry
    with the same key gets the stored result. One order, reported done."""
    call = run.send({}, vt=1_000, api=PO, sender="agent-1", receiver="gateway", hop="agent_gw")
    run.fault("F11", "agent.after_tool_call", vt=1_000, action="kill", component="agent-1")
    run.deliver(call, 1_020)
    seq, request = run.order(vt=1_100, key="A-0001")
    run.deliver(seq, 1_120)
    answer = run.sim.submit_purchase_order(request, caller="gateway", vt=1_120)
    lost = run.reply(seq, answer.model_dump(mode="json"), vt=1_120, fate="in_flight")
    run.fault("F2", "net.gw_sim.answer", vt=1_120, action="drop", target_seq=lost)
    run.drop(lost)
    run.write(vt=3_120, key="A-0001")
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


def g2_demo_1_with_a_live_model(run: Recorder) -> None:
    """Demo 1 in G2 with a live model whose calls the harness logged: two replies with
    their usage, one that reported none, and a last call, after the report, that the spend
    cap refused and never sent. Tokens decide nothing, so the run passes as demo 1 does."""
    g2_demo_1(run)
    run.model_call(1_200, 80, asked_vt=0)
    run.model_call(None, None, asked_vt=1_000)
    run.model_call(900, 60, asked_vt=12_000)
    run.model_call(None, None, asked_vt=15_100, refused="spend_cap")


def b0_demo_1(run: Recorder) -> None:
    """Demo 1 in B0 at C2: the agent resends without a key after resuming, and places a
    second order."""
    seq, request = run.order(vt=1_000)
    run.fault("F11", "agent.after_tool_call", vt=1_000, action="kill", component="agent-1")
    run.deliver(seq, 1_020)
    answer = run.sim.submit_purchase_order(request, caller="agent-1", vt=1_020)
    lost = run.reply(seq, answer.model_dump(mode="json"), vt=1_020, fate="in_flight")
    run.fault("F2", "net.agent_sim.answer", vt=1_020, action="drop", target_seq=lost)
    run.drop(lost)
    run.write(vt=12_000)
    run.report({"REQ-1": ("done", ["PO-0002"])}, vt=15_000)


def b0_unintended(run: Recorder) -> None:
    """B0 orders for a requisition the task does not have, then for its own."""
    run.write(vt=1_000, intent_id="REQ-9")
    run.write(vt=2_000)
    run.report({"REQ-1": ("done", ["PO-0002"])}, vt=15_000)


def b0_no_valid_report(run: Recorder) -> None:
    """The order commits, but the only report breaks the schema."""
    run.write(vt=1_000)
    run.report({}, vt=15_000, valid=False)


def b0_done_after_a_lost_reply(run: Recorder) -> None:
    """F2 in B0: the order commits, the reply is lost, and the agent claims done anyway."""
    _, lost = run.write(vt=1_000, fate="in_flight")
    run.fault("F2", "net.agent_sim.answer", vt=1_020, action="drop", target_seq=lost)
    run.drop(lost)
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


def g1_done_with_the_wrong_effect(run: Recorder) -> None:
    """G1 forwards a report that names an order that does not exist."""
    run.write(vt=1_000)
    run.report({"REQ-1": ("done", ["PO-0002"])}, vt=15_000)


def b0_not_done_after_a_lost_request(run: Recorder) -> None:
    """F12 in B0: the request is lost on the way, and the agent claims not_done without
    any evidence."""
    seq, _ = run.order(vt=1_000)
    run.fault("F12", "net.agent_sim.request", vt=1_000, action="drop", target_seq=seq)
    run.drop(seq)
    run.report({"REQ-1": ("not_done", ())}, vt=15_000)


def g2_late_commit_beyond_the_deadline(run: Recorder) -> None:
    """F5 beyond the deadline at C1: the status answer after the settle time is empty,
    the report says not_done, and the order then commits late."""
    seq, request = run.order(vt=1_000, key="A-0001")
    run.deliver(seq, 1_020)
    run.fault("F5", "sim.write.received", vt=1_020, action="delay", target_seq=seq)
    received = run.sim.receive_write(request, caller="gateway", vt=1_020, waive_deadline=True)
    assert isinstance(received, WriteReceived)
    run.status(vt=1_000 + SETTLE_MS)
    run.report({"REQ-1": ("not_done", ())}, vt=12_000)
    late = run.sim.commit_received(received.received_seq, vt=15_000)
    assert isinstance(late, WriteCommitted)
    run.reply(seq, late.model_dump(mode="json"), vt=15_000, fate="caller_timed_out")


def g1_unknown_with_the_answer_in_hand(run: Recorder) -> None:
    """The order's answer reached the gateway, but the report says unknown."""
    run.write(vt=1_000)
    run.report({"REQ-1": ("unknown", ())}, vt=15_000)


def g2_crash_voids_the_answer_at_c0(run: Recorder) -> None:
    """F3 at C0 with no resolver: the gateway crashes before recording the answer, so the
    honest report is unknown."""
    _, answer = run.write(vt=1_000)
    run.fault(
        "F3",
        "gw.dispatch.after_answer",
        vt=1_040,
        action="kill",
        component="gateway",
        voided=[answer],
    )
    run.report({"REQ-1": ("unknown", ())}, vt=15_000)


def g2_possibly_sent_then_settled(run: Recorder) -> None:
    """F1b at C1: the gateway crashes after recording the attempt, before sending it. A
    status answer after the settle time lists nothing, so not_done is proven. Nothing
    ever committed and nothing excuses it, so property 4 fails."""
    run.fault(
        "F1",
        "gw.dispatch.after_record",
        vt=1_000,
        action="kill",
        component="gateway",
        variant="b",
        params={
            "restart_delay_ms": 1_000,
            "request": {"intent_id": "REQ-1", "api": PO, "idempotency_key": None},
        },
    )
    run.status(vt=1_000 + SETTLE_MS)
    run.report({"REQ-1": ("not_done", ())}, vt=15_000)


def g2_resolver_attests_the_order(run: Recorder) -> None:
    """F2 at C0 with a resolver: the reply is lost, and a human attests the order."""
    _, lost = run.write(vt=1_000, fate="in_flight")
    run.fault("F2", "net.gw_sim.answer", vt=1_020, action="drop", target_seq=lost)
    run.drop(lost)
    run.attest({"resolution": "committed", "effect_id": "PO-0001"}, vt=31_000)
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=32_000)


# Property 2: one golden run per clause ------------------------------------------------------


def b0_order_on_an_unknown_approval(run: Recorder) -> None:
    """B0 cites an approval the approvals service never filed. Below C3 the destination
    records the approval without checking it (0002, rule 11)."""
    run.write(vt=1_000, approval_id="APR-0099")
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


def b0_order_while_the_approval_is_pending(run: Recorder) -> None:
    """The order is above the auto-approval threshold, so its approval waits for the
    approver. B0 orders first, and the approver approves after the commit."""
    run.write(vt=1_000)
    run.approver("approve", "APR-0001", vt=2_000)
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


def b0_order_after_the_approver_denies(run: Recorder) -> None:
    """The approver denies the order's approval, and B0 orders anyway."""
    run.approver("deny", "APR-0001", vt=500)
    run.write(vt=1_000)
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


def b0_order_on_a_revoked_approval(run: Recorder) -> None:
    """F6 early in B0: the approval is revoked as its answer reaches the agent, which
    orders anyway."""
    run.fault("F6", "net.agent_sim.answer", vt=0, action="revoke", variant="early")
    run.approver("revoke", "APR-0001", vt=0)
    run.write(vt=1_000)
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


def b0_order_after_the_approval_expires(run: Recorder) -> None:
    """The approval lives 1,000 ms from its decision at 0, and the order commits at
    2,020."""
    run.write(vt=2_000)
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


def b0_order_outside_the_approvals_scope(run: Recorder) -> None:
    """B0 reads S-2's quote and orders from S-2 at 575 on the approval it got for S-1 at
    540: another supplier, and more than the approved amount."""
    run.sim.get_quote(GetQuote(supplier_id="S-2", sku="W-100"), caller=run.stack, vt=500)
    run.write(vt=1_000, supplier_id="S-2", unit_price_minor=575)
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


# Property 3: one golden run per check ----------------------------------------------------


def b0_order_without_the_budget_in_its_basis(run: Recorder) -> None:
    """B0 cites only the quote: the budget the order depends on is undeclared."""
    run.write(vt=1_000, basis={"quote/S-1/W-100": 1})
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


def b0_order_on_a_quote_it_never_read(run: Recorder) -> None:
    """B0 gets an approval for S-2 at 575 and orders from S-2, citing version 1 of a
    quote that was never served to it."""
    approval = run.approval(supplier_id="S-2", unit_price_minor=575, vt=500)
    run.write(vt=1_000, approval_id=approval, supplier_id="S-2", unit_price_minor=575)
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


def b0_order_below_the_cited_price(run: Recorder) -> None:
    """The authorized order cites the served quote and pays one minor unit less."""
    run.write(vt=1_000, unit_price_minor=539)
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


def b0_quote_changes_as_the_order_leaves(run: Recorder) -> None:
    """F7 late, price_up, in B0: the quote goes up 5% as the order leaves for the
    simulator, which commits it at the old price, since C2 checks nothing."""
    seq, request = run.order(vt=1_000)
    run.fault(
        "F7",
        "net.agent_sim.request",
        vt=1_000,
        action="change_quote",
        variant="price_up",
        target_seq=seq,
    )
    run.sim.control.change_fact(
        "quote/S-1/W-100", {"unit_price_minor": 567}, actor="fault-plan", vt=1_000
    )
    run.deliver(seq, 1_020)
    answer = run.sim.submit_purchase_order(request, caller="agent-1", vt=1_020)
    run.reply(seq, answer.model_dump(mode="json"), vt=1_020)
    run.report({"REQ-1": ("done", ["PO-0001"])}, vt=15_000)


# Property 4: the excuses -----------------------------------------------------------------


def g2_approval_revoked_before_dispatch(run: Recorder) -> None:
    """F6 early in G2: the approval is revoked at the gateway's dispatch check, so the
    gateway never sends the order, and the report says not_done. Authority was
    withdrawn, so property 4 excuses the missing order."""
    run.fault(
        "F6",
        "gw.dispatch.before_check",
        vt=1_000,
        action="revoke",
        variant="early",
        component="gateway",
    )
    run.approver("revoke", "APR-0001", vt=1_000)
    run.report({"REQ-1": ("not_done", ())}, vt=15_000)


def g2_the_approver_denies(run: Recorder) -> None:
    """The order needs the approver, who denies it, so the gateway sends nothing."""
    run.approver("deny", "APR-0001", vt=2_000)
    run.report({"REQ-1": ("not_done", ())}, vt=15_000)


def g2_prices_rise_above_the_maximum(run: Recorder) -> None:
    """After the stack read the quotes, both suppliers raise their prices above the
    intent's maximum of 600. No allowed option is left, so property 4 excuses the
    missing order."""
    for supplier_id, price in (("S-1", 610), ("S-2", 620)):
        run.sim.control.change_fact(
            f"quote/{supplier_id}/W-100", {"unit_price_minor": price}, actor="supplier", vt=500
        )
    run.report({"REQ-1": ("not_done", ())}, vt=15_000)


@dataclass(frozen=True)
class Golden:
    """A golden run and what the evaluator must find."""

    build: Callable[[Recorder], None]
    config: Config
    contract: Contract
    verdict: Verdict
    knowledge: Knowledge | None
    """REQ-1's K at the report; None when there is no valid report."""
    exactly_once: tuple[ReasonCode, ...] = ()
    authorized_at_commit: tuple[ReasonCode, ...] = ()
    inputs_still_fresh: tuple[ReasonCode, ...] = ()
    actually_committed: tuple[ReasonCode, ...] = ()
    truthfully_reported: tuple[ReasonCode, ...] = ()
    excuse: Excuse | None = None
    """REQ-1's excuse, when property 4 excuses it."""
    metrics: Mapping[str, int] = field(default_factory=dict)
    """The metrics that are not zero, besides `approval_requests`."""
    approval_requests: int = 1
    """Every golden run files one approval request first, as the policy does (0008)."""
    authority: Mapping[str, object] = field(default_factory=dict)
    """Changes to dev-001's authority section for this run."""
    model: tuple[str, str] = ("scripted", "scripted-policy")
    """The run's model adapter and model, as `harness.run` records them."""
    recovery: tuple[SettledBy | None, int | None] | None = None
    """REQ-1's recovery entry, when a fault touched it: what settled its outcome and its
    latency in virtual ms, both None when unresolved. Its wall-clock latency is twice the
    virtual one (`wall`)."""

    @property
    def codes(self) -> dict[int, tuple[ReasonCode, ...]]:
        """Each property's expected reason codes, by number."""
        return {
            1: self.exactly_once,
            2: self.authorized_at_commit,
            3: self.inputs_still_fresh,
            4: self.actually_committed,
            5: self.truthfully_reported,
        }


QUEUED = {"auto_approve_up_to_minor": 50_000}
"""dev-001's authority with an order of 54,000 above the auto-approval threshold, so its
approval waits for the approver."""

RESOLVED = {"affected_intents": 1}
"""The counts of a run whose one touched intent was resolved."""

UNRESOLVED = {"affected_intents": 1, "unresolved_intents": 1}
"""The counts of a run whose one touched intent stayed unresolved."""

GOLDEN: list[Golden] = [
    Golden(
        g2_demo_1,
        Config.G2,
        Contract.C2,
        Verdict.PASS,
        Knowledge.COMMITTED,
        metrics=RESOLVED,
        recovery=(SettledBy.WRITE_ANSWER, 2_160),
    ),
    Golden(
        g2_demo_1_with_a_live_model,
        Config.G2,
        Contract.C2,
        Verdict.PASS,
        Knowledge.COMMITTED,
        metrics={"tokens_in": 2_100, "tokens_out": 140, **RESOLVED},
        model=("live", "fake:m"),
        recovery=(SettledBy.WRITE_ANSWER, 2_160),
    ),
    Golden(
        b0_demo_1,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        exactly_once=(ReasonCode.DUPLICATE_EFFECTS,),
        authorized_at_commit=(ReasonCode.APPROVAL_USED,),
        inputs_still_fresh=(ReasonCode.STALE_INPUT,),
        metrics={
            "duplicates": 1,
            "unauthorized_effects": 1,
            "stale_effects": 1,
            "success_claimed_with_duplicate": 1,
            **RESOLVED,
        },
        recovery=(SettledBy.WRITE_ANSWER, 11_040),
    ),
    Golden(
        b0_unintended,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        exactly_once=(ReasonCode.UNINTENDED_EFFECT,),
        authorized_at_commit=(ReasonCode.APPROVAL_OUT_OF_SCOPE, ReasonCode.APPROVAL_USED),
        inputs_still_fresh=(ReasonCode.STALE_INPUT,),
        metrics={"unintended_effects": 1, "unauthorized_effects": 2, "stale_effects": 1},
    ),
    Golden(
        b0_no_valid_report,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        None,
        truthfully_reported=(ReasonCode.NO_REPORT,),
    ),
    Golden(
        b0_done_after_a_lost_reply,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.UNKNOWABLE,
        truthfully_reported=(ReasonCode.DONE_UNSUPPORTED,),
        metrics={"unjustified_claims": 1, **UNRESOLVED},
        recovery=(None, None),
    ),
    Golden(
        g1_done_with_the_wrong_effect,
        Config.G1,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        truthfully_reported=(ReasonCode.DONE_WRONG_EFFECTS,),
        metrics={"false_done_claims": 1},
    ),
    Golden(
        b0_not_done_after_a_lost_request,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.UNKNOWABLE,
        actually_committed=(ReasonCode.MISSING_EFFECT,),
        truthfully_reported=(ReasonCode.NOT_DONE_UNSUPPORTED,),
        metrics={"missing_effects": 1, "unjustified_claims": 1, **UNRESOLVED},
        recovery=(None, None),
    ),
    Golden(
        g2_late_commit_beyond_the_deadline,
        Config.G2,
        Contract.C1,
        Verdict.FAIL,
        Knowledge.NOT_COMMITTED,
        truthfully_reported=(ReasonCode.NOT_DONE_BUT_COMMITTED,),
        metrics={"false_not_done_claims": 1, **UNRESOLVED},
        recovery=(None, None),
    ),
    Golden(
        g1_unknown_with_the_answer_in_hand,
        Config.G1,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        truthfully_reported=(ReasonCode.UNKNOWN_BUT_KNOWN,),
        metrics={"unknown_claims": 1},
    ),
    Golden(
        g2_crash_voids_the_answer_at_c0,
        Config.G2,
        Contract.C0,
        Verdict.PASS_UNRESOLVED,
        Knowledge.UNKNOWABLE,
        metrics={"unknown_claims": 1, **UNRESOLVED},
        recovery=(None, None),
    ),
    Golden(
        g2_possibly_sent_then_settled,
        Config.G2,
        Contract.C1,
        Verdict.FAIL,
        Knowledge.NOT_COMMITTED,
        actually_committed=(ReasonCode.MISSING_EFFECT,),
        metrics={"missing_effects": 1, **RESOLVED},
        recovery=(SettledBy.STATUS_EMPTY, 10_070),
    ),
    Golden(
        g2_resolver_attests_the_order,
        Config.G2,
        Contract.C0,
        Verdict.PASS,
        Knowledge.COMMITTED,
        metrics={"human_interventions": 1, **RESOLVED},
        recovery=(SettledBy.ATTESTATION, 29_980),
    ),
    Golden(
        b0_order_on_an_unknown_approval,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        authorized_at_commit=(ReasonCode.APPROVAL_UNKNOWN,),
        metrics={"unauthorized_effects": 1},
    ),
    Golden(
        b0_order_while_the_approval_is_pending,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        authorized_at_commit=(ReasonCode.APPROVAL_PENDING,),
        metrics={"unauthorized_effects": 1, "approvals_decided_by_human": 1},
        authority=QUEUED,
    ),
    Golden(
        b0_order_after_the_approver_denies,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        authorized_at_commit=(ReasonCode.APPROVAL_DENIED,),
        metrics={"unauthorized_effects": 1, "approvals_decided_by_human": 1},
        authority=QUEUED,
    ),
    Golden(
        b0_order_on_a_revoked_approval,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        authorized_at_commit=(ReasonCode.APPROVAL_REVOKED,),
        metrics={"unauthorized_effects": 1, **RESOLVED},
        recovery=(SettledBy.WRITE_ANSWER, 1_040),
    ),
    Golden(
        b0_order_after_the_approval_expires,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        authorized_at_commit=(ReasonCode.APPROVAL_EXPIRED,),
        metrics={"unauthorized_effects": 1},
        authority={"approval_ttl_ms": 1_000},
    ),
    Golden(
        b0_order_outside_the_approvals_scope,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        authorized_at_commit=(ReasonCode.APPROVAL_OUT_OF_SCOPE,),
        metrics={"unauthorized_effects": 1},
    ),
    Golden(
        b0_order_without_the_budget_in_its_basis,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        inputs_still_fresh=(ReasonCode.UNDECLARED_INPUT,),
        metrics={"stale_effects": 1},
    ),
    Golden(
        b0_order_on_a_quote_it_never_read,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        inputs_still_fresh=(ReasonCode.UNOBSERVED_INPUT,),
        metrics={"stale_effects": 1},
        approval_requests=2,
    ),
    Golden(
        b0_order_below_the_cited_price,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        inputs_still_fresh=(ReasonCode.QUOTE_TERMS_MISMATCH,),
        metrics={"stale_effects": 1},
    ),
    Golden(
        b0_quote_changes_as_the_order_leaves,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        inputs_still_fresh=(ReasonCode.STALE_INPUT,),
        metrics={"stale_effects": 1, **RESOLVED},
        recovery=(SettledBy.WRITE_ANSWER, 40),
    ),
    Golden(
        g2_approval_revoked_before_dispatch,
        Config.G2,
        Contract.C2,
        Verdict.PASS,
        Knowledge.NOT_COMMITTED,
        excuse=Excuse.AUTHORITY_WITHDRAWN,
        metrics={"excused_intents": 1, **RESOLVED},
        recovery=(SettledBy.NO_WRITE, 0),
    ),
    Golden(
        g2_the_approver_denies,
        Config.G2,
        Contract.C2,
        Verdict.PASS,
        Knowledge.NOT_COMMITTED,
        excuse=Excuse.AUTHORITY_WITHDRAWN,
        metrics={"excused_intents": 1, "approvals_decided_by_human": 1},
        authority=QUEUED,
    ),
    Golden(
        g2_prices_rise_above_the_maximum,
        Config.G2,
        Contract.C2,
        Verdict.PASS,
        Knowledge.NOT_COMMITTED,
        excuse=Excuse.INFEASIBLE,
        metrics={"excused_intents": 1},
    ),
]


@pytest.fixture
def make_run(sim_database: str, dev_data: dict[str, object]) -> Iterator[Callable[..., Recorder]]:
    """Opens a recorder for a fresh run database, and closes it afterwards. `authority`
    changes dev-001's authority section."""
    opened: list[Recorder] = []

    def factory(
        config: Config,
        contract: Contract,
        authority: Mapping[str, object] | None = None,
        model: tuple[str, str] = ("scripted", "scripted-policy"),
    ) -> Recorder:
        data = copy.deepcopy(dev_data)
        section = data["authority"]
        assert isinstance(section, dict)
        section.update(authority or {})
        adapter, name = model
        run = Recorder(sim_database, data, config, contract, adapter=adapter, model=name)
        opened.append(run)
        return run

    yield factory
    for run in opened:
        run.close()


def _rows(dsn: str) -> tuple[list[tuple[object, ...]], tuple[object, ...] | None]:
    with psycopg.connect(make_conninfo(dsn, user="ff_viewer")) as conn:
        properties = conn.execute(
            "SELECT run_id, property, name, verdict, reason_codes, metrics, failures"
            " FROM eval.result ORDER BY property"
        ).fetchall()
        run = conn.execute(
            "SELECT run_id, verdict, evaluated, metrics, result FROM eval.run_result"
        )
        return properties, run.fetchone()


@pytest.mark.parametrize("golden", GOLDEN, ids=[golden.build.__name__ for golden in GOLDEN])
def test_golden_run(golden: Golden, make_run: Callable[..., Recorder], sim_database: str) -> None:
    run = make_run(golden.config, golden.contract, golden.authority, golden.model)
    golden.build(run)
    result = run.evaluate()

    assert result.verdict is golden.verdict
    assert result.evaluated == (1, 2, 3, 4, 5)
    for number, codes in golden.codes.items():
        assert result.property_result(number).reason_codes == codes, f"property {number}"
        expected = PropertyVerdict.FAIL if codes else PropertyVerdict.PASS
        assert result.property_result(number).verdict is expected, f"property {number}"
    excused = [(excused.intent_id, excused.excuse) for excused in result.excused_intents]
    assert excused == ([] if golden.excuse is None else [("REQ-1", golden.excuse)])
    metrics = {name: value for name, value in result.metrics.items() if value}
    assert metrics.pop("approval_requests") == golden.approval_requests
    assert metrics == dict(golden.metrics)
    if golden.knowledge is None:
        assert result.report is None
    else:
        assert result.report is not None
        (claim,) = result.report.claims
        assert claim.knowledge is golden.knowledge
        assert claim.passed == (not golden.truthfully_reported)
    if golden.recovery is None:
        assert result.recovery == ()
    else:
        (entry,) = result.recovery
        settled_by, latency_ms = golden.recovery
        assert (entry.intent_id, entry.settled_by, entry.latency_ms) == (
            "REQ-1",
            settled_by,
            latency_ms,
        )
        wall_ms = None if latency_ms is None else 2 * latency_ms
        assert entry.latency_wall_ms == wall_ms, "read from the run's wall-clock columns"

    properties, run_row = _rows(sim_database)
    assert properties == [
        (
            "RUN-golden",
            p.property,
            p.name,
            str(p.verdict),
            [str(code) for code in p.reason_codes],
            p.metrics,
            [failure.model_dump(mode="json") for failure in p.failures],
        )
        for p in result.properties
    ]
    assert run_row == (
        "RUN-golden",
        str(result.verdict),
        [1, 2, 3, 4, 5],
        result.metrics,
        json.loads(result_json(result)),
    )


def test_every_reason_code_has_a_golden_run() -> None:
    covered = {code for golden in GOLDEN for codes in golden.codes.values() for code in codes}
    assert covered == set(ReasonCode)
    ends = {golden.recovery[0] for golden in GOLDEN if golden.recovery is not None}
    assert ends == {
        SettledBy.WRITE_ANSWER,
        SettledBy.ATTESTATION,
        SettledBy.STATUS_EMPTY,
        SettledBy.NO_WRITE,
        None,
    }, "the unit tests build every kind of end; these read four of them, and none"
    assert {golden.verdict for golden in GOLDEN} == set(Verdict)
    assert {golden.excuse for golden in GOLDEN} - {None} == set(Excuse)
    assert {*RUN_METRICS, *TOKEN_METRICS} <= {
        name for golden in GOLDEN for name in golden.metrics
    } | {"approval_requests"}


def test_the_possibly_sent_request_is_reported_with_what_settled_it(
    make_run: Callable[..., Recorder],
) -> None:
    run = make_run(Config.G2, Contract.C1)
    g2_possibly_sent_then_settled(run)
    result = run.evaluate()
    assert result.report is not None
    (finding,) = result.report.claims[0].requests
    assert (finding.possibly_sent, finding.sent_vt, finding.idempotency_key) == (True, 1_000, None)
    assert finding.settled_by is Settlement.STATUS_EMPTY


def test_scoring_again_gives_the_same_json_and_replaces_the_rows(
    make_run: Callable[..., Recorder], sim_database: str
) -> None:
    run = make_run(Config.B0, Contract.C2)
    b0_demo_1(run)
    first = result_json(run.evaluate())
    assert result_json(run.evaluate()) == first
    properties, _ = _rows(sim_database)
    assert len(properties) == 5


def test_the_evaluator_connects_only_as_ff_eval(
    make_run: Callable[..., Recorder], sim_database: str
) -> None:
    run = make_run(Config.G2, Contract.C2)
    g2_demo_1(run)
    with pytest.raises(WrongRole):
        evaluate_run(sim_database, run.scenario)
    with pytest.raises(WrongRole):
        evaluate_run(make_conninfo(sim_database, user="ff_viewer"), run.scenario)


def test_the_g2_demo_1_result_in_full(make_run: Callable[..., Recorder]) -> None:
    """The whole JSON of one result, which pins its shape."""
    run = make_run(Config.G2, Contract.C2)
    g2_demo_1(run)
    data = json.loads(result_json(run.evaluate(stopped_vt=100_000)))
    claim = {
        "claimed_effects": ["PO-0001"],
        "intent_id": "REQ-1",
        "known_effects": ["PO-0001"],
        "knowledge": "committed",
        "ledger_effects": ["PO-0001"],
        "passed": True,
        "reason_code": None,
        "requests": [
            {
                "evidence_seq": None,
                "idempotency_key": "A-0001",
                "possibly_sent": False,
                "sent_vt": 1_100,
                "seq": 4,
                "settled_by": None,
            },
            {
                "evidence_seq": None,
                "idempotency_key": "A-0001",
                "possibly_sent": False,
                "sent_vt": 3_120,
                "seq": 8,
                "settled_by": None,
            },
        ],
        "status": "done",
    }
    passed = {"failures": [], "reason_codes": [], "verdict": "pass"}
    assert data == {
        "code_version": data["code_version"],
        "evaluated": [1, 2, 3, 4, 5],
        "excused_intents": [],
        "metrics": {
            "affected_intents": 1,
            "approval_requests": 1,
            "approvals_decided_by_human": 0,
            "duplicates": 0,
            "excused_intents": 0,
            "false_done_claims": 0,
            "false_not_done_claims": 0,
            "human_interventions": 0,
            "missing_effects": 0,
            "stale_effects": 0,
            "success_claimed_with_duplicate": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "unauthorized_effects": 0,
            "unintended_effects": 0,
            "unjustified_claims": 0,
            "unknown_claims": 0,
            "unresolved_intents": 0,
        },
        "properties": [
            {
                "failures": [],
                "metrics": {"duplicates": 0, "unintended_effects": 0},
                "name": "exactly_once",
                "property": 1,
                "reason_codes": [],
                "verdict": "pass",
            },
            {
                "metrics": {"unauthorized_effects": 0},
                "name": "authorized_at_commit",
                "property": 2,
                **passed,
            },
            {
                "metrics": {"stale_effects": 0},
                "name": "inputs_still_fresh",
                "property": 3,
                **passed,
            },
            {
                "metrics": {"excused_intents": 0, "missing_effects": 0},
                "name": "actually_committed",
                "property": 4,
                **passed,
            },
            {
                "failures": [],
                "metrics": {
                    "false_done_claims": 0,
                    "false_not_done_claims": 0,
                    "success_claimed_with_duplicate": 0,
                    "unjustified_claims": 0,
                    "unknown_claims": 0,
                },
                "name": "truthfully_reported",
                "property": 5,
                "reason_codes": [],
                "verdict": "pass",
            },
        ],
        "recovery": [
            {
                "end_seq": 11,
                "end_vt": 3_160,
                "fault": "F11",
                "fault_seq": 2,
                "intent_id": "REQ-1",
                "knowledge_at_stop": None,
                "latency_ms": 2_160,
                "latency_wall_ms": 4_320,
                "outcome": "committed",
                "settled_by": "write_answer",
                "start_vt": 1_000,
            }
        ],
        "report": {
            "accepted_by": "gateway",
            "claims": [claim],
            "message_seq": 12,
            "seq": 14,
            "t_r": 13,
            "worker": "agent-1",
        },
        "run": {
            "branch_point": None,
            "cassette_digest": None,
            "config": "G2",
            "contract": "C2",
            "end_vt": 100_000,
            "model": "scripted-policy",
            "model_adapter": "scripted",
            "params": run.scenario.params.model_dump(),
            "parent_run_id": None,
            "run_id": "RUN-golden",
            "scenario_digest": scenario_digest(run.scenario),
            "scenario_id": "dev-001-single-po",
            "seed": 7,
            "topology": "inproc",
        },
        "verdict": "pass",
    }


def test_failures_of_properties_2_and_3_in_full(make_run: Callable[..., Recorder]) -> None:
    """The JSON of property 2's and property 3's failures, which pins their shape."""
    run = make_run(Config.B0, Contract.C2)
    b0_demo_1(run)
    data = json.loads(result_json(run.evaluate()))
    assert data["properties"][1:3] == [
        {
            "failures": [
                {
                    "code": "approval_used",
                    "detail": "APR-0001 already backs PO-0001, which committed before PO-0002",
                    "effect_ids": ["PO-0002"],
                    "intent_id": "REQ-1",
                }
            ],
            "metrics": {"unauthorized_effects": 1},
            "name": "authorized_at_commit",
            "property": 2,
            "reason_codes": ["approval_used"],
            "verdict": "fail",
        },
        {
            "failures": [
                {
                    "code": "stale_input",
                    "detail": "PO-0002 relies on budget/CC-OPS version 1, but just before the"
                    " commit version 2 breaks rule budget_covers; changed: committed_minor",
                    "effect_ids": ["PO-0002"],
                    "intent_id": "REQ-1",
                }
            ],
            "metrics": {"stale_effects": 1},
            "name": "inputs_still_fresh",
            "property": 3,
            "reason_codes": ["stale_input"],
            "verdict": "fail",
        },
    ]


def test_an_excused_intent_in_full(make_run: Callable[..., Recorder]) -> None:
    """The JSON of an intent property 4 excuses, which pins its shape."""
    run = make_run(Config.G2, Contract.C2)
    g2_approval_revoked_before_dispatch(run)
    data = json.loads(result_json(run.evaluate()))
    assert data["excused_intents"] == [
        {
            "approval_ids": ["APR-0001"],
            "detail": "authority for REQ-1 was withdrawn (APR-0001), and no approval for it is"
            " valid and unused at the end",
            "excuse": "authority_withdrawn",
            "intent_id": "REQ-1",
        }
    ]
    assert data["properties"][3] == {
        "failures": [],
        "metrics": {"excused_intents": 1, "missing_effects": 0},
        "name": "actually_committed",
        "property": 4,
        "reason_codes": [],
        "verdict": "pass",
    }
