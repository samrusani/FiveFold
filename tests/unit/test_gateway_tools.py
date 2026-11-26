"""The gateway's tools, called by an MCP client over the scheduler (0001, 0003, 0008).

A stand-in simulator answers reads, approvals and writes in memory, so these tests pin
the tool layer: admission through MCP, the waits, the result shapes, the report check
and the report sink. The real simulator client is tested against the simulator itself.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import pytest
from mcp.client.session import ClientSession

from fivefold.domain.api import FactRead, GetBudget, GetInventory, GetQuote, ListSuppliers
from fivefold.domain.contracts import Contract, ContractParams
from fivefold.faults.plan import FaultInjector, parse_plan
from fivefold.gateway.codec import plain_from_json
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
    RefusalReason,
)
from fivefold.gateway.server import serve
from fivefold.gateway.sim_client import ReadRefused, ReadRequest
from fivefold.gateway.state_machine import AnswerClass, GatewaySettings, Reason
from fivefold.gateway.tools import TOOLS, GatewayTools, ToolTiming
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.mcp_link import Link
from fivefold.harness.reports import ReportSink
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Transport

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
    "currency": "usd",
    "cost_center": "CC-OPS",
}
HOP = 10


class FakeSim:
    """Facts, approvals and a C2 ledger in memory, with a fixed delay per call."""

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self.facts_now: dict[str, tuple[int, dict[str, object]]] = {
            QUOTE: (
                1,
                {
                    "unit_price_minor": 540,
                    "currency": "USD",
                    "min_quantity": 1,
                    "lead_time_days": 5,
                    "status": "active",
                },
            ),
            BUDGET: (1, {"limit_minor": 100_000, "committed_minor": 0}),
        }
        self.policy = ApprovalStatus.APPROVED
        self.approvals: dict[str, ApprovalStatus] = {}
        self.ledger: dict[str, str] = {}

    def change(self, key: str, **fields: object) -> None:
        version, value = self.facts_now[key]
        self.facts_now[key] = (version + 1, {**value, **fields})

    async def read(self, request: ReadRequest) -> FactRead:
        await self.scheduler.sleep(2 * HOP)
        key = _key_of(request)
        if key not in self.facts_now:
            raise ReadRefused("not_found", f"no fact {key}")
        version, value = self.facts_now[key]
        return FactRead(key=key, version=version, data=value)  # type: ignore[arg-type]

    async def facts(self, keys: Sequence[str]) -> Mapping[str, FactNow]:
        await self.scheduler.sleep(2 * HOP)
        return {k: FactNow(k, *self.facts_now[k]) for k in keys}

    async def request_approval(self, action: Action) -> ApprovalNow:
        await self.scheduler.sleep(2 * HOP)
        approval_id = f"APR-{action.action_id[2:]}"
        self.approvals.setdefault(approval_id, self.policy)
        return await self.approval(approval_id)

    async def approval(self, approval_id: str) -> ApprovalNow:
        status = self.approvals[approval_id]
        expires = 10**9 if status is ApprovalStatus.APPROVED else None
        return ApprovalNow(approval_id, status, expires)

    async def send(self, action: Action, attempt: Attempt, approval_id: str) -> Answer:
        await self.scheduler.sleep(2 * HOP)
        effect = self.ledger.get(attempt.idempotency_key)
        if effect is None:
            effect = f"PO-{len(self.ledger) + 1:04d}"
            self.ledger[attempt.idempotency_key] = effect
            amount = int(action.args["quantity"]) * int(action.args["unit_price_minor"])
            committed = self.facts_now[BUDGET][1]["committed_minor"]
            assert isinstance(committed, int)
            self.change(BUDGET, committed_minor=committed + amount)
        return Answer(AnswerClass.COMMITTED, Reason.COMMITTED, effect)

    async def lookup(self, action: Action) -> ProbeAnswer:
        raise Unreachable("C2 has no status")


def _key_of(request: ReadRequest) -> str:
    if isinstance(request, GetQuote):
        return f"quote/{request.supplier_id}/{request.sku}"
    if isinstance(request, GetBudget):
        return f"budget/{request.cost_center}"
    if isinstance(request, GetInventory):
        return f"inventory/{request.warehouse_id}/{request.sku}"
    assert isinstance(request, ListSuppliers)
    return "suppliers"


Agent = Callable[["Client"], Awaitable[None]]


class Client:
    """The agent's end: calls tools and decodes their JSON."""

    def __init__(self, session: ClientSession, scheduler: Scheduler) -> None:
        self.session = session
        self.scheduler = scheduler

    async def call(self, name: str, **arguments: object) -> tuple[bool, dict[str, Any]]:
        result = await self.session.call_tool(name, arguments)
        text = result.content[0]
        assert text.type == "text"
        return result.is_error, json.loads(text.text)

    async def ok(self, name: str, **arguments: object) -> dict[str, Any]:
        failed, body = await self.call(name, **arguments)
        assert not failed, body
        return body


class Run:
    def __init__(
        self, plan: list[dict[str, object]] | None = None, timing: ToolTiming | None = None
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
            task_id="TASK-1", intents=INTENTS, contract=Contract.C2, settings=SETTINGS
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
            timing=timing,
        )

    def go(self, agent: Agent) -> None:
        link = Link(self.scheduler, self.transport, agent="agent-1", gateway="gateway")

        async def worker() -> None:
            async with ClientSession(link.client_read, link.client_write) as session:
                await session.initialize()
                await agent(Client(session, self.scheduler))
            link.server_read.close()

        async def main() -> None:
            self.scheduler.spawn("gateway", serve(self.tools, link), name="mcp server")
            self.scheduler.spawn("agent-1", worker(), name="agent")
            await self.scheduler.run(deadline_ms=120_000)

        asyncio.run(main())


async def read_both(client: Client) -> list[str]:
    quote = await client.ok("get_quote", supplier_id="S-1", sku="W-100")
    budget = await client.ok("get_budget", cost_center="CC-OPS")
    return [quote["read_id"], budget["read_id"]]


def test_every_tool_is_listed_with_its_schema() -> None:
    run = Run()
    listed: list[Any] = []

    async def agent(client: Client) -> None:
        listed.extend((await client.session.list_tools()).tools)

    run.go(agent)
    assert [tool.name for tool in listed] == [t.name for t in TOOLS]
    order = next(tool for tool in listed if tool.name == "submit_purchase_order")
    assert {"intent_id", "based_on", "unit_price_minor"} <= set(order.input_schema["properties"])


def test_a_cited_order_is_confirmed_and_reported_done() -> None:
    run = Run()
    seen: dict[str, Any] = {}

    async def agent(client: Client) -> None:
        seen["quote"] = await client.ok("get_quote", supplier_id="S-1", sku="W-100")
        budget = await client.ok("get_budget", cost_center="CC-OPS")
        based_on = [seen["quote"]["read_id"], budget["read_id"]]
        seen["write"] = await client.ok("submit_purchase_order", **ORDER, based_on=based_on)
        seen["status"] = await client.ok("get_action_status", action_id="A-0001")
        claims = [{"intent_id": "REQ-1", "status": "done", "effect_ids": ["PO-0001"]}]
        seen["report"] = await client.ok("submit_final_report", claims=claims)

    run.go(agent)
    assert seen["quote"] == {
        "read_id": "R-0001",
        "key": QUOTE,
        "version": 1,
        "data": run.sim.facts_now[QUOTE][1],
    }
    write = seen["write"]
    assert (write["result"], write["state"], write["effect_id"]) == (
        "action",
        "confirmed",
        "PO-0001",
    )
    assert seen["status"]["state"] == "confirmed"
    assert seen["report"] == {"result": "accepted"}
    action = run.store.snapshot().actions["A-0001"]
    assert dict(action.args)["currency"] == "USD", "the gateway uppercased the currency"
    assert dict(action.basis) == {QUOTE: 1, BUDGET: 1}
    (report,) = run.log.reports
    assert (report.worker, report.valid, report.accepted_by) == ("agent-1", True, "gateway")
    carried = [m for m in run.log.messages if (m.kind, m.api) == ("request", "submit_final_report")]
    assert [m.seq for m in carried] == [report.message_seq], "the sink names the report's message"
    assert carried[0].delivered_seq is not None and carried[0].delivered_seq < report.seq
    assert run.store.snapshot().run_closed, "the only expected report closes the run"
    assert {m.hop for m in run.log.messages} == {"agent_gw"}


def test_each_report_names_the_delivery_that_carried_it_a_copy_included() -> None:
    """F4 (0006): the transport delivers the report twice, and the gateway serves both.
    Each report it records names its own delivery, so the evaluator can tell the copy's
    from the original's (0004)."""
    plan: list[dict[str, object]] = [
        {
            "fault": "F4",
            "at": {"point": "net.agent_gw.request", "match": {"api": "submit_final_report"}},
            "params": {"copy_delay_ms": 0},
        }
    ]
    run = Run(plan)

    async def agent(client: Client) -> None:
        claims = [{"intent_id": "REQ-1", "status": "not_done", "effect_ids": []}]
        await client.ok("submit_final_report", claims=claims)

    run.go(agent)
    request, copy = (
        m for m in run.log.messages if (m.kind, m.api) == ("request", "submit_final_report")
    )
    assert copy.copy_of == request.seq
    assert [report.message_seq for report in run.log.reports] == [request.seq, copy.seq]


def test_each_logged_response_holds_the_arguments_its_call_arrived_with() -> None:
    """0003's data model: `tool_call` holds each call's arguments. Writes that are
    admitted, attach, are refused or break the schema, a status call, and reports refused
    and accepted each log the arguments as the agent sent them: a field outside the
    schema, the currency before it is uppercased, a float. Reads and tool errors are not
    logged."""
    run = Run()
    sent: list[tuple[str, dict[str, object]]] = []

    async def agent(client: Client) -> None:
        async def call(name: str, **arguments: object) -> None:
            sent.append((name, arguments))
            await client.call(name, **arguments)

        based_on = await read_both(client)
        extra = {"why": ["cheapest", 1]}
        await call("submit_purchase_order", **ORDER, based_on=based_on, note="n", extra=extra)
        await call("submit_purchase_order", **ORDER, based_on=based_on[::-1])
        await call("submit_purchase_order", **{**ORDER, "intent_id": "REQ-9"}, based_on=based_on)
        await call("submit_purchase_order", **{**ORDER, "quantity": 1.5})
        await call("get_action_status", action_id="A-0001", wait_ms=0)
        not_done = {"intent_id": "REQ-1", "status": "not_done", "effect_ids": []}
        await call("submit_final_report", claims=[not_done])
        done = {"intent_id": "REQ-1", "status": "done", "effect_ids": ["PO-0001"]}
        await call("submit_final_report", claims=[done])
        await client.call("get_action_status", action_id="A-0042")

    run.go(agent)
    logged = run.store.snapshot().tool_calls
    assert [(c.tool, c.args) for c in logged] == [(n, plain_from_json(a)) for n, a in sent]
    assert [type(c.result).__name__ for c in logged] == [
        "ActionResult",
        "ActionResult",
        "Refusal",
        "Refusal",
        "ActionResult",
        "Refusal",
        "ReportAccepted",
    ]
    assert logged[0].args["currency"] == "usd", "as sent, before the gateway uppercased it"


def test_a_write_that_breaks_the_schema_is_refused_and_recorded() -> None:
    run = Run()
    seen: list[dict[str, Any]] = []

    async def agent(client: Client) -> None:
        seen.append(await client.ok("submit_purchase_order", **{**ORDER, "quantity": 1.5}))

    run.go(agent)
    (refused,) = seen
    assert (refused["result"], refused["reason"]) == ("refused", "invalid_args")
    assert "quantity" in refused["details"]["message"]
    (record,) = run.store.snapshot().proposals
    assert (record.admission, record.reason) == (Admission.REFUSED, RefusalReason.INVALID_ARGS)


def test_an_unencodable_argument_is_refused_before_proposing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fivefold.gateway import tools as gateway_tools_module

    def encode(*args: object) -> None:
        raise RecursionError("the store cannot encode this argument")

    monkeypatch.setattr(gateway_tools_module, "storable", encode, raising=False)
    run = Run()
    seen: list[dict[str, Any]] = []

    async def agent(client: Client) -> None:
        seen.append(await client.ok("submit_purchase_order", **ORDER))

    run.go(agent)
    (refused,) = seen
    assert (refused["result"], refused["reason"]) == ("refused", "invalid_args")
    assert not run.store.snapshot().actions


def test_a_write_without_citations_is_refused_for_its_missing_basis() -> None:
    run = Run()
    seen: list[dict[str, Any]] = []

    async def agent(client: Client) -> None:
        seen.append(await client.ok("submit_purchase_order", **ORDER))

    run.go(agent)
    assert seen[0]["reason"] == "missing_basis"
    assert seen[0]["details"] == {"missing": [QUOTE, BUDGET]}


def test_a_price_change_after_the_read_rejects_the_order_as_stale() -> None:
    run = Run()
    seen: list[dict[str, Any]] = []

    async def agent(client: Client) -> None:
        based_on = await read_both(client)
        run.sim.change(QUOTE, unit_price_minor=567)
        seen.append(await client.ok("submit_purchase_order", **ORDER, based_on=based_on))

    run.go(agent)
    (rejected,) = seen
    assert (rejected["state"], rejected["reason"]) == ("rejected", "stale_input")
    assert rejected["stale"] == [
        {
            "key": QUOTE,
            "cited_version": 1,
            "current_version": 2,
            "changed_fields": ["unit_price_minor"],
        }
    ]
    assert run.sim.ledger == {}


def test_a_harmless_change_to_the_quote_still_lets_the_order_through() -> None:
    """F7's `harmless` variant: only the lead time changes, which no rule reads (0004)."""
    run = Run()
    seen: list[dict[str, Any]] = []

    async def agent(client: Client) -> None:
        based_on = await read_both(client)
        run.sim.change(QUOTE, lead_time_days=9)
        seen.append(await client.ok("submit_purchase_order", **ORDER, based_on=based_on))

    run.go(agent)
    assert seen[0]["state"] == "confirmed"


def test_a_pending_approval_is_returned_at_once() -> None:
    run = Run()
    run.sim.policy = ApprovalStatus.PENDING
    seen: list[tuple[int, dict[str, Any]]] = []

    async def agent(client: Client) -> None:
        based_on = await read_both(client)
        start = client.scheduler.now
        result = await client.ok("submit_purchase_order", **ORDER, based_on=based_on)
        seen.append((client.scheduler.now - start, result))

    run.go(agent)
    ((took, result),) = seen
    assert result["state"] == "pending_approval"
    assert took < 2_500, "the call does not wait for the tool's full wait"


def test_unknown_reads_and_actions_are_tool_errors() -> None:
    run = Run()
    seen: list[tuple[bool, dict[str, Any]]] = []

    async def agent(client: Client) -> None:
        seen.append(await client.call("get_quote", supplier_id="S-9", sku="W-100"))
        seen.append(await client.call("get_action_status", action_id="A-0042"))
        seen.append(await client.call("get_quote", supplier_id="S 1", sku="W-100"))

    run.go(agent)
    assert seen == [
        (True, {"error": "not_found", "message": "no fact quote/S-9/W-100"}),
        (True, {"error": "unknown_action", "message": "no action A-0042"}),
        (True, {"error": "invalid_args", "message": seen[2][1]["message"]}),
    ]
    assert "supplier_id" in seen[2][1]["message"]


def test_a_status_wait_above_the_run_s_max_wait_ms_is_refused() -> None:
    """0005: a scenario can lower the longest status wait. The published schema keeps
    60,000 ms in every run, so the gateway checks the run's own value as it validates
    the call, and refuses a longer wait as it refuses any other bad argument."""
    run = Run(timing=ToolTiming(max_wait_ms=5_000))
    seen: list[tuple[bool, dict[str, Any]]] = []
    schemas: list[dict[str, Any]] = []

    async def agent(client: Client) -> None:
        listed = (await client.session.list_tools()).tools
        schemas.extend(tool.input_schema for tool in listed if tool.name == "get_action_status")
        based_on = await read_both(client)
        await client.ok("submit_purchase_order", **ORDER, based_on=based_on)
        for wait_ms in (5_001, 60_001, 5_000):
            seen.append(await client.call("get_action_status", action_id="A-0001", wait_ms=wait_ms))

    run.go(agent)
    assert seen[:2] == [
        (
            True,
            {
                "error": "invalid_args",
                "message": "wait_ms: Value error, the run's max_wait_ms is 5000",
            },
        ),
        (
            True,
            {
                "error": "invalid_args",
                "message": "wait_ms: Input should be less than or equal to 60000",
            },
        ),
    ]
    failed, status = seen[2]
    assert (failed, status["state"]) == (False, "confirmed")
    (schema,) = schemas
    assert schema["properties"]["wait_ms"] == {
        "default": 0,
        "maximum": 60000,
        "minimum": 0,
        "title": "Wait Ms",
        "type": "integer",
    }


def test_a_report_must_cover_each_intent_once_and_match_the_records() -> None:
    run = Run()
    seen: list[dict[str, Any]] = []

    async def agent(client: Client) -> None:
        seen.append(await client.ok("submit_final_report", claims=[]))
        based_on = await read_both(client)
        await client.ok("submit_purchase_order", **ORDER, based_on=based_on)
        claims = [{"intent_id": "REQ-1", "status": "not_done"}]
        seen.append(await client.ok("submit_final_report", claims=claims))

    run.go(agent)
    invalid, inconsistent = seen
    assert (invalid["reason"], invalid["details"]) == (
        "invalid_args",
        {"message": "REQ-1 has no claim"},
    )
    assert inconsistent["reason"] == "report_inconsistent"
    (conflict,) = inconsistent["details"]["conflicts"]
    assert conflict["recorded"] == [
        {"action_id": "A-0001", "state": "confirmed", "effect_ids": ["PO-0001"]}
    ]
    assert run.log.reports == [], "the sink receives only reports the gateway accepted"
