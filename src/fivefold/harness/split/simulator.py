"""The simulator's service in the split topology: its API over HTTP (0008, 0009).

Each call of the simulator's API (`fivefold.domain.api`) is a POST to `/api/{call}` with
the request model as a JSON body, and the caller's secret as a bearer token
(`credentials`). The answer is the answer model as JSON. Every answer the API defines,
refusals and rejections included, comes with status 200, except a caller refused for
holding no credential of the run, which gets 403. Requests that never reach the API get
other statuses, and are not logged:

| Status | When |
| --- | --- |
| 401 | no token, or not a secret of this run; the body is the call's forbidden answer |
| 404 | no such call |
| 422 | a body that is not the call's request model or holds a NUL; a wait over `max_wait_ms` |
| 503 | the run has stopped, or, for anyone but the gateway, its agents' phase is over |

A baseline's agent submits its final report the same way, to `/api/submit_final_report`:
the harness's report sink takes it, as in the inproc topology (0008). In G1 and G2 reports
go through the gateway, so this call is refused (403) to everyone.

The caller's identity comes from its token, never from what it sends. The simulator then
applies its own rule, which the run set up: in G1 and G2 only the gateway holds a
credential, so the agent's identity is refused, reads and writes alike (0008, trust
boundaries). A request from the gateway travels on the `gw_sim` hop, one from the agent on
`agent_sim`.

The service runs the components the inproc harness runs next to the simulator: the
scripted approver, F6 and F7 through the simulator's control interface, F5 and F10 at the
simulator's points (`fivefold.sim.endpoint`, `fivefold.harness.sim_points`), and the
faults of the network points it owns (`fivefold.harness.split.faults`). As the inproc
transport tells the simulator's points of each message it sends, the service tells them
of each answer it sends, so F10 logs one more row for each status answer it changed
(0014). Each request
runs as a task of the component `simulator`, so stopping the run cancels the requests
still in flight, such as a write F5 holds.

`/harness/drain` and `/harness/stop` take the harness's token. Drain refuses the agent
from then on: the agents' phase is over (0009). Stop aborts every write in flight past
its deadline, records the run's stop time (`harness.run_stop`), refuses everyone, and
writes no harness record after it.
`/health` says whether the service is serving, and how many of its tasks failed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from fivefold.domain.api import (
    REQUESTS,
    ApiName,
    Refusal,
    RefusalReason,
    WaitLimit,
    WriteRejected,
    WriteRejectReason,
)
from fivefold.domain.report import validation_messages
from fivefold.faults.plan import FaultInjector, Fired
from fivefold.harness.actors import ScriptedApprover
from fivefold.harness.fault_effects import SimulatorFaults, message_context
from fivefold.harness.log import DropReason
from fivefold.harness.reports import ReportSink, sink_endpoint
from fivefold.harness.sim_points import SimulatorPoints
from fivefold.harness.split.credentials import GATEWAY, HARNESS
from fivefold.harness.split.hops import SEQ_HEADER, WRITES, Hops, logged_message
from fivefold.harness.split.rundir import SplitRun
from fivefold.harness.split.service import (
    OpenRun,
    json_storage_problem,
    open_run,
    unavailable,
    until_gone,
)
from fivefold.harness.transport import Envelope, Handler, Hop, Reply
from fivefold.sim import Simulator
from fivefold.sim.endpoint import endpoint

MAX_REQUEST_BYTES = 4 * 1024 * 1024
"""Match the MCP SDK's 4 MiB JSON request limit."""

SIMULATOR = "simulator"
REPORT_SINK = "report_sink"
REPORT_CALL = "submit_final_report"


@dataclass
class _Serving:
    """What the service holds while it serves its run."""

    opened: OpenRun
    simulator: Simulator
    hops: Hops
    points: SimulatorPoints
    api: Handler
    sink: Handler
    wait_limit: WaitLimit
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    draining: bool = False
    stopped_vt: int | None = None

    def has_stopped(self) -> bool:
        """Whether the run has stopped. A request asks again after each wait, since the run
        can stop meanwhile."""
        return self.stopped_vt is not None


class SimulatorService:
    """The simulator's API for one split run, over HTTP."""

    def __init__(self, run: SplitRun, *, base_dsn: str) -> None:
        self.run = run
        self._base_dsn = base_dsn
        self._callers = run.callers()
        self._serving: _Serving | None = None

    def app(self) -> Starlette:
        """The service's ASGI app."""
        return Starlette(
            routes=[
                Route("/health", self._health, methods=["GET"]),
                Route("/api/{call}", self._call, methods=["POST"]),
                Route("/harness/{action}", self._control, methods=["POST"]),
            ],
            lifespan=self._lifespan,
        )

    @property
    def serving(self) -> _Serving:
        if self._serving is None:
            raise RuntimeError("the simulator's service has not started")
        return self._serving

    # Starting and stopping ----------------------------------------------------------------

    @asynccontextmanager
    async def _lifespan(self, app: Starlette) -> AsyncIterator[None]:
        opened = open_run(self.run, self._base_dsn)
        try:
            self._serving = self._start(opened)
            yield
        finally:
            if self._serving is not None:
                self._serving.stopping.set()
                self._serving.simulator.close()
            opened.close()

    def _start(self, opened: OpenRun) -> _Serving:
        scenario, scheduler, log = opened.scenario, opened.scheduler, opened.log
        params = scenario.params
        simulator = Simulator(self.run.conninfo(self._base_dsn, "ff_sim"))
        faults = FaultInjector(opened.plan)
        approver = ScriptedApprover(scheduler, simulator.control, log, scenario.authority)
        effects = SimulatorFaults(scheduler, simulator.control, log, scenario)

        def effect(fired: Fired, envelope: Envelope) -> None:
            effects.apply(fired, message_context(envelope.body))

        points = SimulatorPoints(
            scheduler,
            faults,
            log,
            dispatch_timeout_ms=params.dispatch_timeout_ms,
            status_lag_ms=params.status_lag_ms,
        )
        api = endpoint(
            simulator,
            scheduler,
            poll_ms=params.approval_poll_interval_ms,
            max_wait_ms=params.max_wait_ms,
            served=[approver.served],
            points=points,
        )
        intents = [intent.id for intent in scenario.intents]
        sink = sink_endpoint(ReportSink(scheduler, log, intents))
        hops = Hops(scheduler, log, faults, effect=effect)
        limit = WaitLimit(params.max_wait_ms)
        return _Serving(opened, simulator, hops, points, api, sink, limit)

    def stop(self) -> int:
        """Stop the run: abort writes in flight past their deadline, record the stop time,
        and end every task and timer. Returns the stop time; stopping again returns it."""
        serving = self.serving
        if serving.stopped_vt is None:
            scheduler = serving.opened.scheduler
            now = scheduler.now
            serving.simulator.control.abort_overdue(vt=now)
            serving.opened.log.run_stopped(now)
            serving.stopped_vt = now
            scheduler.stop()
            serving.opened.stop_records()
            serving.stopping.set()
        return serving.stopped_vt

    # Routes -------------------------------------------------------------------------------

    async def _health(self, request: Request) -> Response:
        serving = self.serving
        status = "serving"
        if serving.has_stopped():
            status = "stopped"
        elif serving.draining:
            status = "draining"
        failures = len(serving.opened.scheduler.failures)
        body = {"run_id": self.run.run_id, "status": status, "failures": failures}
        return JSONResponse(body)

    async def _control(self, request: Request) -> Response:
        if self._callers.identify(request.headers.get("authorization")) != HARNESS:
            return JSONResponse({"error": "forbidden"}, status_code=401)
        action = request.path_params["action"]
        if action == "drain":
            if self.serving.has_stopped():
                return unavailable("the run has stopped")
            self.serving.draining = True
            return JSONResponse({"status": "draining"})
        if action == "stop":
            return JSONResponse({"stopped_vt": self.stop()})
        return JSONResponse({"error": "unknown_action", "detail": action}, status_code=404)

    async def _call(self, request: Request) -> Response:
        call = str(request.path_params["call"])
        serving = self.serving
        if serving.has_stopped():
            return unavailable("the run has stopped")
        caller = self._callers.identify(request.headers.get("authorization"))
        if caller is None or caller == HARNESS:
            return _forbidden(call, "no credential of this run", status=401)
        if caller not in serving.simulator.config.credentials:
            return _forbidden(call, f"{caller} holds no credential for this service", status=403)
        if serving.draining and caller != GATEWAY:
            return unavailable("the run's agents' phase is over")
        body = await _json_request(request)
        if isinstance(body, JSONResponse):
            return body
        if serving.has_stopped():
            return unavailable("the run has stopped")
        receiver, handler = SIMULATOR, serving.api
        if call == REPORT_CALL:
            # Only B0's agent reports here; in G1 and G2 reports go through the gateway
            # (0008), so no caller of the simulator may report.
            if self.run.has_gateway:
                return _forbidden(
                    call, f"{caller} cannot report here in {self.run.config}", status=403
                )
            receiver, handler = REPORT_SINK, serving.sink
        else:
            try:
                api = ApiName(call)
            except ValueError:
                return JSONResponse({"error": "unknown_call", "detail": call}, status_code=404)
            try:
                REQUESTS[api].model_validate(body, context=serving.wait_limit)
            except ValidationError as error:
                return _invalid("; ".join(validation_messages(error)))
        hop: Hop = "gw_sim" if caller == GATEWAY else "agent_sim"
        arrived = self._arrived(request, hop, caller, receiver, call, body)
        if arrived is None:
            return _invalid(f"{SEQ_HEADER} names no request the gateway sent")
        return await self._serve(request, arrived, handler)

    def _arrived(
        self, request: Request, hop: Hop, caller: str, receiver: str, call: str, body: object
    ) -> Envelope | None:
        """The arrived request, as logged: the gateway logged its own as it sent it; the
        agent's is logged now. None for a seq that names no unanswered gateway request."""
        serving = self.serving
        header = request.headers.get(SEQ_HEADER)
        if hop == "gw_sim" and header is not None:
            try:
                found = logged_message(serving.opened.conn, int(header))
            except ValueError:
                return None
            if found is None:
                return None
            logged, has_fate = found
            if (
                has_fate
                or logged.hop != hop
                or logged.kind != "request"
                or logged.sender != caller
                or logged.receiver != receiver
                or logged.api != call
            ):
                return None
            return logged
        fields = body if isinstance(body, Mapping) else {}
        return serving.hops.sent(
            hop=hop,
            kind="request",
            sender=caller,
            receiver=receiver,
            api=call,
            body=body,
            intent_id=_text(fields.get("intent_id")),
            idempotency_key=_text(fields.get("idempotency_key")),
        )

    async def _serve(self, request: Request, arrived: Envelope, handler: Handler) -> Response:
        serving = self.serving
        hops = serving.hops
        point = f"net.{arrived.hop}.request"
        outcome = hops.fire(point, arrived)
        if outcome.dropped:
            hops.dropped(arrived, "fault")
        else:
            hops.delivered(arrived)
        for fired in outcome.copies:
            self._deliver_copy_later(fired, point, arrived, handler)
        if outcome.dropped:
            await until_gone(request, serving.stopping)
            return unavailable("the request was lost")
        reply = await self._handled(arrived, handler)
        if reply is None or serving.has_stopped():
            return unavailable("the run stopped while the request was handled")
        if reply.answer_class == "aborted":
            return unavailable("the write missed its commit deadline")
        answer = hops.sent(
            hop=arrived.hop,
            kind="answer",
            sender=arrived.receiver,
            receiver=arrived.sender,
            api=arrived.api,
            body=reply.body,
            intent_id=arrived.intent_id,
            idempotency_key=arrived.idempotency_key,
            correlation=arrived.seq,
            answer_class=reply.answer_class,
        )
        serving.points.answer_sent(answer, "sent")
        status = 403 if _refused_forbidden(reply.body) else 200
        if arrived.hop == "gw_sim":
            # The gateway logs the answer's fate as it arrives, or does not arrive.
            return JSONResponse(
                reply.body, status_code=status, headers={SEQ_HEADER: str(answer.seq)}
            )
        if hops.fire("net.agent_sim.answer", answer).dropped:
            hops.dropped(answer, "fault")
            await until_gone(request, serving.stopping)
            return unavailable("the answer was lost")
        gone = await request.is_disconnected()
        if serving.has_stopped():
            return unavailable("the run stopped while the request was handled")
        if gone:
            hops.dropped(answer, "caller_timed_out")
            return unavailable("the caller has gone")
        hops.delivered(answer)
        return JSONResponse(reply.body, status_code=status)

    async def _handled(self, arrived: Envelope, handler: Handler) -> Reply | None:
        """`handler`'s reply to `arrived`, run as a task of the simulator; None if the run
        stopped first."""
        scheduler = self.serving.opened.scheduler
        if scheduler.stopped:
            return None

        async def handled() -> Reply:
            return await handler(arrived)

        task = scheduler.spawn(SIMULATOR, handled(), name=f"{arrived.api} {arrived.seq}")
        await asyncio.wait({task})
        if task.cancelled():
            return None
        return task.result()

    # F4's copies ----------------------------------------------------------------------------

    def _deliver_copy_later(
        self, fired: Fired, point: str, original: Envelope, handler: Handler
    ) -> None:
        """F4: log the copy now, and deliver it `copy_delay_ms` after the request (0006)."""
        serving = self.serving
        scheduler = serving.opened.scheduler
        copy = serving.hops.copy(fired, point, original)

        def arrive() -> None:
            scheduler.spawn(SIMULATOR, self._copy_arrives(copy, handler), name=f"copy {copy.seq}")

        scheduler.call_later(
            fired.planned.param("copy_delay_ms"), arrive, label=f"deliver copy {copy.seq}"
        )

    async def _copy_arrives(self, copy: Envelope, handler: Handler) -> None:
        serving = self.serving
        hops = serving.hops
        hops.delivered(copy)
        reply = await handler(copy)
        answer = hops.sent(
            hop=copy.hop,
            kind="answer",
            sender=copy.receiver,
            receiver=copy.sender,
            api=copy.api,
            body=reply.body,
            intent_id=copy.intent_id,
            idempotency_key=copy.idempotency_key,
            correlation=copy.seq,
            answer_class=reply.answer_class,
        )
        serving.points.answer_sent(answer, "sent")
        reason: DropReason = "answer_to_copy"
        hops.dropped(answer, reason)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _refused_forbidden(body: object) -> bool:
    """Whether an answer refuses its caller for holding no credential of the run."""
    return isinstance(body, Mapping) and body.get("reason") == "forbidden"


def _forbidden(call: str, detail: str, *, status: int) -> JSONResponse:
    """The call's own answer to a caller with no credential: a write is rejected, every
    other call refused (`fivefold.domain.api`)."""
    answer: Any
    if call in WRITES:
        answer = WriteRejected(reason=WriteRejectReason.FORBIDDEN, detail=detail)
    elif call == REPORT_CALL:
        return JSONResponse(
            {"result": "refused", "reason": "forbidden", "details": {"message": detail}},
            status_code=status,
            headers={"www-authenticate": "Bearer"},
        )
    else:
        answer = Refusal(reason=RefusalReason.FORBIDDEN, detail=detail)
    return JSONResponse(
        answer.model_dump(mode="json"), status_code=status, headers={"www-authenticate": "Bearer"}
    )


def _invalid(detail: str) -> JSONResponse:
    return JSONResponse({"error": "invalid_request", "detail": detail}, status_code=422)


async def _json_request(request: Request) -> dict[str, Any] | JSONResponse:
    data = bytearray()
    try:
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_REQUEST_BYTES:
                return JSONResponse(
                    {"error": "invalid_request", "detail": "the body exceeds 4 MiB"},
                    status_code=413,
                )
            data.extend(chunk)
        body = json.loads(data)
    except (ValueError, RecursionError, ClientDisconnect):
        return _invalid("the body is not a readable JSON object")
    if not isinstance(body, dict):
        return _invalid("the body is not a JSON object")
    if problem := json_storage_problem(body):
        return _invalid(f"the body is refused: {problem}")
    return body
