"""The gateway's tools against the real simulator, over MCP and the transport (M1).

Everything but the agent runs as it will in a G2 run: an MCP client calls the gateway's
tools over the `agent_gw` hop; the gateway reads, files approvals and writes through the
simulator's endpoint on the `gw_sim` hop; every message goes through the scheduler and
the harness log. The ledger is the simulator's own, so these tests check ground truth.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from mcp.client.session import ClientSession

from fivefold.domain.api import GetQuote
from fivefold.domain.contracts import Contract
from fivefold.faults.plan import FaultInjector, parse_plan
from fivefold.gateway.dispatcher import Dispatcher
from fivefold.gateway.freshness import RuleFreshness
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.server import serve
from fivefold.gateway.sim_client import SimClient
from fivefold.gateway.state_machine import GatewaySettings
from fivefold.gateway.tools import GatewayTools
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.mcp_link import Link
from fivefold.harness.reports import ReportSink
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Transport
from fivefold.sim import Simulator
from fivefold.sim.endpoint import endpoint

pytestmark = pytest.mark.pg

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
F2: dict[str, object] = {
    "fault": "F2",
    "at": {
        "point": "net.gw_sim.answer",
        "match": {"api": "submit_purchase_order", "answer": "committed"},
        "occurrence": 1,
    },
}


class Stack:
    """G2 without its agent: the gateway in front of the simulator."""

    def __init__(self, sim: Simulator, plan: list[dict[str, object]] | None = None) -> None:
        self.sim = sim
        self.scheduler = Scheduler()
        self.log = MemoryHarnessLog()
        self.transport = Transport(
            self.scheduler,
            self.log,
            Generators(11).get("latency"),
            max_hop_latency_ms=50,
            faults=FaultInjector(parse_plan(plan or [])),
        )
        self.transport.serve("simulator", endpoint(sim, self.scheduler))
        settings = GatewaySettings(approval_wait_ms=600_000, dispatch_timeout_ms=2_000)
        self.store = MemoryStore(
            task_id="TASK-dev-001", intents=INTENTS, contract=Contract.C2, settings=settings
        )
        self.client = SimClient(self.transport, timeout_ms=settings.dispatch_timeout_ms)
        self.dispatcher = Dispatcher(
            self.scheduler, self.store, self.client, RuleFreshness(self.store)
        )
        self.tools = GatewayTools(
            self.scheduler,
            self.store,
            self.dispatcher,
            self.client,
            ReportSink(self.scheduler, self.log, INTENTS),
            task_id="TASK-dev-001",
            intents=INTENTS,
        )

    def go(self, agent: Callable[[ClientSession], Awaitable[None]]) -> None:
        link = Link(self.scheduler, self.transport, agent="agent-1", gateway="gateway")

        async def worker() -> None:
            async with ClientSession(link.client_read, link.client_write) as session:
                await session.initialize()
                await agent(session)
            link.server_read.close()

        async def main() -> None:
            self.scheduler.spawn("gateway", serve(self.tools, link), name="mcp server")
            self.scheduler.spawn("agent-1", worker(), name="agent")
            await self.scheduler.run(deadline_ms=300_000)

        asyncio.run(main())


async def call(session: ClientSession, name: str, **arguments: object) -> dict[str, Any]:
    result = await session.call_tool(name, arguments)
    text = result.content[0]
    assert text.type == "text" and not result.is_error, text
    body: dict[str, Any] = json.loads(text.text)
    return body


async def order(session: ClientSession) -> dict[str, Any]:
    """Read the quote and the budget, then propose the order citing both."""
    quote = await call(session, "get_quote", supplier_id="S-1", sku="W-100")
    budget = await call(session, "get_budget", cost_center="CC-OPS")
    return await call(
        session, "submit_purchase_order", **ORDER, based_on=[quote["read_id"], budget["read_id"]]
    )


def effects(sim_database: str) -> list[tuple[object, ...]]:
    with psycopg.connect(sim_database) as conn:
        return conn.execute(
            "SELECT effect_id, intent_id, approval_id, basis, idempotency_key, client_ref"
            " FROM sim.effect ORDER BY commit_seq"
        ).fetchall()


def test_a_gateway_read_is_served_and_logged_by_the_simulator(
    make_sim: Callable[..., Simulator], sim_database: str
) -> None:
    stack = Stack(make_sim(Contract.C2))
    read: list[Any] = []

    async def main() -> None:
        stack.scheduler.spawn(
            "gateway",
            _append(read, stack.client.read(GetQuote(supplier_id="S-1", sku="W-100"))),
            name="read",
        )
        await stack.scheduler.run()

    asyncio.run(main())
    (fact,) = read
    assert (fact.key, fact.version, fact.data["unit_price_minor"]) == ("quote/S-1/W-100", 1, 540)
    with psycopg.connect(sim_database) as conn:
        rows = conn.execute("SELECT key, version, caller FROM sim.read_log").fetchall()
    assert rows == [("quote/S-1/W-100", 1, "gateway")]


async def _append(into: list[Any], awaitable: Awaitable[Any]) -> None:
    into.append(await awaitable)


def test_an_order_through_the_gateway_commits_once_with_its_key_and_basis(
    make_sim: Callable[..., Simulator], sim_database: str
) -> None:
    stack = Stack(make_sim(Contract.C2))
    seen: list[dict[str, Any]] = []

    async def agent(session: ClientSession) -> None:
        seen.append(await order(session))

    stack.go(agent)
    (result,) = seen
    assert (result["state"], result["effect_id"]) == ("confirmed", "PO-0001")
    assert effects(sim_database) == [
        (
            "PO-0001",
            "REQ-1",
            "APR-0001",
            {"quote/S-1/W-100": 1, "budget/CC-OPS": 1},
            "A-0001",
            "A-0001/1",
        )
    ]
    hops = {m.hop for m in stack.log.messages}
    assert hops == {"agent_gw", "gw_sim"}


def test_f2_at_c2_the_gateway_retries_with_the_same_key_and_one_order_exists(
    make_sim: Callable[..., Simulator], sim_database: str
) -> None:
    """Demo 1's gateway half: the commit's answer is lost; the retry gets the stored result."""
    stack = Stack(make_sim(Contract.C2), plan=[F2])
    seen: list[dict[str, Any]] = []

    async def agent(session: ClientSession) -> None:
        first = await order(session)
        seen.append(first)
        seen.append(
            await call(session, "get_action_status", action_id=first["action_id"], wait_ms=60_000)
        )

    stack.go(agent)
    first, final = seen
    assert first["state"] in ("dispatched", "outcome_unknown"), first
    assert (final["state"], final["effect_id"]) == ("confirmed", "PO-0001")
    (effect,) = effects(sim_database)
    assert effect[0] == "PO-0001"
    writes = [
        m
        for m in stack.log.messages
        if m.hop == "gw_sim" and m.kind == "request" and m.api == "submit_purchase_order"
    ]
    assert [m.idempotency_key for m in writes] == ["A-0001", "A-0001"]
    dropped = [m for m in stack.log.messages if m.drop_reason == "fault"]
    assert [(m.api, m.answer_class) for m in dropped] == [("submit_purchase_order", "committed")]
    with psycopg.connect(sim_database) as conn:
        states = conn.execute("SELECT state FROM sim.write ORDER BY received_seq").fetchall()
    assert states == [("committed",), ("replayed",)]
