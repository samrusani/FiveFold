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
  such request has arrived: `max_hop_latency_ms` after the crash, or later when a copy
  that F4 makes is still due. A restarted agent opens a new connection.
- A crashed gateway's connections are gone too (F1). Answers it sent before the crash
  still arrive; then the agent's end reads end-of-stream, `max_hop_latency_ms` after the
  crash, and a call still waiting fails as a closed connection. What the agent sends on
  the old connection is dropped, even after the gateway restarts: the restarted gateway
  never knew it. The agent opens a new connection, with a new MCP handshake.
- A connection opened while the gateway is down is never served: what the agent sends
  on it is dropped, as messages to a component that is down are.
- A component that is down sends nothing (`Transport.post`).

When the transport delivers a request twice (F4), the copy reaches the gateway with a
JSON-RPC id of its own, `copy-<seq>`, made from its message seq. The gateway answers
both, and each answer carries the id of the delivery it answers, so the agent always
gets the answer to its own request and the transport drops the copy's (0006), whichever
the gateway sends first. The logged copy keeps the request's body; only the delivered
message carries the new id. The gateway never uses the JSON-RPC id itself.

Each request delivered to the gateway carries its own message seq, as a transport
attaches its per-request data: `ServerMessageMetadata(request_context=...)`, which the
SDK hands to the handler as `ServerRequestContext.request` (mcp 2.3.0,
`mcp/shared/message.py`, `mcp/server/runner.py` `_make_context`). So the gateway names
the delivery it served, the copy included, when it records a final report (0004).

A message of the agent's that holds a NUL character, in a value or a key, is refused
before anything logs it, since the harness's log cannot store one
(`fivefold.harness.log.holds_nul`). As the split gateway does, the link answers a request
with invalid params, at once and in the agent's own inbox, and drops a notification.
Nothing goes on the wire, so the refusal takes no seq, no latency draw and no virtual time.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType

import anyio
import mcp_types as types
from mcp.shared.message import ServerMessageMetadata, SessionMessage
from pydantic_core import PydanticSerializationError

from fivefold.domain.json_storage import encodable_json
from fivefold.harness.log import NUL_REFUSED, DropReason, Kind, holds_nul
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Envelope, Transport, answer_to_copy

Item = SessionMessage | Exception


@dataclass(frozen=True)
class Delivery:
    """The delivery of a request to the gateway: the seq of the message that carried it."""

    seq: int


def delivered_seq(request: object) -> int | None:
    """The message seq of the delivery a gateway handler serves.

    `request` is the handler context's `request`, which the link sets on each request it
    delivers to the gateway. When the transport delivered a request twice (F4), each
    delivery has its own seq, so a handler serving the copy names the copy.
    """
    return request.seq if isinstance(request, Delivery) else None


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
    """What one side's requests were: their message seq, tool or method, and intent, and
    the deliveries of each that the other side has not answered yet, oldest first."""

    sent: dict[object, tuple[int, str | None, str | None]] = field(default_factory=dict)
    arrived: dict[object, deque[Envelope]] = field(default_factory=dict)

    def answering(self, message_id: object) -> Envelope | None:
        """The delivery that an answer with this JSON-RPC id answers: the oldest one."""
        waiting = self.arrived.get(message_id)
        if not waiting:
            return None
        delivery = waiting.popleft()
        if not waiting:
            del self.arrived[message_id]
        return delivery


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
        message = item.message
        try:
            body = message.model_dump(mode="json", by_alias=True, exclude_none=True)
        except (PydanticSerializationError, RecursionError, ValueError):
            if self._sender != self._link.agent:
                raise
            self._refuse(
                getattr(message, "method", None),
                getattr(message, "id", None),
                "it cannot be stored as JSON",
            )
            return
        method = body.get("method")
        message_id = body.get("id")
        if self._sender == self._link.agent:
            if holds_nul(body):
                self._refuse(method, message_id, NUL_REFUSED)
                return
            if not encodable_json(body):
                self._refuse(method, message_id, "it cannot be stored as JSON")
                return
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
            answer_class = answer_label(body)
        wanted = self._link.wanted(self._sender, kind)
        if kind == "answer":
            answered = self._theirs.answering(message_id)
            if answered is not None:
                correlation, api, intent_id = answered.seq, answered.api, answered.intent_id
                if answered.copy_of is not None:
                    wanted = answer_to_copy

        def arrive(envelope: Envelope) -> None:
            delivered = item
            if kind == "request":
                # Deliver the transport's copied JSON, the same body that was logged.
                # The sender's object can have a different mapping order or change in flight.
                key = message_id
                message = type(item.message).model_validate(envelope.body)
                if envelope.copy_of is not None:
                    key = _copy_id(envelope.seq)
                    message = message.model_copy(update={"id": key})
                delivered = SessionMessage(message, metadata=item.metadata)
                self._own.arrived.setdefault(key, deque()).append(envelope)
                if self._receiver == self._link.gateway:
                    metadata = ServerMessageMetadata(request_context=Delivery(envelope.seq))
                    delivered = SessionMessage(message, metadata=metadata)
            self._peer.deliver(delivered)

        envelope = self._transport.post(
            hop="agent_gw",
            kind=kind,
            sender=self._sender,
            receiver=self._receiver,
            body=body,
            on_arrival=arrive,
            api=api,
            correlation=correlation,
            intent_id=intent_id,
            answer_class=answer_class,
            wanted=wanted,
        )
        if envelope is not None and kind == "request":
            self._own.sent[message_id] = (envelope.seq, api, intent_id)

    def _refuse(self, method: object, message_id: object, why: str) -> None:
        """Refuse a message of the agent's that holds a NUL character, which the harness's
        log cannot store (`fivefold.harness.log.holds_nul`): nothing is logged or sent. A
        request gets the split gateway's answer, invalid params, at once, in the agent's
        own inbox; a notification is dropped."""
        if not isinstance(method, str) or not isinstance(message_id, int | str):
            return
        error = types.ErrorData(code=types.INVALID_PARAMS, message=f"the message is refused: {why}")
        refusal = types.JSONRPCError(jsonrpc="2.0", id=message_id, error=error)
        self._link.client_read.deliver(SessionMessage(refusal))

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


def _copy_id(seq: int) -> str:
    """The JSON-RPC id a copy carries into the receiver: its own, never the request's."""
    return f"copy-{seq}"


def answer_label(body: dict[str, object]) -> str | None:
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
        self._gateway_incarnation = scheduler.incarnation(gateway)
        self._served = scheduler.is_up(gateway)
        self.client_read = Inbox()
        self.server_read = Inbox()
        agent_requests, gateway_requests = _Requests(), _Requests()
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

    @property
    def ended(self) -> bool:
        """Whether the agent's end of the connection has ended, or is about to."""
        return self.client_read.closed

    def close(self) -> None:
        """End the connection from the agent's side, once its messages have arrived."""
        self.close_after_flight(self.agent, self.server_read)

    def wanted(self, sender: str, kind: Kind) -> Callable[[], DropReason | None]:
        """The check on arrival for a message `sender` sends on this connection."""
        if kind == "answer":
            return self.answer_wanted
        if sender == self.agent:
            return self.gateway_wanted
        return _always_wanted

    def answer_wanted(self) -> DropReason | None:
        """Drop answers sent to an agent that has crashed since it opened this link."""
        if self._scheduler.incarnation(self.agent) != self._agent_incarnation:
            return "caller_crashed"
        return None

    def gateway_wanted(self) -> DropReason | None:
        """Drop what the agent sends once the gateway's end is gone: the gateway was down
        when the connection opened, or has crashed since, restarted or not."""
        if (
            not self._served
            or self._scheduler.incarnation(self.gateway) != self._gateway_incarnation
        ):
            return "receiver_down"
        return None

    def close_after_flight(self, sender: str, inbox: Inbox) -> None:
        """Close `inbox` once every message `sender` has sent can have arrived."""
        if not self._scheduler.is_up(sender):
            return
        self._scheduler.call_later(
            self._in_flight_ms(sender), inbox.close, label=f"{sender} closes its end"
        )

    def _in_flight_ms(self, sender: str) -> int:
        """How long until every message `sender` has sent on this hop can have arrived:
        `max_hop_latency_ms`, or longer while a copy that F4 makes is still due."""
        wait = self._transport.max_hop_latency_ms + 1
        due = self._transport.last_due_vt("agent_gw", sender)
        if due is not None:
            wait = max(wait, due - self._scheduler.now + 1)
        return wait

    def _crashed(self, component: str) -> None:
        if component == self.agent:
            self.client_read.close()
            self._scheduler.call_later(
                self._in_flight_ms(self.agent),
                self.server_read.close,
                label=f"{self.agent}'s connection ends",
            )
        elif component == self.gateway and self._served and not self.client_read.closed:
            self._scheduler.call_later(
                self._in_flight_ms(self.gateway),
                self.client_read.close,
                label=f"{self.agent}'s connection to the gateway ends",
            )


def _always_wanted() -> DropReason | None:
    return None
