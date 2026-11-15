"""The gateway's admin API over HTTP, built with FastAPI (0002 rule 22, 0003, 0010).

`admin_app` serves one gateway's admin path as JSON. It lists the gateway's actions,
grants and action events and its open escalations, and it resolves an escalation when a
human attests what happened: committed, naming the effect, or not committed (0003,
escalation). It never issues or revokes approvals; that happens in the approvals service
(0002, rule 22). In-process runs and the demos call `GatewayAdmin` directly, as the
scripted resolver does. This app is for a gateway served over HTTP, which mounts it, for
example under `/admin`. Paths below are relative to that mount.

- `GET /actions`: `actions`, every action in action-id order, with its state, intent,
  operation, target, arguments, grant, the approval its grant names and its effect id.
- `GET /actions/{action_id}`: the `action`, its `escalation` (or null) and its
  `events`, in seq order.
- `GET /grants`: `grants`, every grant in action-id order.
- `GET /events?from_seq=N`: `events`, the run's action events with seq N or more, in
  seq order. N defaults to 0, which gives all of them.
- `GET /escalations`: `escalations`, the open ones, as `GatewayAdmin.open_escalations`
  lists them, each with `not_committed_from_vt`.
- `POST /escalations/{escalation_id}/resolve`: the action's result after the
  attestation, in the shape the agent sees (0003).

The resolve request is `{"resolution": "committed", "effect_id": "PO-0001", "actor":
"ops-1"}`, or `not_committed` with no effect id. It goes through `GatewayAdmin.resolve`,
so the store's rules and the dispatcher's wake apply unchanged: T11 or T12, with reason
`human_attested`, and every wait on the action ends at once.

Every error answers `{"reason": ..., "message": ...}`:

- 401 without the bearer token: `missing_token`, `wrong_scheme` or `invalid_token`;
- 404 for an unknown action, escalation or path: `unknown_action`, `unknown_escalation`
  or `not_found` (405 `method_not_allowed` for a known path with another method);
- 409 for a step the store refused: `escalation_resolved`, with the `escalation` as it
  was resolved, or `before_settle_time`, with `not_committed_from_vt`, when "not
  committed" comes before S past the action's latest send (0010, reading 8), or
  `step_refused` for any other refusal, with the store's message;
- 422 `invalid_request` for input that does not parse;
- 500 `internal_error` for anything else.

Authentication: every HTTP request needs `Authorization: Bearer TOKEN` (RFC 6750), with
the token the app was built with, before routing, so an unknown path needs it too. A
deployment reads the token from `FIVEFOLD_ADMIN_API_TOKEN` (`token_from_environment`).
The app compares SHA-256 digests of the two tokens in constant time, so how long a
refusal takes says nothing about the token, not even its length. The app refuses to be
built with an empty token, because the admin API attests outcomes, or with a token that
RFC 6750 does not allow.

Time and reach: the app reads time only through `GatewayAdmin`, from the gateway's
scheduler (0009). It never contacts the simulator, and holds no simulator client and no
credential: it reads the gateway's records and calls the admin path. FastAPI's own
telemetry is off, so a request reads no wall clock, and no environment variable can make
the app export anything.

Every handler is a coroutine, so FastAPI runs it on the event loop that serves the app,
never in a worker thread. Serve the app on the gateway's own event loop: the handlers
call the store and the dispatcher directly, a store is used from one thread at a time,
and the dispatcher's wake sets the loop's events. Under the deterministic scheduler, a
request made in a scheduler event, such as one through httpx's ASGI transport, waits on
no timer and on no real wait, so `settle` lets it finish before the next event fires:
it acts at that event's virtual time, as a call to `GatewayAdmin` there would.

FastAPI and Starlette APIs used, checked in the installed source (fastapi 0.162.7,
starlette 1.10.0): `FastAPI(openapi_url=, docs_url=, redoc_url=, telemetry=)`,
`FastAPI.get`, `FastAPI.post`, `FastAPI.exception_handler`, `FastAPI.add_middleware`,
`fastapi.Query`, `fastapi.exceptions.RequestValidationError.errors`,
`fastapi.responses.JSONResponse`, `starlette.exceptions.HTTPException`,
`starlette.datastructures.Headers(scope=)` and the types in `starlette.types`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from typing import Annotated, Protocol

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from fivefold.domain.api import Id
from fivefold.gateway import wire
from fivefold.gateway.admin import (
    BeforeSettleTime,
    EscalationResolved,
    GatewayAdmin,
    OpenEscalation,
    UnknownEscalation,
)
from fivefold.gateway.codec import to_json
from fivefold.gateway.records import Action, ActionEvent, Escalation, Grant, Resolution, Snapshot
from fivefold.gateway.steps import StepRefused, action_number
from fivefold.gateway.store import UnknownAction, view_of

TOKEN_VARIABLE = "FIVEFOLD_ADMIN_API_TOKEN"
"""The environment variable a deployment reads the admin API's bearer token from."""

REALM = "fivefold-admin"
"""The realm a 401 answer names in its `WWW-Authenticate` header (RFC 6750)."""

_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
"""RFC 6750's `b64token`: what a bearer token may hold."""

Json = dict[str, object]

Actor = Annotated[str, StringConstraints(pattern=r"^[^\x00-\x1f\x7f]{1,200}$")]
"""The human who attests: 1 to 200 characters, no control characters."""


class GatewayRecords(Protocol):
    """The admin API's read path: the gateway's records as one consistent snapshot.

    Every `ActionStore` is one. A gateway may pass the store its admin path uses, or a
    further store of the same run on its own connection.
    """

    def snapshot(self) -> Snapshot:
        """Every gateway record of the run, as one read sees them."""
        ...


class Attestation(BaseModel):
    """What a human attests about an escalated action (0003, escalation)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolution: Resolution
    effect_id: Id | None = None
    actor: Actor

    @model_validator(mode="after")
    def _effect_fits(self) -> Attestation:
        if (self.resolution is Resolution.COMMITTED) != (self.effect_id is not None):
            raise ValueError("effect_id names the effect for committed, and only for committed")
        return self


def token_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """The admin API's bearer token, from `FIVEFOLD_ADMIN_API_TOKEN`.

    Raises `ValueError` when the variable is missing or empty. `environ` defaults to the
    process's environment.
    """
    token = (os.environ if environ is None else environ).get(TOKEN_VARIABLE, "")
    if not token:
        raise ValueError(f"set {TOKEN_VARIABLE} to the admin API's bearer token")
    return token


def admin_app(admin: GatewayAdmin, records: GatewayRecords, *, token: str) -> FastAPI:
    """The admin API of one gateway, as an ASGI app; see the module docstring.

    `admin` is the gateway's admin path, `records` its read path and `token` the bearer
    token every request must carry. Raises `ValueError` for an empty token, or one with a
    character RFC 6750 does not allow.
    """
    if not token:
        raise ValueError("the admin API needs a bearer token: it attests outcomes")
    if not _TOKEN.fullmatch(token):
        raise ValueError(
            "a bearer token holds letters, digits and -._~+/, then any number of = (RFC 6750)"
        )
    app = FastAPI(
        title="Fivefold gateway admin API",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        telemetry={"tracing": False, "metrics": False, "logs": False, "auto_configure": False},
    )
    app.add_middleware(BearerToken, digest=_digest(token))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        reasons = {404: "not_found", 405: "method_not_allowed"}
        reason = reasons.get(error.status_code, "http_error")
        return _error(error.status_code, reason, str(error.detail), headers=error.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_request", _problems(error))

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        return _error(500, "internal_error", "the gateway could not serve this request")

    @app.get("/actions")
    async def list_actions() -> JSONResponse:
        snapshot = records.snapshot()
        actions = sorted(snapshot.actions.values(), key=lambda a: action_number(a.action_id))
        return JSONResponse({"actions": [_action(a, _grant_of(snapshot, a)) for a in actions]})

    @app.get("/actions/{action_id}")
    async def get_action(action_id: str) -> JSONResponse:
        try:
            view = view_of(records.snapshot(), action_id)
        except UnknownAction:
            return _error(404, "unknown_action", f"no action {action_id}")
        escalation = view.escalation
        return JSONResponse(
            {
                "action": _action(view.action, view.grant),
                "escalation": None if escalation is None else _escalation(escalation),
                "events": [_event(event) for event in view.events],
            }
        )

    @app.get("/grants")
    async def list_grants() -> JSONResponse:
        grants = sorted(
            records.snapshot().grants.values(), key=lambda g: action_number(g.action_id)
        )
        return JSONResponse({"grants": [_grant(grant) for grant in grants]})

    @app.get("/events")
    async def list_events(from_seq: Annotated[int, Query(ge=0)] = 0) -> JSONResponse:
        events = [_event(e) for e in records.snapshot().events if e.seq >= from_seq]
        return JSONResponse({"events": events})

    @app.get("/escalations")
    async def list_escalations() -> JSONResponse:
        escalations = [_open_escalation(e) for e in admin.open_escalations()]
        return JSONResponse({"escalations": escalations})

    @app.post("/escalations/{escalation_id}/resolve")
    async def resolve(escalation_id: str, attestation: Attestation) -> JSONResponse:
        try:
            result = admin.resolve(
                escalation_id,
                attestation.resolution,
                attestation.effect_id,
                actor=attestation.actor,
            )
        except UnknownEscalation:
            return _error(404, "unknown_escalation", f"no escalation {escalation_id}")
        except EscalationResolved as refused:
            resolved = _escalation(refused.escalation)
            return _error(409, "escalation_resolved", str(refused), escalation=resolved)
        except BeforeSettleTime as refused:
            due = refused.not_committed_from_vt
            return _error(409, "before_settle_time", str(refused), not_committed_from_vt=due)
        except StepRefused as refused:
            return _error(409, "step_refused", str(refused))
        except ValueError as invalid:
            return _error(422, "invalid_request", str(invalid))
        return JSONResponse(wire.action(result))

    return app


class BearerToken:
    """ASGI middleware that refuses every HTTP request without the app's bearer token.

    It runs before routing, so every path needs the token. `digest` is the SHA-256 of
    the token. Other scopes pass through: the lifespan, and WebSockets, which no route
    serves, so the router closes them.
    """

    def __init__(self, app: ASGIApp, digest: bytes) -> None:
        self._app = app
        self._digest = digest

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            refusal = self._refusal(Headers(scope=scope).get("authorization"))
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        await self._app(scope, receive, send)

    def _refusal(self, header: str | None) -> JSONResponse | None:
        """The 401 answer for this Authorization header, or None when the token is right."""
        if header is None:
            return _unauthorized("missing_token", "send Authorization: Bearer TOKEN")
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() != "bearer":
            return _unauthorized("wrong_scheme", "the admin API takes a bearer token")
        if not hmac.compare_digest(_digest(credentials.strip()), self._digest):
            return _unauthorized("invalid_token", "this is not the gateway's admin token")
        return None


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _unauthorized(reason: str, message: str) -> JSONResponse:
    challenge = f'Bearer realm="{REALM}"'
    if reason == "invalid_token":
        challenge += ', error="invalid_token"'
    return _error(401, reason, message, headers={"WWW-Authenticate": challenge})


def _error(
    status: int,
    reason: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
    **extra: object,
) -> JSONResponse:
    body: Json = {"reason": reason, "message": message, **extra}
    return JSONResponse(body, status_code=status, headers=headers)


def _problems(error: RequestValidationError) -> str:
    """One clause per problem, naming where it is: `body.resolution: Input should be ...`."""
    lines = []
    for problem in error.errors():
        where = ".".join(str(part) for part in problem.get("loc", ()))
        message = str(problem.get("msg", "invalid"))
        lines.append(f"{where}: {message}" if where else message)
    return "; ".join(lines)


# Records as JSON ---------------------------------------------------------------------------


def _grant_of(snapshot: Snapshot, action: Action) -> Grant | None:
    return None if action.grant_id is None else snapshot.grants.get(action.grant_id)


def _action(action: Action, grant: Grant | None) -> Json:
    return {
        "action_id": action.action_id,
        "intent_id": action.intent_id,
        "operation": action.operation,
        "target": action.target,
        "args": dict(action.args),
        "state": str(action.state),
        "contract": str(action.contract),
        "grant_id": action.grant_id,
        "approval_id": None if grant is None else grant.approval_id,
        "effect_id": action.effect_id,
        "created_vt": action.created_vt,
        "updated_vt": action.updated_vt,
    }


def _grant(grant: Grant) -> Json:
    return {
        "grant_id": grant.grant_id,
        "action_id": grant.action_id,
        "approval_id": grant.approval_id,
        "state": str(grant.state),
        "requested_vt": grant.requested_vt,
        "consumed_vt": grant.consumed_vt,
    }


def _event(event: ActionEvent) -> Json:
    return {
        "seq": event.seq,
        "action_id": event.action_id,
        "kind": str(event.kind),
        "from_state": None if event.from_state is None else str(event.from_state),
        "to_state": str(event.to_state),
        "reason": str(event.reason),
        "details": to_json(event.details),
        "vt": event.vt,
    }


def _escalation(escalation: Escalation) -> Json:
    return {
        "escalation_id": escalation.escalation_id,
        "action_id": escalation.action_id,
        "opened_vt": escalation.opened_vt,
        "resolved_vt": escalation.resolved_vt,
        "resolution": None if escalation.resolution is None else str(escalation.resolution),
        "effect_id": escalation.effect_id,
        "actor": escalation.actor,
    }


def _open_escalation(escalation: OpenEscalation) -> Json:
    return {
        "escalation_id": escalation.escalation_id,
        "action_id": escalation.action_id,
        "intent_id": escalation.intent_id,
        "operation": escalation.operation,
        "target": escalation.target,
        "args": dict(escalation.args),
        "approval_id": escalation.approval_id,
        "opened_vt": escalation.opened_vt,
        "last_sent_vt": escalation.last_sent_vt,
        "not_committed_from_vt": escalation.not_committed_from_vt,
    }
