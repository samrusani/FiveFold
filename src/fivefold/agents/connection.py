"""An agent worker's MCP connection to the gateway, in G modes (0008, 0009).

The worker talks to the gateway through one MCP session at a time. A session ends when
its connection does, for example when the gateway crashes (F1, 0006). The next call
then opens a new connection and session, with the MCP handshake, as an MCP client does
when its server has forgotten the session. A call already waiting when the connection
ends fails with `ConnectionError`, which the framework's retry handles (0008). Each
handshake request waits at most T, as a tool call does (0005).

Each session runs in a task of the worker, so the worker's crash ends it.

MCP SDK calls used (mcp 2.3.0): `ClientSession(read_stream, write_stream)` as an async
context manager, `ClientSession.initialize`, `list_tools` and `call_tool`;
`MCPError.code`; and `mcp_types.CONNECTION_CLOSED`, the code a call gets when its
connection has closed (`mcp/shared/jsonrpc_dispatcher.py`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import mcp_types as types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError

from fivefold.harness.scheduler import Scheduler, SchedulerTimeout
from fivefold.harness.transport import TransportTimeout


async def within[T](scheduler: Scheduler, call: Awaitable[T], timeout_ms: int, what: str) -> T:
    """Wait for `call` at most `timeout_ms` virtual ms; raise `TransportTimeout` after."""
    try:
        return await scheduler.wait_for(call, timeout_ms)
    except SchedulerTimeout:
        raise TransportTimeout(f"{what}: no answer within {timeout_ms} ms") from None


class Channel(Protocol):
    """One connection, as its agent end sees it. Its two streams satisfy the MCP SDK's
    stream protocols, as `ClientSession` needs (`mcp/shared/_stream_protocols.py`)."""

    @property
    def client_read(self) -> Any:
        """What the gateway sends."""
        ...

    @property
    def client_write(self) -> Any:
        """Where the agent sends."""
        ...

    @property
    def ended(self) -> bool:
        """Whether the connection has ended, or is about to."""
        ...

    def close(self) -> None:
        """End the connection from the agent's side."""


@dataclass
class _Session:
    channel: Channel
    session: ClientSession
    done: asyncio.Event


class GatewayConnection:
    """The worker's connection to the gateway: one session at a time, a new one as needed."""

    def __init__(
        self,
        scheduler: Scheduler,
        connect: Callable[[], Channel],
        *,
        worker: str,
        timeout_ms: int,
    ) -> None:
        """`connect` opens a new connection; `timeout_ms` bounds each handshake request."""
        self._scheduler = scheduler
        self._connect = connect
        self._worker = worker
        self._timeout_ms = timeout_ms
        self._current: _Session | None = None
        self._opening = asyncio.Lock()

    async def list_tools(self) -> types.ListToolsResult:
        """The gateway's tools, from `tools/list`."""
        session = await self._session()
        return await within(self._scheduler, session.list_tools(), self._timeout_ms, "tools/list")

    async def call_tool(
        self, name: str, arguments: dict[str, Any], timeout_ms: int
    ) -> types.CallToolResult:
        """Call a tool, waiting at most `timeout_ms` for its result."""
        session = await self._session()
        try:
            return await within(
                self._scheduler, session.call_tool(name, arguments), timeout_ms, name
            )
        except MCPError as error:
            if error.code != types.CONNECTION_CLOSED:
                raise
            self._end()
            raise ConnectionError(f"{name}: the connection to the gateway closed") from error

    def close(self) -> None:
        """End the current session and its connection, if there is one."""
        self._end()

    async def _session(self) -> ClientSession:
        async with self._opening:
            current = self._current
            if current is not None and not current.channel.ended:
                return current.session
            self._end()
            channel = self._connect()
            opened: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
            done = asyncio.Event()
            self._scheduler.spawn(
                self._worker, self._hold(channel, opened, done), name=f"{self._worker} session"
            )
            session = await opened
            fresh = _Session(channel, session, done)
            self._current = fresh
            try:
                await within(self._scheduler, session.initialize(), self._timeout_ms, "initialize")
            except BaseException:
                if self._current is fresh:
                    self._end()
                raise
            return session

    async def _hold(
        self, channel: Channel, opened: asyncio.Future[ClientSession], done: asyncio.Event
    ) -> None:
        """Keep one session open until it is no longer needed."""
        try:
            async with ClientSession(channel.client_read, channel.client_write) as session:
                opened.set_result(session)
                await done.wait()
        except Exception as error:
            if not opened.done():
                opened.set_exception(error)

    def _end(self) -> None:
        current, self._current = self._current, None
        if current is not None:
            current.done.set()
            current.channel.close()
