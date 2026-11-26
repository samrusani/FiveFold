"""G1's tools, called over MCP on the scheduler (0008, "G1: authorization only").

G1 exposes G2's tools with G2's schemas; only the behaviour differs. A stand-in simulator
answers reads, approvals and writes in memory. Every write commits a new order, because
G1 sends no idempotency key, and the stand-in can lose an answer after the commit. These
tests pin what the agent sees from G1: a new action per call, `gateway_timeout` for a
send with no answer, which the agent's tool layer raises so that the framework retries,
and a final report handed to the report sink unchanged and unchecked.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import pytest
from langgraph.types import RetryPolicy
from mcp.client.session import ClientSession

from fivefold.agents.connection import GatewayConnection
from fivefold.agents.tools import ServerError, Timeouts, gateway_tools
from fivefold.domain.api import FactRead, GetBudget, GetQuote
from fivefold.domain.contracts import Contract, ContractParams
from fivefold.faults.plan import FaultInjector, parse_plan
from fivefold.gateway.destination import ApprovalNow, FactNow, Unreachable
from fivefold.gateway.dispatcher import Dispatcher
from fivefold.gateway.freshness import RuleFreshness
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.records import (
    Action,
    Admission,
    Answer,
    ApprovalStatus,
    Attempt,
    ProbeAnswer,
)
from fivefold.gateway.server import serve
from fivefold.gateway.sim_client import ReadRequest
from fivefold.gateway.state_machine import ActionState, AnswerClass, GatewaySettings, Reason
from fivefold.gateway.tools import TOOLS, GatewayTools
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.mcp_link import Link
from fivefold.harness.reports import ReportSink
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Transport

S = ActionState
QUOTE, BUDGET = "quote/S-1/W-100", "budget/CC-OPS"
SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
)
INTENTS = {"REQ-1": "submit_purchase_order"}
ORDER: dict[str, object] = {
    "intent_id": "REQ-1",
    "supplier_id": "S-1",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 540,
    "currency": "USD",
    "cost_center": "CC-OPS",
}
HOP = 10


class FakeSim:
    """Facts, approvals and a ledger with no idempotency, with a fixed delay per call."""

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self.facts_now: dict[str, tuple[int, dict[str, object]]] = {
            QUOTE: (1, {"unit_price_minor": 540, "currency": "USD", "status": "active"}),
            BUDGET: (1, {"limit_minor": 100_000, "committed_minor": 0}),
        }
        self.approvals: dict[str, ApprovalStatus] = {}
        self.ledger: list[tuple[str, str]] = []
        self.lose_answers = 0
        self.fact_checks = 0
        self.slow: set[str] = set()
        """Actions whose answer comes 50 ms later than the others'."""

    async def read(self, request: ReadRequest) -> FactRead:
        await self.scheduler.sleep(2 * HOP)
        key = QUOTE if isinstance(request, GetQuote) else BUDGET
        assert isinstance(request, GetQuote | GetBudget)
        version, value = self.facts_now[key]
        return FactRead(key=key, version=version, data=value)  # type: ignore[arg-type]

    async def facts(self, keys: Sequence[str]) -> Mapping[str, FactNow]:
        self.fact_checks += 1
        await self.scheduler.sleep(2 * HOP)
        return {k: FactNow(k, *self.facts_now[k]) for k in keys}

    async def request_approval(self, action: Action) -> ApprovalNow:
        await self.scheduler.sleep(2 * HOP)
        approval_id = f"APR-{action.action_id[2:]}"
        self.approvals.setdefault(approval_id, ApprovalStatus.APPROVED)
        return await self.approval(action, approval_id)

    async def approval(self, action: Action, approval_id: str) -> ApprovalNow:
        status = self.approvals[approval_id]
        expires = 10**9 if status is ApprovalStatus.APPROVED else None
        return ApprovalNow(approval_id, status, expires)

    async def send(self, action: Action, attempt: Attempt, approval_id: str) -> Answer:
        await self.scheduler.sleep(HOP + (50 if action.action_id in self.slow else 0))
        effect = f"PO-{len(self.ledger) + 1:04d}"
        self.ledger.append((effect, approval_id))
        if self.lose_answers:
            self.lose_answers -= 1
            await self.scheduler.sleep(SETTINGS.dispatch_timeout_ms - HOP)
            return Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)
        await self.scheduler.sleep(HOP)
        return Answer(AnswerClass.COMMITTED, Reason.COMMITTED, effect)

    async def lookup(self, action: Action) -> ProbeAnswer:
        raise Unreachable("G1 never asks")


class Client:
    """The agent's end, calling tools directly through the MCP session."""

    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, name: str, **arguments: object) -> tuple[bool, dict[str, Any]]:
        result = await self.session.call_tool(name, arguments)
        text = result.content[0]
        assert text.type == "text"
        return result.is_error, json.loads(text.text)

    async def ok(self, name: str, **arguments: object) -> dict[str, Any]:
        failed, body = await self.call(name, **arguments)
        assert not failed, body
        return body

    async def cite(self) -> list[str]:
        quote = await self.ok("get_quote", supplier_id="S-1", sku="W-100")
        budget = await self.ok("get_budget", cost_center="CC-OPS")
        return [quote["read_id"], budget["read_id"]]


class Run:
    """A G1 gateway: the store with action identity off, at `contract`."""

    def __init__(
        self, contract: Contract = Contract.C2, plan: list[dict[str, object]] | None = None
    ) -> None:
        self.scheduler = Scheduler()
        self.log = MemoryHarnessLog()
        self.transport = Transport(
            self.scheduler,
            self.log,
            Generators(7).get("latency"),
            max_hop_latency_ms=50,
            faults=FaultInjector(parse_plan(plan or [])),
        )
        self.sim = FakeSim(self.scheduler)
        self.store = MemoryStore(
            task_id="TASK-1",
            intents=INTENTS,
            contract=contract,
            settings=SETTINGS,
            identity=False,
        )
        self.dispatcher = Dispatcher(
            self.scheduler, self.store, self.sim, RuleFreshness(self.store)
        )
        self.tools = GatewayTools(
            self.scheduler,
            self.store,
            self.dispatcher,
            self.sim,
            ReportSink(self.scheduler, self.log, INTENTS),
            task_id="TASK-1",
            intents=INTENTS,
        )

    def link(self) -> Link:
        link = Link(self.scheduler, self.transport, agent="agent-1", gateway="gateway")
        self.scheduler.spawn("gateway", serve(self.tools, link), name="mcp server")
        return link

    def go(self, agent: Callable[[Client], Awaitable[None]]) -> None:
        async def worker(link: Link) -> None:
            async with ClientSession(link.client_read, link.client_write) as session:
                await session.initialize()
                await agent(Client(session))
            link.server_read.close()

        async def main() -> None:
            self.scheduler.spawn("agent-1", worker(self.link()), name="agent")
            await self.scheduler.run(deadline_ms=120_000)

        asyncio.run(main())


def test_g1_lists_the_same_tools_with_the_same_schemas() -> None:
    run = Run()
    listed: list[Any] = []

    async def agent(client: Client) -> None:
        listed.extend((await client.session.list_tools()).tools)

    run.go(agent)
    assert [(t.name, t.input_schema) for t in listed] == [(t.name, t.input_schema()) for t in TOOLS]


def test_the_same_g1_order_twice_is_two_actions_and_two_orders() -> None:
    """Rules 1 and 2: each call files its own approval, is checked and sent once."""
    run = Run()
    seen: list[dict[str, Any]] = []

    async def agent(client: Client) -> None:
        based_on = await client.cite()
        for _ in range(2):
            seen.append(await client.ok("submit_purchase_order", **ORDER, based_on=based_on))

    run.go(agent)
    assert [(r["action_id"], r["state"], r["effect_id"]) for r in seen] == [
        ("A-0001", "confirmed", "PO-0001"),
        ("A-0002", "confirmed", "PO-0002"),
    ]
    assert run.sim.ledger == [("PO-0001", "APR-0001"), ("PO-0002", "APR-0002")]
    assert run.sim.fact_checks == 0, "rule 3: G1 checks no fact"
    snapshot = run.store.snapshot()
    assert [p.admission for p in snapshot.proposals] == [Admission.ADMITTED] * 2
    assert [c.tool for c in snapshot.tool_calls] == ["submit_purchase_order"] * 2


def test_a_g1_order_with_no_answer_fails_as_a_proxys_504_and_stays_unknown() -> None:
    """Rule 5: the call fails with `gateway_timeout`, and the action stays OutcomeUnknown.
    Asked later, the gateway reports the state truthfully, with nothing to reconcile."""
    run = Run()
    run.sim.lose_answers = 1
    seen: list[tuple[bool, dict[str, Any]]] = []

    async def agent(client: Client) -> None:
        based_on = await client.cite()
        seen.append(await client.call("submit_purchase_order", **ORDER, based_on=based_on))
        seen.append(await client.call("get_action_status", action_id="A-0001", wait_ms=5_000))

    run.go(agent)
    (failed, error), (status_failed, status) = seen
    assert (failed, error["error"]) == (True, "gateway_timeout")
    assert error["message"] == (
        "submit_purchase_order for REQ-1: no usable answer from the simulator (timeout)"
    )
    assert not status_failed
    assert (status["state"], status["reason"], status["effect_id"]) == (
        "outcome_unknown",
        "timeout",
        None,
    )
    assert "reconcile" not in status
    snapshot = run.store.snapshot()
    assert snapshot.actions["A-0001"].state is S.OUTCOME_UNKNOWN and not snapshot.escalations
    assert [c.tool for c in snapshot.tool_calls] == ["get_action_status"], (
        "the 504 reports no state, so only the status answer is logged"
    )
    assert run.sim.ledger == [("PO-0001", "APR-0001")], "the order committed; G1 cannot know"


def test_the_agents_tool_layer_raises_gateway_timeout_so_the_framework_retries() -> None:
    """0008: the agent's tool layer raises G1's `gateway_timeout` as a server error, and
    LangGraph's default retry predicate retries it."""
    run = Run()
    run.sim.lose_answers = 1
    raised: list[ServerError] = []
    connection = GatewayConnection(
        run.scheduler, run.link, worker="agent-1", timeout_ms=SETTINGS.dispatch_timeout_ms
    )

    async def agent() -> None:
        listed = await connection.list_tools()
        timeouts = Timeouts(t_ms=SETTINGS.dispatch_timeout_ms, tool_wait_ms=2_500)
        tools = {tool.name: tool for tool in gateway_tools(connection, listed, timeouts)}
        quote = json.loads(await tools["get_quote"].ainvoke({"supplier_id": "S-1", "sku": "W-100"}))
        budget = json.loads(await tools["get_budget"].ainvoke({"cost_center": "CC-OPS"}))
        based_on = [quote["read_id"], budget["read_id"]]
        try:
            await tools["submit_purchase_order"].ainvoke({**ORDER, "based_on": based_on})
        except ServerError as error:
            raised.append(error)
        connection.close()

    async def main() -> None:
        run.scheduler.spawn("agent-1", agent(), name="agent")
        await run.scheduler.run(deadline_ms=120_000)

    asyncio.run(main())
    (error,) = raised
    assert "gateway_timeout" in str(error)
    retry_on = RetryPolicy().retry_on
    assert callable(retry_on) and not isinstance(retry_on, type)
    assert retry_on(error)


# Rule 6: the report is forwarded unchanged ------------------------------------------------


def test_g1_hands_the_report_to_the_sink_unchanged_and_checks_nothing() -> None:
    """G2 would refuse this report: it claims not_done while the order is confirmed."""
    run = Run()
    seen: list[dict[str, Any]] = []
    claims = [{"intent_id": "REQ-1", "status": "not_done", "effect_ids": []}]

    async def agent(client: Client) -> None:
        based_on = await client.cite()
        await client.ok("submit_purchase_order", **ORDER, based_on=based_on)
        seen.append(await client.ok("submit_final_report", claims=claims))

    run.go(agent)
    assert seen == [{"result": "accepted"}]
    (report,) = run.log.reports
    assert report.report == {"claims": claims}, "as sent: no default note was filled in"
    assert (report.worker, report.valid, report.accepted_by) == ("agent-1", True, "gateway")
    carried = [m for m in run.log.messages if (m.kind, m.api) == ("request", "submit_final_report")]
    assert [m.seq for m in carried] == [report.message_seq]
    snapshot = run.store.snapshot()
    assert not snapshot.reports and not snapshot.run_closed and not snapshot.closed_intents
    assert [c.tool for c in snapshot.tool_calls] == ["submit_purchase_order"]


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ([], "REQ-1 has no claim"),
        ([{"intent_id": "REQ-1", "status": "done", "effect_ids": []}], "effect_ids"),
    ],
)
def test_a_g1_report_that_breaks_the_schema_is_recorded_and_refused(
    claims: list[dict[str, object]], message: str
) -> None:
    """The sink records it as invalid and says why, as B0's sink does; the agent can fix
    it and resubmit."""
    run = Run()
    seen: list[dict[str, Any]] = []

    async def agent(client: Client) -> None:
        seen.append(await client.ok("submit_final_report", claims=claims))

    run.go(agent)
    (refused,) = seen
    assert (refused["result"], refused["reason"]) == ("refused", "invalid_args")
    assert message in refused["details"]["message"]
    (report,) = run.log.reports
    assert (report.report, report.valid) == ({"claims": claims}, False)


# A copy of the agent's call, in G1 --------------------------------------------------------


def test_the_agent_gets_the_answer_to_its_own_call() -> None:
    """F4 on the agent's hop (0006): the transport delivers the order call twice, and the
    answer to the copy is the one not delivered to the agent. In G2 both deliveries reach
    one action, so their answers are the same. In G1 the copy is a second action with its
    own answer, which here comes first. The agent is still told about its own call, and
    the copy's answer is dropped."""
    plan: list[dict[str, object]] = [
        {
            "fault": "F4",
            "at": {"point": "net.agent_gw.request", "match": {"api": "submit_purchase_order"}},
            "params": {"copy_delay_ms": 0},
        }
    ]
    run = Run(plan=plan)
    run.sim.slow = {"A-0001"}
    seen: list[dict[str, Any]] = []

    async def agent(client: Client) -> None:
        based_on = await client.cite()
        seen.append(await client.ok("submit_purchase_order", **ORDER, based_on=based_on))

    run.go(agent)
    request, copy = (
        m for m in run.log.messages if (m.kind, m.api) == ("request", "submit_purchase_order")
    )
    assert copy.copy_of == request.seq
    created = [p.action_id for p in run.store.snapshot().proposals]
    assert created == ["A-0001", "A-0002"], "G1: one action per delivery"
    answers = [
        (m.correlation, m.fate, m.drop_reason, m.delivered_vt)
        for m in run.log.messages
        if (m.hop, m.kind, m.api) == ("agent_gw", "answer", "submit_purchase_order")
    ]
    (copys_answer, own_answer) = answers
    assert copys_answer[:3] == (copy.seq, "dropped", "answer_to_copy"), "it came first"
    assert own_answer[:3] == (request.seq, "delivered", None)
    (result,) = seen
    assert (result["action_id"], result["state"]) == ("A-0001", "confirmed")
