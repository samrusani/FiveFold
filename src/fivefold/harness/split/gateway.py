"""The gateway's service in the split topology: MCP over streamable HTTP (0008, 0009).

The service serves the gateway's tools at `/mcp`, through the MCP SDK's streamable HTTP
session manager, to the run's one agent worker, `agent-1`. The agent reaches the gateway
and nothing else when it runs, as `start` tells it to, in a container on a network it
shares only with the gateway (0008); an agent on the host can reach the simulator,
PostgreSQL and the run's directory too. The gateway's store, dispatcher and tools are the
inproc topology's, on a `HostScheduler`; G1's store has action identity off, as in the
inproc topology.

The gateway reaches the simulator only through the simulator's API, over HTTP, with its
own credential (`SimulatorLink`). Its simulator client (`fivefold.gateway.sim_client`) is
unchanged: the link takes the place of the harness's transport, logs each request as it
sends it and each answer as it arrives, and fires F2 at `net.gw_sim.answer`. A request
that gets no answer within its timeout, or fails on its connection, raises
`TransportTimeout`, which the client reads as a timeout; a server error is a server error.

`AgentHop`, a context-tier MCP middleware, logs every MCP message the agent sends, and the
gateway's answers, on the `agent_gw` hop, and fires F4 and F12 at `net.agent_gw.request`.
It logs an answer as delivered when the gateway's handler returns it, before the SDK
writes it to the agent's connection, and it cannot see whether the agent is still there
to read it (`fivefold.harness.split.hops`).
It sees every request and notification in both of the SDK's protocol eras, the
`initialize` handshake and the 2026-07-28 per-request envelope. It gives the gateway's
handler the message's seq, as the inproc link does, so the report sink records which
message carried a report.

`/harness/drain` and `/harness/stop` take the harness's token. Drain ends the agents'
phase (0009): G2 closes the run, as the inproc harness does once its agents are done
(0003), and the service refuses further MCP requests; the dispatcher keeps recovering
actions through the drain window. Stop ends the dispatcher's tasks and timers, and the
service writes no harness record after it, except the actor-log row of an attestation
already in flight: a call still in flight then fails.

`/admin` serves the gateway's admin API, when the service is given one: a human resolves
an escalation there by attesting what happened (0003). It shares the MCP endpoint's
listener, so whatever reaches `/mcp`, the agent included, reaches `/admin`, and the API's
bearer token is its only guard. The evaluator counts an
attestation only from the harness's actor log (0004). The scripted resolver writes that
row in the inproc topology; here a human attests over HTTP, so the admin path the API
calls is a `RecordedAdmin`, which writes the same row after the gateway has recorded the
attestation. The two writes are separate transactions on separate connections: the
gateway's as `ff_gw`, the harness's as `ff_harness`. Once stop has begun, no attestation
is taken: none commits in the gateway's records, and the API answers 503. An attestation
taken before keeps the harness's records open until its row is written (`Attestations`).

Checked in starlette 1.10.0 (`applications.py`, `_exception_handler.py`):
`add_exception_handler` adds a handler before the app builds its middleware, which it does
at its first request; a handler for an exception class runs inside the routes, found by
the exception's MRO, before the app's handler for `Exception`.

Checked in mcp 2.3.0: `StreamableHTTPSessionManager(app=, security_settings=)`, its
`run()` context, and `StreamableHTTPASGIApp(manager)`
(`mcp/server/streamable_http_manager.py`); `TransportSecuritySettings(
enable_dns_rebinding_protection=, allowed_hosts=, allowed_origins=)`
(`mcp/server/transport_security.py`); `Server.middleware`, a list of `ServerMiddleware`
called as `(ctx, call_next)` for every inbound request and notification
(`mcp/server/context.py`, `mcp/server/runner.py`: both eras reach
`ServerRunner._on_request`); `ServerRequestContext`, a keyword-only dataclass with
`method`, `params`, `request_id` and `request`; and `MCPError.error`, the `ErrorData` it
carries (`mcp/shared/exceptions.py`). Checked in httpx 0.28.1: `AsyncClient(base_url=,
timeout=)`, `post(url, content=, headers=)`, and `HTTPError`, the base of its request
errors.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from fivefold.faults.plan import FaultInjector, Fired
from fivefold.gateway.admin import GatewayAdmin
from fivefold.gateway.admin_api import TOKEN_VARIABLE, admin_app, token_from_environment
from fivefold.gateway.dispatcher import Dispatcher, Timing
from fivefold.gateway.freshness import RuleFreshness
from fivefold.gateway.pg_store import PgStore
from fivefold.gateway.records import ActionResult, Resolution
from fivefold.gateway.server import worker_server
from fivefold.gateway.sim_client import SimClient
from fivefold.gateway.state_machine import GatewaySettings
from fivefold.gateway.store import ActionStore
from fivefold.gateway.tools import GatewayTools, ToolTiming
from fivefold.harness.actors import ATTEST
from fivefold.harness.clock import HostClock
from fivefold.harness.log import ActorRecord, HarnessLog, JsonValue
from fivefold.harness.mcp_link import Delivery, answer_label
from fivefold.harness.reports import ReportSink
from fivefold.harness.scheduler import Scheduler, SchedulerTimeout
from fivefold.harness.split import SERVICE_STOPPED
from fivefold.harness.split.credentials import AGENT, GATEWAY, HARNESS
from fivefold.harness.split.hops import SEQ_HEADER, Hops, answer_class
from fivefold.harness.split.rundir import SplitRun
from fivefold.harness.split.service import OpenRun, open_run, unavailable
from fivefold.harness.transport import Envelope, Hop, Reply, TransportTimeout

DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*", "gateway:*")
"""Host headers the MCP endpoint accepts: the loopback names and the Compose service name.
Others are refused, against DNS rebinding."""

SERVER_ERROR = {"answer": "server_error"}
"""What the link hands the simulator client for an answer that is a server error."""

AdminFactory = Callable[[GatewayAdmin, ActionStore], ASGIApp]
"""Builds the gateway's admin API, for mounting at `/admin`, from its admin path and its
records, as `fivefold.gateway.admin_api.admin_app` takes them with its token."""


def admin_api(environ: Mapping[str, str] | None = None) -> AdminFactory | None:
    """The gateway's admin API (`fivefold.gateway.admin_api.admin_app`), with the bearer
    token in `FIVEFOLD_ADMIN_API_TOKEN`; None while that variable is unset or empty, and
    the gateway then serves no admin API. `environ` defaults to the process's environment.
    """
    environment = os.environ if environ is None else environ
    if not environment.get(TOKEN_VARIABLE):
        return None
    token = token_from_environment(environment)

    def factory(admin: GatewayAdmin, records: ActionStore) -> ASGIApp:
        app = admin_app(admin, records, token=token)
        app.add_exception_handler(AttestationsClosed, _attestations_closed)
        return app

    return factory


async def _attestations_closed(request: Request, error: Exception) -> Response:
    """503 for an attestation the gateway no longer takes: nothing was recorded. The body
    has the admin API's error shape."""
    return JSONResponse({"reason": "unavailable", "message": str(error)}, status_code=503)


# The gateway's end of the gw_sim hop ---------------------------------------------------------


class SimulatorLink:
    """The gateway's calls to the simulator's service, over HTTP, logged and faulted.

    It stands in for the harness's transport (`fivefold.harness.transport.Calls`), so the
    gateway's simulator client runs unchanged.
    """

    def __init__(
        self, scheduler: Scheduler, hops: Hops, client: httpx.AsyncClient, *, secret: str
    ) -> None:
        self._scheduler = scheduler
        self._hops = hops
        self._client = client
        self._secret = secret

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
        """Send one request and wait at most `timeout_ms` for its answer.

        Raises `TransportTimeout` when none arrives in time, when the connection fails,
        or when F2 drops the answer; the last waits out the timeout first, as a lost
        answer would. A write is logged at `sent_vt`, its attempt's recorded send time
        (`fivefold.harness.split.hops`).
        """
        request = self._hops.sent(
            hop=hop,
            kind="request",
            sender=sender,
            receiver=receiver,
            api=api,
            body=body,
            intent_id=intent_id,
            idempotency_key=idempotency_key,
            sent_vt=sent_vt,
        )
        deadline = self._scheduler.now + timeout_ms
        headers = {
            "authorization": f"Bearer {self._secret}",
            "content-type": "application/json",
            SEQ_HEADER: str(request.seq),
        }
        content = json.dumps(request.body, sort_keys=True)
        try:
            response = await self._scheduler.wait_for(
                self._client.post(f"/api/{api}", content=content, headers=headers), timeout_ms
            )
        except SchedulerTimeout:
            raise TransportTimeout(
                f"{api} to {receiver}: no answer within {timeout_ms} ms"
            ) from None
        except httpx.HTTPError as error:
            raise TransportTimeout(f"{api} to {receiver}: the connection failed: {error}") from None
        answer_seq = response.headers.get(SEQ_HEADER)
        if response.status_code >= 500 or answer_seq is None:
            return Reply(dict(SERVER_ERROR))
        reply_body = response.json()
        answer = Envelope(
            seq=int(answer_seq),
            hop=hop,
            kind="answer",
            sender=receiver,
            receiver=sender,
            api=api,
            correlation=request.seq,
            intent_id=intent_id,
            idempotency_key=idempotency_key,
            answer_class=answer_class(api, reply_body),
            body=reply_body,
            sent_vt=self._scheduler.now,
        )
        if self._hops.fire("net.gw_sim.answer", answer).dropped:
            self._hops.dropped(answer, "fault")
            await self._scheduler.sleep(max(0, deadline - self._scheduler.now))
            raise TransportTimeout(f"{api} to {receiver}: no answer within {timeout_ms} ms")
        self._hops.delivered(answer)
        return Reply(reply_body, answer.answer_class)


# The agent_gw hop -------------------------------------------------------------------------


class AgentHop:
    """The agent's MCP messages at the gateway's end of the `agent_gw` hop: a context-tier
    MCP middleware (`mcp.server.context.ServerMiddleware`)."""

    def __init__(self, service: GatewayService) -> None:
        self._service = service

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        serving = self._service.serving
        hops = serving.hops
        message = hops.sent(
            hop="agent_gw",
            kind="notification" if ctx.request_id is None else "request",
            sender=AGENT,
            receiver=GATEWAY,
            api=_api(ctx),
            body=_message_body(ctx),
            intent_id=_intent(ctx),
        )
        outcome = hops.fire("net.agent_gw.request", message)
        if outcome.dropped:
            hops.dropped(message, "fault")
            await serving.stopping.wait()
            raise MCPError(code=SERVICE_STOPPED, message="the run has stopped")
        hops.delivered(message)
        for fired in outcome.copies:
            self._deliver_copy_later(fired, message, ctx, call_next)
        if message.kind == "notification":
            return await call_next(replace(ctx, request=Delivery(message.seq)))
        try:
            result = await call_next(replace(ctx, request=Delivery(message.seq)))
        except Exception as error:
            self._answer(message, ctx, _error_body(ctx, error)).delivered()
            raise
        self._answer(message, ctx, _result_body(ctx, result)).delivered()
        return result

    def _answer(self, request: Envelope, ctx: ServerRequestContext[Any, Any], body: Any) -> _Sent:
        hops = self._service.serving.hops
        answer = hops.sent(
            hop="agent_gw",
            kind="answer",
            sender=GATEWAY,
            receiver=AGENT,
            api=request.api,
            body=body,
            intent_id=request.intent_id,
            correlation=request.seq,
            answer_class=answer_label(body),
        )
        return _Sent(hops, answer)

    def _deliver_copy_later(
        self,
        fired: Fired,
        original: Envelope,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> None:
        """F4: the gateway serves the message a second time, `copy_delay_ms` later; the
        answer to the copy is dropped (0006)."""
        serving = self._service.serving
        scheduler = serving.opened.scheduler
        copy = serving.hops.copy(fired, "net.agent_gw.request", original)

        async def arrives() -> None:
            serving.hops.delivered(copy)
            delivered = replace(ctx, request=Delivery(copy.seq))
            try:
                result = await call_next(delivered)
                body = _result_body(ctx, result)
            except Exception as error:
                body = _error_body(ctx, error)
            if copy.kind == "request":
                self._answer(copy, ctx, body).dropped()

        def arrive() -> None:
            scheduler.spawn(GATEWAY, arrives(), name=f"copy {copy.seq}")

        scheduler.call_later(
            fired.planned.param("copy_delay_ms"), arrive, label=f"deliver copy {copy.seq}"
        )


INVALID_PARAMS = -32602
"""JSON-RPC's code for invalid params: how the log names a request the SDK refused."""


@dataclass(frozen=True)
class _Sent:
    """An answer the gateway sent, whose fate comes next."""

    hops: Hops
    answer: Envelope

    def delivered(self) -> None:
        """The gateway hands the answer to the agent's connection."""
        self.hops.delivered(self.answer)

    def dropped(self) -> None:
        """The answer to a copy: the transport never delivers it (0006)."""
        self.hops.dropped(self.answer, "answer_to_copy")


def _api(ctx: ServerRequestContext[Any, Any]) -> str:
    """The message's call: a tool's name for `tools/call`, otherwise its method."""
    params = ctx.params
    if ctx.method == "tools/call" and isinstance(params, Mapping):
        return str(params.get("name"))
    return ctx.method


def _intent(ctx: ServerRequestContext[Any, Any]) -> str | None:
    params = ctx.params
    if ctx.method == "tools/call" and isinstance(params, Mapping):
        arguments = params.get("arguments")
        if isinstance(arguments, Mapping) and isinstance(arguments.get("intent_id"), str):
            return str(arguments["intent_id"])
    return None


def _message_body(ctx: ServerRequestContext[Any, Any]) -> dict[str, Any]:
    """The JSON-RPC message as the agent sent it."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": ctx.method}
    if ctx.request_id is not None:
        body["id"] = ctx.request_id
    if ctx.params is not None:
        body["params"] = dict(ctx.params)
    return body


def _result_body(ctx: ServerRequestContext[Any, Any], result: HandlerResult) -> dict[str, Any]:
    if isinstance(result, BaseModel):
        dumped: Any = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    else:
        dumped = {} if result is None else dict(result)
    return {"jsonrpc": "2.0", "id": ctx.request_id, "result": dumped}


def _error_body(ctx: ServerRequestContext[Any, Any], error: Exception) -> dict[str, Any]:
    """The JSON-RPC error a failed request answers with: an `MCPError`'s own, or, for a
    request the SDK refuses for another reason, such as malformed params, its message."""
    if isinstance(error, MCPError):
        dumped = error.error.model_dump(mode="json", by_alias=True, exclude_none=True)
    else:
        dumped = {"code": INVALID_PARAMS, "message": f"{type(error).__name__}: {error}"}
    return {"jsonrpc": "2.0", "id": ctx.request_id, "error": dumped}


# The admin path, with attestations in the harness's actor log ------------------------------------


ATTESTATION_WAIT_MS = 5_000
"""How long stop keeps the harness's records open, at most, for attestations in flight."""


class AttestationsClosed(RuntimeError):
    """The gateway takes no attestation: its run has begun to stop."""


class Attestations:
    """The admin path's attestations in flight, and whether the gateway still takes any.

    An attestation is in flight from the check that the gateway takes it, before its
    resolution commits in the gateway's records, to its row in the harness's actor log,
    which the evaluator counts (0004). Stop closes the gate first, so that no resolution
    commits once stop has begun, and closes the harness's records only once no
    attestation is in flight, or `ATTESTATION_WAIT_MS` later at most.

    A resolution runs on the gateway's event loop without a pause, so stop can begin
    inside one only from within it; the count keeps the records open for it all the same,
    and keeps them so if a resolution ever waits.
    """

    def __init__(self) -> None:
        self._closed = False
        self._in_flight = 0
        self._then: Callable[[], None] | None = None
        self._bound: asyncio.TimerHandle | None = None

    @contextmanager
    def attesting(self) -> Iterator[None]:
        """Hold one attestation in flight. Raises `AttestationsClosed` once closed."""
        if self._closed:
            raise AttestationsClosed("the run is stopping: the gateway takes no attestation now")
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._settled()

    def close(self) -> None:
        """Take no attestation from now on."""
        self._closed = True

    def when_settled(self, then: Callable[[], None], *, clock: HostClock, limit_ms: int) -> None:
        """Call `then` once no attestation is in flight: now, when the last one has logged
        its row, or `limit_ms` of the run's time later, whichever comes first."""
        self._then = then
        if self._in_flight == 0:
            self._settled()
        else:
            self._bound = clock.call_at(clock.now + limit_ms, self._settled)

    def _settled(self) -> None:
        then, self._then = self._then, None
        bound, self._bound = self._bound, None
        if bound is not None:
            bound.cancel()
        if then is not None:
            then()


class RecordedAdmin(GatewayAdmin):
    """The gateway's admin path, which also logs each attestation as the evaluator reads
    it (0004): in `harness.actor_log`, exactly as the scripted resolver logs its own.

    The row has actor `resolver`, action `attest`, the escalation as its subject, and
    details naming the resolution, the intent, the action and, for `committed`, the
    effect. Its `vt` is when the gateway recorded the attestation, and its seq is taken
    after that, when the stack has it (`fivefold.harness.actors`). A refused attestation
    logs nothing. Once the run has begun to stop, no attestation is taken: none commits,
    and the admin API answers 503 (`Attestations`).
    """

    def __init__(
        self,
        scheduler: Scheduler,
        store: ActionStore,
        dispatcher: Dispatcher,
        log: HarnessLog,
        attestations: Attestations,
    ) -> None:
        super().__init__(scheduler, store, dispatcher)
        self._records = store
        self._log = log
        self._attestations = attestations

    def resolve(
        self,
        escalation_id: str,
        resolution: Resolution,
        effect_id: str | None = None,
        *,
        actor: str,
    ) -> ActionResult:
        with self._attestations.attesting():
            result = super().resolve(escalation_id, resolution, effect_id, actor=actor)
            self._log_attestation(escalation_id, resolution, effect_id)
        return result

    def _log_attestation(
        self, escalation_id: str, resolution: Resolution, effect_id: str | None
    ) -> None:
        escalation = self._records.snapshot().escalations[escalation_id]
        if escalation.resolved_vt is None:
            raise RuntimeError(f"{escalation_id} was attested but is not resolved")
        action = self._records.view(escalation.action_id).action
        details: dict[str, object] = {
            "resolution": str(resolution),
            "intent_id": action.intent_id,
            "action_id": action.action_id,
        }
        if effect_id is not None:
            details["effect_id"] = effect_id
        self._log.actor_acted(
            ActorRecord(
                seq=self._log.next_seq(),
                actor="resolver",
                action=ATTEST,
                subject=escalation_id,
                details=details,
                vt=escalation.resolved_vt,
            )
        )


# The service --------------------------------------------------------------------------------


@dataclass
class _Serving:
    """What the service holds while it serves its run."""

    opened: OpenRun
    store: PgStore
    client: httpx.AsyncClient
    hops: Hops
    dispatcher: Dispatcher
    tools: GatewayTools
    attestations: Attestations
    admin: ASGIApp | None = None
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    draining: bool = False
    stopped: bool = False


class GatewayService:
    """The gateway of one split run, as an HTTP service."""

    def __init__(
        self,
        run: SplitRun,
        *,
        base_dsn: str,
        simulator_url: str,
        allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
        admin: AdminFactory | None = None,
    ) -> None:
        if not run.has_gateway:
            raise ValueError(f"run {run.run_id} is {run.config}, which has no gateway (0008)")
        self.run = run
        self._base_dsn = base_dsn
        self._simulator_url = simulator_url
        self._callers = run.callers()
        self._serving: _Serving | None = None
        server = worker_server(lambda: self.serving.tools, AGENT)
        server.middleware.append(AgentHop(self))
        origins = [f"http://{host}" for host in allowed_hosts]
        self._sessions = StreamableHTTPSessionManager(
            app=server,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=list(allowed_hosts),
                allowed_origins=origins,
            ),
        )
        self._mcp = StreamableHTTPASGIApp(self._sessions)
        self._admin = admin

    def app(self) -> Starlette:
        """The service's ASGI app."""
        routes: list[Route | Mount] = [
            Route("/mcp", endpoint=_AgentEndpoint(self)),
            Route("/health", self._health, methods=["GET"]),
            Route("/harness/{action}", self._control, methods=["POST"]),
        ]
        if self._admin is not None:
            # The gateway's admin API (spec, Gateway), built when the gateway starts, on
            # its admin path and records.
            routes.append(Mount("/admin", app=_AdminEndpoint(self)))
        return Starlette(routes=routes, lifespan=self._lifespan)

    @property
    def serving(self) -> _Serving:
        if self._serving is None:
            raise RuntimeError("the gateway's service has not started")
        return self._serving

    # Starting and stopping ----------------------------------------------------------------

    @asynccontextmanager
    async def _lifespan(self, app: Starlette) -> AsyncIterator[None]:
        opened = open_run(self.run, self._base_dsn)
        try:
            self._serving = self._start(opened)
            async with self._sessions.run():
                yield
        finally:
            if self._serving is not None:
                self._serving.attestations.close()
                self._serving.stopping.set()
                await self._serving.client.aclose()
                self._serving.store.close()
            opened.close()

    def _start(self, opened: OpenRun) -> _Serving:
        scenario, scheduler = opened.scenario, opened.scheduler
        params = scenario.params
        intents = {intent.id: str(intent.operation) for intent in scenario.intents}
        settings = GatewaySettings(
            approval_wait_ms=scenario.authority.approval_wait_ms,
            params=params.contract_params(),
            dispatch_timeout_ms=params.dispatch_timeout_ms,
            max_attempts=params.max_attempts,
            max_probes=params.max_probes,
            agent_workers=1,
        )
        store = PgStore(
            self.run.conninfo(self._base_dsn, "ff_gw"),
            task_id=scenario.task_id,
            intents=intents,
            contract=self.run.contract,
            settings=settings,
            identity=self.run.config == "G2",
        )
        client = httpx.AsyncClient(base_url=self._simulator_url, timeout=None)
        hops = Hops(scheduler, opened.log, FaultInjector(opened.plan))
        link = SimulatorLink(scheduler, hops, client, secret=self.run.secret(GATEWAY))
        simulator = SimClient(link, timeout_ms=params.dispatch_timeout_ms)
        dispatcher = Dispatcher(
            scheduler,
            store,
            simulator,
            RuleFreshness(store),
            component=GATEWAY,
            timing=Timing(
                approval_poll_ms=params.approval_poll_interval_ms,
                probe_interval_ms=params.status_probe_interval_ms,
            ),
        )
        sink = ReportSink(scheduler, opened.log, list(intents))
        tools = GatewayTools(
            scheduler,
            store,
            dispatcher,
            simulator,
            sink,
            task_id=scenario.task_id,
            intents=intents,
            timing=ToolTiming(tool_wait_ms=params.tool_wait_ms, max_wait_ms=params.max_wait_ms),
        )
        dispatcher.start()
        attestations = Attestations()
        admin: ASGIApp | None = None
        if self._admin is not None:
            recorded = RecordedAdmin(scheduler, store, dispatcher, opened.log, attestations)
            admin = self._admin(recorded, store)
        return _Serving(opened, store, client, hops, dispatcher, tools, attestations, admin)

    def drain(self) -> None:
        """The agents' phase is over: in G2, close the run if no report did (0003), and
        refuse further MCP requests. The dispatcher goes on."""
        serving = self.serving
        serving.draining = True
        if self.run.config == "G2" and not serving.store.snapshot().run_closed:
            serving.store.close_run(serving.opened.scheduler.now)

    def stop(self) -> None:
        """The run has stopped: take no attestation from now on, end the gateway's tasks
        and timers, and write no more harness records once the attestations in flight
        have logged theirs (`Attestations`)."""
        serving = self.serving
        serving.draining = serving.stopped = True
        serving.attestations.close()
        serving.opened.scheduler.stop()
        serving.stopping.set()
        serving.attestations.when_settled(
            serving.opened.stop_records, clock=serving.opened.clock, limit_ms=ATTESTATION_WAIT_MS
        )

    # Routes -------------------------------------------------------------------------------

    async def serve_agent(self, scope: Scope, receive: Receive, send: Send) -> None:
        """The MCP endpoint: the session manager's, until the agents' phase is over."""
        serving = self.serving
        if serving.draining:
            body = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": SERVICE_STOPPED, "message": "the run's agents' phase is over"},
            }
            await JSONResponse(body, status_code=503)(scope, receive, send)
            return
        await self._mcp(scope, receive, send)

    async def _health(self, request: Request) -> Response:
        serving = self.serving
        status = "stopped" if serving.stopped else "draining" if serving.draining else "serving"
        failures = len(serving.opened.scheduler.failures)
        return JSONResponse({"run_id": self.run.run_id, "status": status, "failures": failures})

    async def _control(self, request: Request) -> Response:
        if self._callers.identify(request.headers.get("authorization")) != HARNESS:
            return JSONResponse({"error": "forbidden"}, status_code=401)
        action = request.path_params["action"]
        if action == "drain":
            self.drain()
            return JSONResponse({"status": "draining"})
        if action == "stop":
            self.stop()
            return JSONResponse({"status": "stopped"})
        if self.serving.stopped:
            return unavailable("the run has stopped")
        return JSONResponse({"error": "unknown_action", "detail": action}, status_code=404)


class _AgentEndpoint:
    """The MCP endpoint as an ASGI app, which Starlette routes to as it is."""

    def __init__(self, service: GatewayService) -> None:
        self._service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._service.serve_agent(scope, receive, send)


class _AdminEndpoint:
    """The admin API of the running gateway, until the run stops."""

    def __init__(self, service: GatewayService) -> None:
        self._service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        serving = self._service.serving
        if serving.stopped or serving.admin is None:
            await unavailable("the run has stopped")(scope, receive, send)
            return
        await serving.admin(scope, receive, send)
