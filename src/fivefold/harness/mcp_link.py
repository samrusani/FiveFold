"""An MCP connection between an agent worker and the gateway, through the scheduler (0009).

The inproc topology keeps the MCP layer between the agent and the gateway (0009): the
agent's MCP client and the gateway's MCP server exchange JSON-RPC messages, and each one
is a message on the `agent_gw` hop, with its latency, fate and fault points (0006). The
two ends are stream pairs that satisfy the MCP SDK's `ReadStream` and `WriteStream`
protocols (mcp 2.3.0, `mcp/shared/_stream_protocols.py`), so the SDK's `ClientSession`
and low-level `Server.run` use them as they would any transport.

What a crash means for a connection (0006, 0010 change 3):

- A crashed agent's connection is gone. Its requests already on the wire still reach
  the gateway, which serves them, as a server serves a request whose client died after
  sending it; the answers are dropped. The gateway's end reads end-of-stream once every
  such request has arrived, `max_hop_latency_ms` after the crash. A restarted agent
  opens a new connection.
- A component that is down sends nothing (`Transport.post`).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from types import TracebackType

import anyio
from mcp.shared.message import SessionMessage

from fivefold.harness.log import DropReason, Kind
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Transport

Item = SessionMessage | Exception


class Inbox:
    """The receiving end of one direction of a connection."""

    def __init__(self) -> None:
        self._items: deque[Item] = deque()
        self._waiter: asyncio.Future[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether the sending end has closed, or the connection is gone."""
        return self._closed

    def deliver(self, item: Item) -> None:
        """Hand over an arrived message."""
        if not self._closed:
            self._items.append(item)
            self._wake()

    def close(self) -> None:
        """End the stream: once the delivered messages are read, `receive` ends."""
        self._closed = True
        self._wake()

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    async def receive(self) -> Item:
        while not self._items:
            if self._closed:
                raise anyio.EndOfStream
            self._waiter = asyncio.get_running_loop().create_future()
            await self._waiter
        return self._items.popleft()

    def __aiter__(self) -> Inbox:
        return self

    async def __anext__(self) -> Item:
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.close()

    async def __aenter__(self) -> Inbox:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


@dataclass
class _Requests:
    """What one side's requests were: their message seq, tool or method, and intent."""

    sent: dict[object, tuple[int, str | None, str | None]] = field(default_factory=dict)


class Outbox:
    """The sending end of one direction: each message goes through the transport."""

    def __init__(
        self,
        link: Link,
        transport: Transport,
        *,
        sender: str,
        receiver: str,
        peer: Inbox,
        own: _Requests,
        theirs: _Requests,
    ) -> None:
        self._link = link
        self._transport = transport
        self._sender = sender
        self._receiver = receiver
        self._peer = peer
        self._own = own
        self._theirs = theirs

    async def send(self, item: SessionMessage, /) -> None:
        body = item.message.model_dump(mode="json", by_alias=True, exclude_none=True)
        method = body.get("method")
        message_id = body.get("id")
        kind: Kind
        correlation: int | None = None
        api: str | None = None
        intent_id: str | None = None
        answer_class: str | None = None
        if isinstance(method, str) and message_id is not None:
            kind, api = "request", method
            params = body.get("params")
            if method == "tools/call" and isinstance(params, dict):
                api = str(params.get("name"))
                arguments = params.get("arguments")
                if isinstance(arguments, dict) and isinstance(arguments.get("intent_id"), str):
                    intent_id = arguments["intent_id"]
        elif isinstance(method, str):
            kind, api = "notification", method
        else:
            kind = "answer"
            correlation, api, intent_id = self._theirs.sent.get(message_id, (None, None, None))
            answer_class = _answer_class(body)
        envelope = self._transport.post(
            hop="agent_gw",
            kind=kind,
            sender=self._sender,
            receiver=self._receiver,
            body=body,
            on_arrival=lambda _: self._peer.deliver(item),
            api=api,
            correlation=correlation,
            intent_id=intent_id,
            answer_class=answer_class,
            wanted=self._link.answer_wanted if kind == "answer" else None,
        )
        if envelope is not None and kind == "request":
            self._own.sent[message_id] = (envelope.seq, api, intent_id)

    async def aclose(self) -> None:
        """End this direction, after the messages already on the wire arrive.

        A component that is down closes nothing: its crash ends the connection.
        """
        self._link.close_after_flight(self._sender, self._peer)

    async def __aenter__(self) -> Outbox:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()


def _answer_class(body: dict[str, object]) -> str | None:
    """A short label for an answer: `error`, or the tool result's `result` field."""
    if "error" in body:
        return "error"
    result = body.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("isError"):
        return "error"
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        return str(structured["result"])
    return None


class Link:
    """One MCP connection from an agent worker to the gateway.

    The agent's `ClientSession` uses (`client_read`, `client_write`); the gateway's
    `Server.run` uses (`server_read`, `server_write`). Open a new link after a restart.
    """

    def __init__(
        self, scheduler: Scheduler, transport: Transport, *, agent: str, gateway: str
    ) -> None:
        self._scheduler = scheduler
        self._transport = transport
        self.agent = agent
        self.gateway = gateway
        self._agent_incarnation = scheduler.incarnation(agent)
        self.client_read = Inbox()
        self.server_read = Inbox()
        agent_requests, gateway_requests = _Requests(), _Requests()
        self._agent_requests = agent_requests
        self.client_write = Outbox(
            self,
            transport,
            sender=agent,
            receiver=gateway,
            peer=self.server_read,
            own=agent_requests,
            theirs=gateway_requests,
        )
        self.server_write = Outbox(
            self,
            transport,
            sender=gateway,
            receiver=agent,
            peer=self.client_read,
            own=gateway_requests,
            theirs=agent_requests,
        )
        scheduler.on_crash(self._crashed)

    def request_seq(self, request_id: object) -> int | None:
        """The message seq of the agent's request with this JSON-RPC id, if it was sent.

        The gateway names the message that carried a final report this way, so the
        evaluator can find when the report reached it (0004, property 5).
        """
        sent = self._agent_requests.sent.get(request_id)
        return None if sent is None else sent[0]

    def answer_wanted(self) -> DropReason | None:
        """Drop answers sent to an agent that has crashed since it opened this link."""
        if self._scheduler.incarnation(self.agent) != self._agent_incarnation:
            return "caller_crashed"
        return None

    def close_after_flight(self, sender: str, inbox: Inbox) -> None:
        """Close `inbox` once every message `sender` has sent can have arrived."""
        if not self._scheduler.is_up(sender):
            return
        self._scheduler.call_later(
            self._transport.max_hop_latency_ms + 1,
            inbox.close,
            label=f"{sender} closes its end",
        )

    def _crashed(self, component: str) -> None:
        if component == self.agent:
            self.client_read.close()
            self._scheduler.call_later(
                self._transport.max_hop_latency_ms + 1,
                self.server_read.close,
                label=f"{self.agent}'s connection ends",
            )
