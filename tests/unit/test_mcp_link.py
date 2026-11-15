"""An MCP client and server talking through the scheduler (0009): kinds, order, crashes."""

from __future__ import annotations

import asyncio
from typing import Any

import mcp_types as types
from mcp.client.session import ClientSession
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError

from fivefold.faults.plan import FaultInjector, parse_plan
from fivefold.harness.log import MemoryHarnessLog, MessageRecord
from fivefold.harness.mcp_link import Link
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import Scheduler, SchedulerTimeout
from fivefold.harness.transport import Transport


class Rig:
    def __init__(self, seed: int = 1, plan: list[dict[str, object]] | None = None) -> None:
        self.scheduler = Scheduler()
        self.log = MemoryHarnessLog()
        self.transport = Transport(
            self.scheduler,
            self.log,
            Generators(seed).get("latency"),
            max_hop_latency_ms=50,
            faults=FaultInjector(parse_plan(plan or [])),
        )
        self.handled: list[str] = []
        self.results: list[tuple[int, object]] = []
        self.server_ended: list[int] = []
        self.delay_ms = 100
        self.crash_as_it_answers = False

    def server(self) -> Server[Any]:
        async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
            schema = {"type": "object", "properties": {"intent_id": {"type": "string"}}}
            return types.ListToolsResult(tools=[types.Tool(name="propose", input_schema=schema)])

        async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
            self.handled.append(str((params.arguments or {})["intent_id"]))
            await self.scheduler.sleep(self.delay_ms)
            if self.crash_as_it_answers:
                self.scheduler.call_later(
                    1,
                    lambda: self.scheduler.crash(
                        "agent-1", restart_after_ms=1, restart=lambda: None
                    ),
                )
            structured = {"result": "action", "intent_id": (params.arguments or {})["intent_id"]}
            return types.CallToolResult(
                content=[types.TextContent(text=str(structured))], structured_content=structured
            )

        return Server("gateway", on_list_tools=list_tools, on_call_tool=call_tool)

    def connect(self, calls: int) -> Link:
        link = Link(self.scheduler, self.transport, agent="agent-1", gateway="gateway")
        server = self.server()

        async def serve() -> None:
            await server.run(
                link.server_read, link.server_write, server.create_initialization_options()
            )
            self.server_ended.append(self.scheduler.now)

        async def agent() -> None:
            async with ClientSession(link.client_read, link.client_write) as session:
                await session.initialize()
                for n in range(calls):
                    result = await session.call_tool("propose", {"intent_id": f"REQ-{n + 1}"})
                    self.results.append((self.scheduler.now, result.structured_content))
            link.server_read.close()

        self.scheduler.spawn("gateway", serve(), name="mcp server")
        self.scheduler.spawn("agent-1", agent(), name="agent")
        return link

    def go(self, start: Any) -> None:
        async def main() -> None:
            start()
            await self.scheduler.run()

        asyncio.run(main())


def calls_of(log: MemoryHarnessLog) -> list[tuple[str, str | None, str | None, str | None]]:
    return [
        (m.kind, m.api, m.intent_id, m.answer_class) for m in log.messages if m.api == "propose"
    ]


def test_every_mcp_message_is_a_logged_message_on_the_agent_gw_hop() -> None:
    rig = Rig()
    rig.go(lambda: rig.connect(calls=2))
    assert [r for _, r in rig.results] == [
        {"result": "action", "intent_id": "REQ-1"},
        {"result": "action", "intent_id": "REQ-2"},
    ]
    assert {m.hop for m in rig.log.messages} == {"agent_gw"}
    assert all(m.fate == "delivered" for m in rig.log.messages)
    assert calls_of(rig.log) == [
        ("request", "propose", "REQ-1", None),
        ("answer", "propose", "REQ-1", "action"),
        ("request", "propose", "REQ-2", None),
        ("answer", "propose", "REQ-2", "action"),
    ]
    requests = {m.seq: m for m in rig.log.messages if m.kind == "request"}
    for answer in (m for m in rig.log.messages if m.kind == "answer"):
        assert answer.correlation in requests
        assert requests[answer.correlation].api == answer.api
    kinds = [(m.kind, m.api) for m in rig.log.messages[:3]]
    assert kinds == [
        ("request", "initialize"),
        ("answer", "initialize"),
        ("notification", "notifications/initialized"),
    ]
    assert rig.server_ended, "the server ends when the client closes its end"


def test_a_connection_is_deterministic() -> None:
    def history(seed: int) -> list[MessageRecord]:
        rig = Rig(seed)
        rig.go(lambda: rig.connect(calls=3))
        return rig.log.messages

    assert history(5) == history(5)
    assert history(5) != history(6)


def test_a_crashed_agent_loses_its_answers_and_its_connection() -> None:
    rig = Rig()
    rig.delay_ms = 5_000

    def start() -> None:
        rig.connect(calls=1)
        rig.scheduler.call_at(1_000, lambda: rig.scheduler.crash("agent-1"))

    rig.go(start)
    assert rig.results == []
    (answer,) = [m for m in rig.log.messages if m.kind == "answer" and m.api == "propose"]
    assert (answer.fate, answer.drop_reason) == ("dropped", "receiver_down")
    assert rig.server_ended == [1_051], "the gateway's end ends once nothing is in flight"


def test_an_answer_in_flight_at_a_crash_is_dropped_after_the_restart() -> None:
    """The answer left before the crash and arrives after the restart (0010, change 3)."""
    rig = Rig()
    rig.crash_as_it_answers = True
    rig.go(lambda: rig.connect(calls=1))
    assert rig.results == []
    (answer,) = [m for m in rig.log.messages if m.kind == "answer" and m.api == "propose"]
    assert (answer.fate, answer.drop_reason) == ("dropped", "caller_crashed")
    assert answer.delivered_vt is None


def test_a_request_in_flight_at_a_crash_is_still_served() -> None:
    """The agent dies right after sending: the gateway serves the call; its answer is lost."""
    rig = Rig()
    served: list[int] = []
    sent: list[int] = []

    def start() -> None:
        link = rig.connect(calls=1)

        def on_sent(envelope: Any, stage: str) -> None:
            if stage == "sent" and envelope.kind == "request" and envelope.api == "propose":
                sent.append(envelope.seq)
                rig.scheduler.call_later(0, lambda: rig.scheduler.crash("agent-1"))
            if stage == "delivered" and envelope.api == "propose" and envelope.kind == "request":
                served.append(envelope.seq)

        rig.transport.observe(on_sent)
        assert link.agent == "agent-1"

    rig.go(start)
    request, answer = [m for m in rig.log.messages if m.api == "propose"]
    assert served == sent == [request.seq]
    assert (request.fate, answer.fate, answer.drop_reason) == (
        "delivered",
        "dropped",
        "receiver_down",
    ), "the gateway answered; the dead agent never got it"
    assert rig.results == []


# A gateway crash (F1, 0006) ------------------------------------------------------------------


class GatewayCrashRig(Rig):
    """An agent that records how its calls end, and a gateway that can crash."""

    def __init__(self) -> None:
        super().__init__()
        self.outcomes: list[tuple[int, str]] = []

    def serve(self, link: Link) -> None:
        server = self.server()
        self.scheduler.spawn(
            "gateway",
            server.run(link.server_read, link.server_write, server.create_initialization_options()),
            name="mcp server",
        )

    def agent(self, link: Link, calls: int) -> None:
        async def run() -> None:
            async with ClientSession(link.client_read, link.client_write) as session:
                await session.initialize()
                # A session lists the tools before its first call returns, to check the
                # result against the tool's output schema; list them now, while the
                # gateway is up.
                await session.list_tools()
                for n in range(calls):
                    try:
                        await session.call_tool("propose", {"intent_id": f"REQ-{n + 1}"})
                        self.outcomes.append((self.scheduler.now, "answered"))
                    except MCPError as error:
                        self.outcomes.append((self.scheduler.now, f"error {error.code}"))

        self.scheduler.spawn("agent-1", run(), name="agent")


def test_a_gateway_crash_ends_the_agents_connection() -> None:
    rig = GatewayCrashRig()
    rig.delay_ms = 5_000
    links: list[Link] = []

    def start() -> None:
        link = Link(rig.scheduler, rig.transport, agent="agent-1", gateway="gateway")
        links.append(link)
        rig.serve(link)
        rig.agent(link, calls=2)
        rig.scheduler.call_at(1_000, lambda: rig.scheduler.crash("gateway"))

    rig.go(start)
    closed = f"error {types.CONNECTION_CLOSED}"
    assert rig.outcomes == [(1_051, closed), (1_051, closed)], (
        "the waiting call fails once nothing can still arrive; the next one cannot be sent"
    )
    assert links[0].ended
    (call,) = [m for m in rig.log.messages if m.api == "propose"]
    assert call.kind == "request" and call.fate == "delivered", "it reached the gateway"


def test_an_answer_the_gateway_sent_before_it_crashed_still_arrives() -> None:
    rig = GatewayCrashRig()

    def on_sent(envelope: Any, stage: str) -> None:
        if stage == "sent" and envelope.kind == "answer" and envelope.api == "propose":
            rig.scheduler.call_later(0, lambda: rig.scheduler.crash("gateway"))

    def start() -> None:
        link = Link(rig.scheduler, rig.transport, agent="agent-1", gateway="gateway")
        rig.transport.observe(on_sent)
        rig.serve(link)
        rig.agent(link, calls=1)

    rig.go(start)
    assert [outcome for _, outcome in rig.outcomes] == ["answered"]


def test_what_the_agent_sends_on_the_old_connection_never_reaches_the_restarted_gateway() -> None:
    """The gateway restarts while the agent's call travels: the new gateway never knew the
    connection, so the call is dropped, and the agent's end still closes."""
    rig = GatewayCrashRig()

    def on_sent(envelope: Any, stage: str) -> None:
        if stage == "sent" and envelope.kind == "request" and envelope.api == "propose":
            rig.scheduler.call_later(
                0, lambda: rig.scheduler.crash("gateway", restart_after_ms=0, restart=lambda: None)
            )

    def start() -> None:
        link = Link(rig.scheduler, rig.transport, agent="agent-1", gateway="gateway")
        rig.transport.observe(on_sent)
        rig.serve(link)
        rig.agent(link, calls=1)

    rig.go(start)
    (call,) = [m for m in rig.log.messages if m.api == "propose"]
    assert (call.fate, call.drop_reason) == ("dropped", "receiver_down")
    assert [outcome for _, outcome in rig.outcomes] == [f"error {types.CONNECTION_CLOSED}"]


def test_a_connection_opened_while_the_gateway_is_down_is_never_served() -> None:
    """Even once the gateway is back: no server ever took the connection."""
    rig = GatewayCrashRig()
    timed_out: list[int] = []

    async def handshake(link: Link) -> None:
        async with ClientSession(link.client_read, link.client_write) as session:
            try:
                await rig.scheduler.wait_for(session.initialize(), 2_000)
            except SchedulerTimeout:
                timed_out.append(rig.scheduler.now)

    def start() -> None:
        rig.scheduler.crash("gateway", restart_after_ms=10, restart=lambda: None)
        link = Link(rig.scheduler, rig.transport, agent="agent-1", gateway="gateway")

        def open_session() -> None:
            rig.scheduler.spawn("agent-1", handshake(link), name="handshake")

        rig.scheduler.call_at(1_000, open_session)

    rig.go(start)
    (initialize,) = [m for m in rig.log.messages if m.api == "initialize"]
    assert initialize.sent_vt == 1_000 and rig.scheduler.is_up("gateway")
    assert (initialize.fate, initialize.drop_reason) == ("dropped", "receiver_down")
    assert timed_out == [3_000]


def test_f4_on_the_gateway_hop_the_gateway_answers_twice_and_the_agent_gets_one() -> None:
    """0006: the copy reaches the gateway as the same JSON-RPC request, and the gateway
    answers both. The answers carry the same id, so they pair with the deliveries in the
    order those arrived; the copy's answer is dropped and correlates with the copy."""
    plan: list[dict[str, object]] = [
        {
            "fault": "F4",
            "at": {"point": "net.agent_gw.request", "match": {"api": "propose"}},
            "params": {"copy_delay_ms": 10},
        }
    ]
    rig = Rig(plan=plan)
    rig.go(lambda: rig.connect(calls=2))
    assert rig.handled == ["REQ-1", "REQ-1", "REQ-2"], "the gateway served the copy too"
    assert [r for _, r in rig.results] == [
        {"result": "action", "intent_id": "REQ-1"},
        {"result": "action", "intent_id": "REQ-2"},
    ]
    propose = [m for m in rig.log.messages if m.api == "propose"]
    request, copy = (m for m in propose if m.kind == "request" and m.intent_id == "REQ-1")
    assert copy.copy_of == request.seq and copy.body == request.body
    answers = [(m.correlation, m.fate, m.drop_reason) for m in propose if m.kind == "answer"]
    assert answers[:2] == [
        (request.seq, "delivered", None),
        (copy.seq, "dropped", "answer_to_copy"),
    ]
    assert answers[2][1:] == ("delivered", None), "the next call is answered as usual"
    (fault,) = rig.log.faults
    assert (fault.fault, fault.target_seq, fault.params["copy_seq"]) == (
        "F4",
        request.seq,
        copy.seq,
    )
