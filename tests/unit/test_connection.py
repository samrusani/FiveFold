"""An agent worker's MCP connection to the gateway: one session at a time, a new one after
the gateway crashes (F1, 0006)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import mcp_types as types
import pytest
from mcp.server.lowlevel.server import Server

from fivefold.agents.connection import GatewayConnection
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.mcp_link import Link
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Transport, TransportTimeout

T_MS = 2_000


class Rig:
    """A gateway that answers `propose` after `delay_ms`, and one agent worker."""

    def __init__(self) -> None:
        self.scheduler = Scheduler()
        self.log = MemoryHarnessLog()
        self.transport = Transport(
            self.scheduler, self.log, Generators(3).get("latency"), max_hop_latency_ms=50
        )
        self.delay_ms = 100
        self.links: list[Link] = []
        self.connection = GatewayConnection(
            self.scheduler, self.connect, worker="agent-1", timeout_ms=T_MS
        )
        self.outcomes: list[tuple[int, str]] = []

    def server(self) -> Server[Any]:
        async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
            return types.ListToolsResult(
                tools=[types.Tool(name="propose", input_schema={"type": "object"})]
            )

        async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
            await self.scheduler.sleep(self.delay_ms)
            return types.CallToolResult(content=[types.TextContent(text="ok")])

        return Server("gateway", on_list_tools=list_tools, on_call_tool=call_tool)

    def connect(self) -> Link:
        """As the harness does: a gateway that is up serves the new connection."""
        link = Link(self.scheduler, self.transport, agent="agent-1", gateway="gateway")
        self.links.append(link)
        if self.scheduler.is_up("gateway"):
            server = self.server()
            self.scheduler.spawn(
                "gateway",
                server.run(
                    link.server_read, link.server_write, server.create_initialization_options()
                ),
                name="mcp server",
            )
        return link

    async def call(self) -> None:
        try:
            result = await self.connection.call_tool("propose", {}, T_MS + self.delay_ms)
            text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
            self.outcomes.append((self.scheduler.now, text))
        except (ConnectionError, TransportTimeout) as error:
            self.outcomes.append((self.scheduler.now, type(error).__name__))

    def go(self, agent: Callable[[], Awaitable[None]]) -> None:
        async def run_agent() -> None:
            await agent()
            self.connection.close()

        async def main() -> None:
            self.scheduler.spawn("agent-1", run_agent(), name="agent")
            await self.scheduler.run()

        asyncio.run(main())

    def handshakes(self) -> list[tuple[int, str | None, str | None]]:
        return [
            (m.sent_vt, m.fate, m.drop_reason)
            for m in self.log.messages
            if m.api == "initialize" and m.kind == "request"
        ]


def test_one_session_serves_every_call_while_the_connection_lasts() -> None:
    rig = Rig()

    async def agent() -> None:
        await rig.connection.list_tools()
        await rig.call()
        await rig.call()

    rig.go(agent)
    assert [text for _, text in rig.outcomes] == ["ok", "ok"]
    assert len(rig.links) == 1 and len(rig.handshakes()) == 1


def test_a_call_waiting_when_the_gateway_crashes_fails_and_the_next_call_reconnects() -> None:
    rig = Rig()
    rig.delay_ms = 5_000
    crashed: list[int] = []

    def crash() -> None:
        crashed.append(rig.scheduler.now)
        rig.scheduler.crash("gateway", restart_after_ms=500, restart=lambda: None)

    async def agent() -> None:
        await rig.connection.list_tools()
        rig.scheduler.call_later(1_000, crash)
        await rig.call()
        rig.delay_ms = 100
        await rig.scheduler.sleep(1_000)
        await rig.call()

    rig.go(agent)
    (failed_at, failed), (_, answered) = rig.outcomes
    assert (failed_at, failed) == (crashed[0] + 51, "ConnectionError"), (
        "the call fails once nothing the gateway sent can still arrive"
    )
    assert answered == "ok"
    assert len(rig.links) == 2, "the second call opened a new connection"
    assert [fate for _, fate, _ in rig.handshakes()] == ["delivered", "delivered"]


def test_while_the_gateway_is_down_the_handshake_times_out_and_a_later_call_reconnects() -> None:
    rig = Rig()
    crashed: list[int] = []

    async def agent() -> None:
        await rig.connection.list_tools()
        crashed.append(rig.scheduler.now)
        rig.scheduler.crash("gateway", restart_after_ms=1_000, restart=lambda: None)
        await rig.scheduler.sleep(100)
        await rig.call()
        await rig.call()

    rig.go(agent)
    (_, first), (_, second) = rig.outcomes
    assert first == "TransportTimeout", "the handshake got no answer within T"
    assert second == "ok"
    down = crashed[0] + 100
    assert rig.handshakes()[1:] == [
        (down, "dropped", "receiver_down"),
        (down + T_MS, "delivered", None),
    ]


def test_a_handshake_that_times_out_names_initialize() -> None:
    rig = Rig()
    raised: list[str] = []

    async def agent() -> None:
        rig.scheduler.crash("gateway")
        with pytest.raises(TransportTimeout, match="initialize") as caught:
            await rig.connection.list_tools()
        raised.append(str(caught.value))

    rig.go(agent)
    assert raised == [f"initialize: no answer within {T_MS} ms"]


def test_closing_ends_the_session_and_its_connection() -> None:
    rig = Rig()

    async def agent() -> None:
        await rig.connection.list_tools()
        rig.connection.close()
        await rig.scheduler.sleep(100)

    rig.go(agent)
    (link,) = rig.links
    assert link.server_read.closed, "the gateway's end read end-of-stream"
