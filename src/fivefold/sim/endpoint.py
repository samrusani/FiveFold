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
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from fivefold.domain.api import REQUESTS, ApiName, ApprovalAnswer, GetApprovalStatus
from fivefold.domain.authority import ApprovalStatus
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Envelope, Handler, Reply
from fivefold.sim.service import Simulator

WRITES = frozenset({ApiName.SUBMIT_PURCHASE_ORDER, ApiName.RESERVE_INVENTORY})


Served = Callable[[ApiName, object], None]
"""Told of each answer the endpoint sends, with the call it answers."""


def endpoint(
    simulator: Simulator,
    scheduler: Scheduler,
    *,
    poll_ms: int = 500,
    served: Sequence[Served] = (),
) -> Handler:
    """A transport handler that serves `simulator`'s API.

    Each of `served` is told of every answer, as the scripted approver needs to learn of
    queued approval requests (0002, rule 7).
    """

    async def handle(request: Envelope) -> Reply:
        if request.api is None:
            raise ValueError(f"request {request.seq} names no API call")
        api = ApiName(request.api)
        parsed = REQUESTS[api].model_validate(request.body)
        method = getattr(simulator, str(api))
        answer = method(parsed, caller=request.sender, vt=scheduler.now)
        if isinstance(parsed, GetApprovalStatus) and parsed.wait_ms > 0:
            deadline = scheduler.now + parsed.wait_ms
            while (
                isinstance(answer, ApprovalAnswer)
                and answer.status is ApprovalStatus.PENDING
                and scheduler.now < deadline
            ):
                await scheduler.sleep(min(poll_ms, deadline - scheduler.now))
                answer = method(parsed, caller=request.sender, vt=scheduler.now)
        for listener in served:
            listener(api, answer)
        body = answer.model_dump(mode="json")
        return Reply(body, answer_class=str(body["answer"]) if api in WRITES else None)

    return handle
