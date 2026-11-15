"""Golden runs: hand-built ground truth in a run database, with expected results (0004).

Each golden run is written as the owning roles would write it: the simulator's ground
truth through the simulator itself, as ff_sim, and the harness's records by hand, as
ff_harness, numbering every send, delivery, fault, actor action and report on one
counter. The evaluator then reads the run as ff_eval. There is at least one golden run
per reason code (0004, Checks), and one for each passing verdict.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

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
    RequestApproval,
    SubmitPurchaseOrder,
    WriteCommitted,
    WriteReceived,
)
from fivefold.domain.contracts import Contract
from fivefold.domain.rules import Operation
from fivefold.domain.scenario import Scenario, scenario_digest, scenario_from_data
from fivefold.evaluator import (
    Config,
    Knowledge,
    PropertyVerdict,
    ReasonCode,
    RunResult,
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


class Recorder:
    """Writes one run's records to its database, in event order."""

    def __init__(self, dsn: str, data: Mapping[str, object], config: Config, contract: Contract):
        self.dsn = dsn
        self.config = config
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
            " VALUES ('RUN-golden', %s, %s, %s, %s, 7, 'inproc', 'scripted',"
            " 'scripted-policy', %s)",
            (
                self.scenario.id,
                scenario_digest(self.scenario),
                str(config),
                str(contract),
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
            " delivered_vt = %s WHERE seq = %s",
            (delivered, vt, seq),
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
    ) -> int:
        seq = self.next()
        self.harness.execute(
            "INSERT INTO harness.fault_log (seq, fault, variant, point, occurrence, action,"
            " target_seq, component, voided, params, vt)"
            " VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s)",
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
            ),
        )
        return seq

    def attest(self, details: Mapping[str, object], *, vt: int) -> int:
        seq = self.next()
        self.harness.execute(
            "INSERT INTO harness.actor_log (seq, actor, action, subject, details, vt)"
            " VALUES (%s, 'resolver', 'attest', 'ESC-0001', %s, %s)",
            (seq, Jsonb(dict(details)), vt),
        )
        return seq

    # Calls -----------------------------------------------------------------------------------

    def order(
        self,
        *,
        vt: int,
        intent_id: str = "REQ-1",
        key: str | None = None,
        sender: str | None = None,
    ) -> tuple[int, SubmitPurchaseOrder]:
        """Send a purchase order for 100 W-100 from S-1 at 540: the request and its seq."""
        request = SubmitPurchaseOrder(
            intent_id=intent_id,
            approval_id="APR-0001",
            basis={"quote/S-1/W-100": 1, "budget/CC-OPS": 1},
            idempotency_key=key,
            supplier_id="S-1",
            sku="W-100",
            quantity=100,
            unit_price_minor=540,
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
    ) -> tuple[int, int]:
        """A whole order: sent at `vt`, it reaches the simulator 20 ms later and commits
        (or replays), and the answer goes back. Returns the request's and answer's seqs."""
        seq, request = self.order(vt=vt, intent_id=intent_id, key=key, sender=sender)
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

    def evaluate(self) -> RunResult:
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
    status answer after the settle time lists nothing, so not_done is proven."""
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
    truthfully_reported: tuple[ReasonCode, ...] = ()
    metrics: Mapping[str, int] = field(default_factory=dict)
    """The metrics that are not zero."""


GOLDEN: list[Golden] = [
    Golden(g2_demo_1, Config.G2, Contract.C2, Verdict.PASS, Knowledge.COMMITTED),
    Golden(
        b0_demo_1,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        exactly_once=(ReasonCode.DUPLICATE_EFFECTS,),
        metrics={"duplicates": 1, "success_claimed_with_duplicate": 1},
    ),
    Golden(
        b0_unintended,
        Config.B0,
        Contract.C2,
        Verdict.FAIL,
        Knowledge.COMMITTED,
        exactly_once=(ReasonCode.UNINTENDED_EFFECT,),
        metrics={"unintended_effects": 1},
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
        metrics={"unjustified_claims": 1},
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
        truthfully_reported=(ReasonCode.NOT_DONE_UNSUPPORTED,),
        metrics={"unjustified_claims": 1},
    ),
    Golden(
        g2_late_commit_beyond_the_deadline,
        Config.G2,
        Contract.C1,
        Verdict.FAIL,
        Knowledge.NOT_COMMITTED,
        truthfully_reported=(ReasonCode.NOT_DONE_BUT_COMMITTED,),
        metrics={"false_not_done_claims": 1},
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
        metrics={"unknown_claims": 1},
    ),
    Golden(
        g2_possibly_sent_then_settled,
        Config.G2,
        Contract.C1,
        Verdict.PASS,
        Knowledge.NOT_COMMITTED,
    ),
    Golden(
        g2_resolver_attests_the_order,
        Config.G2,
        Contract.C0,
        Verdict.PASS,
        Knowledge.COMMITTED,
    ),
]


@pytest.fixture
def make_run(sim_database: str, dev_data: dict[str, object]) -> Iterator[Callable[..., Recorder]]:
    """Opens a recorder for a fresh run database, and closes it afterwards."""
    opened: list[Recorder] = []

    def factory(config: Config, contract: Contract) -> Recorder:
        run = Recorder(sim_database, dev_data, config, contract)
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
    run = make_run(golden.config, golden.contract)
    golden.build(run)
    result = run.evaluate()

    assert result.verdict is golden.verdict
    assert result.evaluated == (1, 5)
    assert result.property_result(1).reason_codes == golden.exactly_once
    assert result.property_result(5).reason_codes == golden.truthfully_reported
    for number, codes in ((1, golden.exactly_once), (5, golden.truthfully_reported)):
        expected = PropertyVerdict.FAIL if codes else PropertyVerdict.PASS
        assert result.property_result(number).verdict is expected
    assert {name: value for name, value in result.metrics.items() if value} == dict(golden.metrics)
    if golden.knowledge is None:
        assert result.report is None
    else:
        assert result.report is not None
        (claim,) = result.report.claims
        assert claim.knowledge is golden.knowledge
        assert claim.passed == (not golden.truthfully_reported)

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
        [1, 5],
        result.metrics,
        json.loads(result_json(result)),
    )


def test_every_reason_code_has_a_golden_run() -> None:
    covered = {
        code for golden in GOLDEN for code in (*golden.exactly_once, *golden.truthfully_reported)
    }
    assert covered == set(ReasonCode)
    assert {golden.verdict for golden in GOLDEN} == set(Verdict)


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
    data = json.loads(result_json(run.evaluate()))
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
    not_evaluated = {"failures": [], "metrics": {}, "reason_codes": [], "verdict": "not_evaluated"}
    assert data == {
        "code_version": data["code_version"],
        "evaluated": [1, 5],
        "excused_intents": None,
        "metrics": {
            "duplicates": 0,
            "false_done_claims": 0,
            "false_not_done_claims": 0,
            "success_claimed_with_duplicate": 0,
            "unintended_effects": 0,
            "unjustified_claims": 0,
            "unknown_claims": 0,
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
            {"name": "authorized_at_commit", "property": 2, **not_evaluated},
            {"name": "inputs_still_fresh", "property": 3, **not_evaluated},
            {"name": "actually_committed", "property": 4, **not_evaluated},
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
