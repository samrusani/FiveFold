"""The simulator's API on the harness transport (0008, 0009).

`endpoint` turns the transport's requests into calls on the simulator: it parses the
body into the request model the API name selects (`fivefold.domain.api.REQUESTS`),
calls the simulator method of the same name with the sender as the caller and the
scheduler's time as `vt`, and answers with the answer model as JSON. A write's answer
carries its answer class (`committed`, `rejected` or `in_progress`), which the harness
log records and fault plans can match (0006, F2).

Both callers build requests from the same models, so a body that does not parse is a
bug in the caller. The endpoint raises, which fails the run loudly, instead of
inventing an answer the API does not define.

The simulator never waits. `get_approval_status` with `wait_ms` above 0 (B0's tool,
0008) waits here, on the scheduler's clock: while the approval is pending, the endpoint
asks again every `poll_ms` until it is decided or the wait ends.

Two fault points of record 0006 are reached here, and `points` decides what fires there:

- `sim.write.received`, as each write request reaches the destination. A `WriteDelay`
  (F5) holds the write in flight, through the simulator's two-phase write: it is
  received now, committed `delay_ms` later, and answered then, committed or aborted.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from fivefold.domain.api import (
    REQUESTS,
    ApiName,
    ApprovalAnswer,
    GetApprovalStatus,
    ReserveInventory,
    SubmitPurchaseOrder,
    Write,
    WriteReceived,
)
from fivefold.domain.authority import ApprovalStatus
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Envelope, Handler, Reply
from fivefold.sim.service import Simulator

WRITES = frozenset({ApiName.SUBMIT_PURCHASE_ORDER, ApiName.RESERVE_INVENTORY})


Served = Callable[[ApiName, object], None]
"""Told of each answer the endpoint sends, with the call it answers."""


@dataclass(frozen=True)
class WriteDelay:
    """Hold a write that reached the destination, and commit it `delay_ms` later (F5).

    `waive_deadline` lets it commit after the commit deadline D, a contract violation
    (0005, 0006).
    """

    delay_ms: int
    waive_deadline: bool = False


class FaultPoints(Protocol):
    """Decides what fires at the simulator's fault points (0006)."""

    def write_received(self, request: Envelope) -> WriteDelay | None:
        """`sim.write.received`: a write request reached the destination."""
        ...


def endpoint(
    simulator: Simulator,
    scheduler: Scheduler,
    *,
    poll_ms: int = 500,
    served: Sequence[Served] = (),
    points: FaultPoints | None = None,
) -> Handler:
    """A transport handler that serves `simulator`'s API.

    Each of `served` is told of every answer, as the scripted approver needs to learn of
    queued approval requests (0002, rule 7). `points`, if given, decides the faults at
    the simulator's fault points.
    """

    async def handle(request: Envelope) -> Reply:
        if request.api is None:
            raise ValueError(f"request {request.seq} names no API call")
        api = ApiName(request.api)
        parsed = REQUESTS[api].model_validate(request.body)
        answer = await respond(request, api, parsed)
        for listener in served:
            listener(api, answer)
        body = answer.model_dump(mode="json")
        return Reply(body, answer_class=str(body["answer"]) if api in WRITES else None)

    async def respond(request: Envelope, api: ApiName, parsed: BaseModel) -> BaseModel:
        method = getattr(simulator, str(api))
        caller = request.sender
        if points is not None and isinstance(parsed, SubmitPurchaseOrder | ReserveInventory):
            delay = points.write_received(request)
            if delay is not None:
                return await commit_late(parsed, caller, delay)
        answer: BaseModel = method(parsed, caller=caller, vt=scheduler.now)
        if isinstance(parsed, GetApprovalStatus) and parsed.wait_ms > 0:
            deadline = scheduler.now + parsed.wait_ms
            while (
                isinstance(answer, ApprovalAnswer)
                and answer.status is ApprovalStatus.PENDING
                and scheduler.now < deadline
            ):
                await scheduler.sleep(min(poll_ms, deadline - scheduler.now))
                answer = method(parsed, caller=caller, vt=scheduler.now)
        return answer

    async def commit_late(write: Write, caller: str, delay: WriteDelay) -> BaseModel:
        """F5: receive the write now, and commit it `delay_ms` later.

        A write the key lookup answers on arrival (a replay, or `in_progress`), or that
        the simulator refuses, is not in flight, so its answer goes back at once.
        """
        received = simulator.receive_write(
            write, caller=caller, vt=scheduler.now, waive_deadline=delay.waive_deadline
        )
        if not isinstance(received, WriteReceived):
            return received
        await scheduler.sleep(delay.delay_ms)
        return simulator.commit_received(received.received_seq, vt=scheduler.now)

    return handle
