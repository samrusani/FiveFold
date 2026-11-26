"""Messages between components, through the scheduler (0006, 0009).

Every interaction between components is a message on a hop: `agent_gw` and `gw_sim` in
gateway configurations, `agent_sim` in baselines. Each message takes a latency drawn from
the run's `latency` generator, between 5 and `max_hop_latency` virtual ms, and arrives in
order of (arrival time, send order). The harness log records every message as it is sent
and then its fate: delivered, with the number of the delivery, or dropped, with why.

Transport rules (0006, as amended by 0010):

- A component that is down sends nothing: what its cancelled tasks try to send while
  they unwind is discarded, and never logged.
- A message addressed to a component that is down is dropped.
- When a caller times out, every later answer to that request is dropped, as when an
  HTTP client closes its connection.
- When a component crashes, every later answer to a request it sent before the crash is
  dropped.
- Faults fire at `net.{hop}.request` and `net.{hop}.answer`, before the message travels.
  The fault's record names the message and its intent (0014).
- When F4 fires on a request, the transport delivers it a second time, `copy_delay_ms`
  after the first delivery. The copy is a message of its own, logged with `copy_of`
  naming the request: its sender sent the request once. A copy reaches no fault point
  and no before-send hook. The answer to a copy is never delivered: it is dropped on
  arrival with reason `answer_to_copy`, as a retrying proxy drops it (0006). An MCP
  notification passes the same point, as with F12, and is copied the same way.

A message's body is copied when it is sent: the log and the receiver each get their own
copy, so nothing done to a body later changes what was sent.

`call` is a request and its answer, like an HTTP call: the agent's tools in baselines and
the gateway's calls to the simulator use it. `post` sends one message and runs a callback
when it arrives; stream transports, such as MCP's in-process connection, build on it.
`Calls` names what `call` does, so the split topology's HTTP clients can stand in for it
(`fivefold.harness.split`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from fivefold.faults.catalog import HOPS, Action
from fivefold.faults.plan import FaultInjector, Fired
from fivefold.harness.log import (
    DropReason,
    FaultRecord,
    HarnessLog,
    JsonValue,
    Kind,
    MessageRecord,
)
from fivefold.harness.rng import Generator
from fivefold.harness.scheduler import Scheduler, SchedulerTimeout

Hop = Literal["agent_gw", "gw_sim", "agent_sim"]

MIN_LATENCY_MS = 5
"""The shortest hop latency, in virtual ms (0009)."""


class TransportTimeout(Exception):
    """No answer arrived within the caller's timeout.

    Not an `OSError`, so LangGraph's default retry predicate retries it (0008).
    """


@dataclass(frozen=True)
class Envelope:
    """One message, as the receiver gets it."""

    seq: int
    hop: Hop
    kind: Kind
    sender: str
    receiver: str
    api: str | None
    correlation: int | None
    intent_id: str | None
    idempotency_key: str | None
    answer_class: str | None
    body: JsonValue
    sent_vt: int
    copy_of: int | None = None
    """For a copy the transport made (F4), the seq of the request it repeats."""


@dataclass(frozen=True)
class Reply:
    """A handler's answer to a request: a body and its answer class, if it has one."""

    body: JsonValue
    answer_class: str | None = None


Handler = Callable[[Envelope], Awaitable[Reply]]


class Calls(Protocol):
    """Sends a request and waits for its answer, as `Transport.call` does: what the
    gateway's simulator client and the baseline's tools need from a transport."""

    async def call(
        self,
        *,
        hop: Hop,
        sender: str,
        receiver: str,
        api: str,
        body: JsonValue,
        timeout_ms: int,
        intent_id: str | None = None,
        idempotency_key: str | None = None,
        sent_vt: int | None = None,
    ) -> Reply:
        """Send a request and return its answer. Raises `TransportTimeout` when no answer
        arrives within `timeout_ms`.

        `sent_vt` is when the sender recorded the send, if it did: the gateway records
        each write attempt with its send time before it sends it (0003). The harness's
        log then gives the request that time, so that the gateway's settle bounds and the
        evaluator's, which reads the log, start from one number (0004, 0005).
        """
        ...


Effect = Callable[[Fired, "Envelope"], None]
"""Applies a fault that fired on a message and does more than drop it (F6, F7)."""

Stage = Literal["sent", "delivered"]
Observer = Callable[[Envelope, Stage], None]
"""Called after a message is sent, and again after it is delivered."""


@dataclass(frozen=True)
class Outgoing:
    """A message about to be sent: what a fault point before a send can match on."""

    hop: Hop
    kind: Kind
    sender: str
    receiver: str
    api: str | None
    intent_id: str | None


BeforeSend = Callable[[Outgoing], None]
"""Called before a message is sent, for the fault point `agent.before_tool_call`."""


class Transport:
    """Carries messages between a run's components, on the scheduler's clock."""

    def __init__(
        self,
        scheduler: Scheduler,
        log: HarnessLog,
        latency: Generator,
        *,
        max_hop_latency_ms: int,
        faults: FaultInjector | None = None,
    ) -> None:
        if max_hop_latency_ms < MIN_LATENCY_MS:
            raise ValueError(f"max_hop_latency_ms must be at least {MIN_LATENCY_MS}")
        self._scheduler = scheduler
        self._log = log
        self._latency = latency
        self._max_latency = max_hop_latency_ms
        self._faults = faults if faults is not None else FaultInjector()
        self._handlers: dict[str, Handler] = {}
        self._observers: list[Observer] = []
        self._before_send: list[BeforeSend] = []
        self._effect: Effect | None = None
        self._last_due: dict[tuple[str, str], int] = {}

    @property
    def max_hop_latency_ms(self) -> int:
        """The longest one-way latency a message can take on any hop."""
        return self._max_latency

    def last_due_vt(self, hop: Hop, sender: str) -> int | None:
        """When the last message `sender` has sent on `hop` is due to arrive, or None.

        A message arrives within `max_hop_latency_ms`, but a copy F4 delivers can arrive
        later, `copy_delay_ms` after the first delivery. A connection waits for this
        before its receiving end closes, so nothing it was due to deliver is lost.
        """
        return self._last_due.get((hop, sender))

    def _due(self, envelope: Envelope, delay_ms: int) -> None:
        """Note that `envelope` is due to arrive `delay_ms` from now."""
        key = (envelope.hop, envelope.sender)
        due = self._scheduler.now + delay_ms
        self._last_due[key] = max(due, self._last_due.get(key, due))

    def apply_faults_with(self, effect: Effect) -> None:
        """Apply faults that change state, such as F6 and F7, with `effect` (0006).

        The effect runs when the fault fires, before the message travels on.
        """
        self._effect = effect

    def observe(self, observer: Observer) -> None:
        """Call `observer` after each message is sent, and after each is delivered.

        The harness uses it for fault points that belong to a component rather than a
        hop, such as F11's `agent.after_tool_call` (0006). It runs inside the step that
        sent or delivered the message, after the message's fate so far is logged. For a
        delivery it runs before the receiver gets the message, and a receiver that an
        observer crashed never gets it: the delivery happened, and the crash voided it.
        """
        self._observers.append(observer)

    def before_send(self, hook: BeforeSend) -> None:
        """Call `hook` before each message is sent.

        The harness uses it for `agent.before_tool_call`, F1's B0 equivalent (0006). A
        hook that crashes the sender stops the message: a component that is down sends
        nothing, so the message is never sent or logged.
        """
        self._before_send.append(hook)

    def draw_latency(self) -> int:
        """One hop's latency, from the run's latency generator (0009)."""
        return self._latency.randint(MIN_LATENCY_MS, self._max_latency)

    # One message ----------------------------------------------------------------------------

    def post(
        self,
        *,
        hop: Hop,
        kind: Kind,
        sender: str,
        receiver: str,
        body: JsonValue,
        on_arrival: Callable[[Envelope], None],
        api: str | None = None,
        correlation: int | None = None,
        intent_id: str | None = None,
        idempotency_key: str | None = None,
        answer_class: str | None = None,
        wanted: Callable[[], DropReason | None] | None = None,
    ) -> Envelope | None:
        """Send one message; `on_arrival` runs when it is delivered.

        `wanted`, checked on arrival, names a reason to drop the message instead, for
        example that the caller has timed out. Returns None, sending nothing, when the
        sender is down, or a before-send hook crashed it.
        """
        if hop not in HOPS:
            raise ValueError(f"unknown hop {hop!r}")
        if not self._scheduler.is_up(sender):
            return None
        if self._before_send:
            outgoing = Outgoing(hop, kind, sender, receiver, api, intent_id)
            for hook in self._before_send:
                hook(outgoing)
            if not self._scheduler.is_up(sender):
                return None
        text = json.dumps(body, sort_keys=True)
        envelope = Envelope(
            seq=self._log.next_seq(),
            hop=hop,
            kind=kind,
            sender=sender,
            receiver=receiver,
            api=api,
            correlation=correlation,
            intent_id=intent_id,
            idempotency_key=idempotency_key,
            answer_class=answer_class,
            body=json.loads(text),
            sent_vt=self._scheduler.now,
        )
        self._log.message_sent(
            MessageRecord(
                seq=envelope.seq,
                hop=hop,
                kind=kind,
                sender=sender,
                receiver=receiver,
                api=api,
                correlation=correlation,
                intent_id=intent_id,
                idempotency_key=idempotency_key,
                answer_class=answer_class,
                body=json.loads(text),
                sent_vt=envelope.sent_vt,
            )
        )
        point = f"net.{hop}.{'answer' if kind == 'answer' else 'request'}"
        fields = {"api": api, "tool": api, "answer": answer_class, "intent_id": intent_id}
        dropped = False
        copies: list[Fired] = []
        approval_status = envelope.body.get("status") if isinstance(envelope.body, dict) else None
        for fired in self._faults.at(
            point, fields, approval_status=approval_status, memory_target=envelope.body
        ):
            action = fired.planned.kind.action
            if action is Action.DUPLICATE:
                copies.append(fired)
                continue
            self._record(fired, point, envelope)
            if action is Action.DROP:
                dropped = True
            elif self._effect is not None:
                self._effect(fired, envelope)
            else:
                raise RuntimeError(f"{fired.fault} fired at {point} with nothing to apply it")
        latency: int | None = None
        if dropped:
            self._log.message_fate(envelope.seq, "dropped", vt=self._scheduler.now, reason="fault")
        else:
            latency = self.draw_latency()
            self._due(envelope, latency)
            self._scheduler.call_later(
                latency,
                lambda: self._arrive(envelope, on_arrival, wanted),
                label=f"deliver {envelope.seq}",
            )
        for fired in copies:
            self._copy(fired, point, envelope, text, on_arrival, wanted, latency)
        for observer in self._observers:
            observer(envelope, "sent")
        return envelope

    def _copy(
        self,
        fired: Fired,
        point: str,
        original: Envelope,
        text: str,
        on_arrival: Callable[[Envelope], None],
        wanted: Callable[[], DropReason | None] | None,
        latency: int | None,
    ) -> None:
        """F4: deliver `original` a second time, `copy_delay_ms` after its first delivery.

        The fault's record names the copy (`copy_seq`), and the copy's names the request
        it repeats (`copy_of`). When a fault dropped the request itself, the copy still
        travels, with a latency of its own.
        """
        if latency is None:
            latency = self.draw_latency()
        fault_seq = self._log.next_seq()
        copy = replace(
            original, seq=self._log.next_seq(), body=json.loads(text), copy_of=original.seq
        )
        self._log.fault_fired(
            FaultRecord(
                seq=fault_seq,
                fault=str(fired.fault),
                variant=fired.planned.variant,
                point=point,
                occurrence=fired.occurrence,
                action=str(Action.DUPLICATE),
                vt=self._scheduler.now,
                target_seq=original.seq,
                params={**fired.planned.params, "copy_seq": copy.seq},
                intent_id=original.intent_id,
            )
        )
        self._log.message_sent(
            MessageRecord(
                seq=copy.seq,
                hop=copy.hop,
                kind=copy.kind,
                sender=copy.sender,
                receiver=copy.receiver,
                api=copy.api,
                correlation=copy.correlation,
                intent_id=copy.intent_id,
                idempotency_key=copy.idempotency_key,
                answer_class=copy.answer_class,
                body=json.loads(text),
                sent_vt=copy.sent_vt,
                copy_of=original.seq,
            )
        )
        delay_ms = latency + fired.planned.param("copy_delay_ms")
        self._due(copy, delay_ms)
        self._scheduler.call_later(
            delay_ms,
            lambda: self._arrive(copy, on_arrival, wanted),
            label=f"deliver copy {copy.seq}",
        )

    def _arrive(
        self,
        envelope: Envelope,
        on_arrival: Callable[[Envelope], None],
        wanted: Callable[[], DropReason | None] | None,
    ) -> None:
        reason: DropReason | None = None
        if not self._scheduler.is_up(envelope.receiver):
            reason = "receiver_down"
        elif wanted is not None:
            reason = wanted()
        now = self._scheduler.now
        if reason is not None:
            self._log.message_fate(envelope.seq, "dropped", vt=now, reason=reason)
            return
        self._log.message_fate(
            envelope.seq, "delivered", vt=now, delivered_seq=self._log.next_seq()
        )
        for observer in self._observers:
            observer(envelope, "delivered")
        if self._scheduler.is_up(envelope.receiver):
            on_arrival(envelope)

    def _record(self, fired: Fired, point: str, envelope: Envelope) -> None:
        self._log.fault_fired(
            FaultRecord(
                seq=self._log.next_seq(),
                fault=str(fired.fault),
                variant=fired.planned.variant,
                point=point,
                occurrence=fired.occurrence,
                action=str(fired.planned.kind.action),
                vt=self._scheduler.now,
                target_seq=envelope.seq,
                params=dict(fired.planned.params),
                intent_id=envelope.intent_id,
            )
        )

    # A request and its answer ------------------------------------------------------------------

    def serve(self, component: str, handler: Handler, *, replace: bool = False) -> None:
        """Answer requests addressed to `component` with `handler`.

        Each request runs as a task of the component, so a crash cancels it. A component
        rebuilt after a crash registers its new handler with `replace`, so no request
        reaches the memory the crash discarded. The handler is looked up when a request
        arrives.
        """
        if component in self._handlers and not replace:
            raise ValueError(f"{component} already has a handler")
        self._handlers[component] = handler

    async def call(
        self,
        *,
        hop: Hop,
        sender: str,
        receiver: str,
        api: str,
        body: JsonValue,
        timeout_ms: int,
        intent_id: str | None = None,
        idempotency_key: str | None = None,
        sent_vt: int | None = None,
    ) -> Reply:
        """Send a request and wait for its answer, at most `timeout_ms` virtual ms.

        Raises `TransportTimeout` if no answer arrives in time. A later answer is dropped.
        `sent_vt` changes nothing here: the request is sent in its sender's step, and the
        scheduler moves virtual time only between steps, so a send the sender recorded in
        that step is sent at the time it recorded.
        """
        if receiver not in self._handlers:
            raise ValueError(f"{receiver} serves no requests")
        answered: asyncio.Future[Envelope] = asyncio.get_running_loop().create_future()
        incarnation = self._scheduler.incarnation(sender)
        abandoned = False

        def still_wanted() -> DropReason | None:
            if self._scheduler.incarnation(sender) != incarnation:
                return "caller_crashed"
            if abandoned or answered.cancelled():
                return "caller_timed_out"
            return None

        def on_answer(answer: Envelope) -> None:
            if not answered.done():
                answered.set_result(answer)

        def on_request(request: Envelope) -> None:
            wanted = answer_to_copy if request.copy_of is not None else still_wanted
            self._scheduler.spawn(
                receiver,
                self._answer(request, self._handlers[receiver], on_answer, wanted),
                name=f"{api} {request.seq}",
            )

        self.post(
            hop=hop,
            kind="request",
            sender=sender,
            receiver=receiver,
            body=body,
            on_arrival=on_request,
            api=api,
            intent_id=intent_id,
            idempotency_key=idempotency_key,
        )
        try:
            answer = await self._scheduler.wait_for(answered, timeout_ms)
        except SchedulerTimeout:
            abandoned = True
            raise TransportTimeout(
                f"{api} from {sender} to {receiver}: no answer within {timeout_ms} ms"
            ) from None
        return Reply(answer.body, answer.answer_class)

    async def _answer(
        self,
        request: Envelope,
        handler: Handler,
        on_answer: Callable[[Envelope], None],
        still_wanted: Callable[[], DropReason | None],
    ) -> None:
        reply = await handler(request)
        self.post(
            hop=request.hop,
            kind="answer",
            sender=request.receiver,
            receiver=request.sender,
            body=reply.body,
            on_arrival=on_answer,
            api=request.api,
            correlation=request.seq,
            intent_id=request.intent_id,
            idempotency_key=request.idempotency_key,
            answer_class=reply.answer_class,
            wanted=still_wanted,
        )


def answer_to_copy() -> DropReason:
    """The arrival check of an answer to a copy (F4): it never reaches the sender (0006)."""
    return "answer_to_copy"
