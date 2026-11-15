"""The gateway's admin API over HTTP (0002 rule 22, 0003, 0010).

The app serves an in-memory store behind a stand-in world: an approvals service that
approves every request at once, and a C0 destination that can lose the order's answer or
its request, so the gateway escalates the action at once. Every request goes through
httpx's ASGI transport, in process. A request made in a scheduler event acts at that
event's virtual time: nothing in the transport or the app waits on a timer or a real
wait, so `settle` lets the request finish before the scheduler fires the next event.
Every test pins the virtual times it checks.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.gateway.admin import GatewayAdmin
from fivefold.gateway.admin_api import TOKEN_VARIABLE, admin_app, token_from_environment
from fivefold.gateway.destination import ApprovalNow, FactNow
from fivefold.gateway.dispatcher import Dispatcher
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.records import (
    Action,
    ActionResult,
    Answer,
    ApprovalStatus,
    Attempt,
    BasisCheck,
    ProbeAnswer,
    Resolution,
    Snapshot,
    WriteCall,
)
from fivefold.gateway.state_machine import (
    FINAL_STATES,
    ActionState,
    AnswerClass,
    GatewaySettings,
    Reason,
)
from fivefold.gateway.steps import StepRefused
from fivefold.harness.scheduler import Scheduler

S = ActionState
SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
)
T = SETTINGS.dispatch_timeout_ms
SETTLE = SETTINGS.settle_ms
HOP = 20
"""The stand-in's one-way latency, in virtual ms."""
FAR = 10**9
TIMEOUT = Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)

SENT = 160
"""When the gateway sends the escalated action's only attempt."""
OPENED = SENT + T
"""When the gateway escalates it: at once, on the timeout (C0)."""

TOKEN = "admin-token-1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RESOLVE = "/escalations/ESC-0001/resolve"
COMMITTED = {"resolution": "committed", "effect_id": "PO-0001", "actor": "ops-1"}
NOT_COMMITTED = {"resolution": "not_committed", "actor": "ops-1"}


@dataclass
class World:
    """The approvals service and a C0 destination, as the dispatcher reaches them."""

    scheduler: Scheduler
    effects: dict[str, str] = field(default_factory=dict)
    lose_requests: int = 0
    lose_answers: int = 0
    calls: int = 0
    """Every call the gateway made."""

    async def facts(self, keys: Sequence[str]) -> Mapping[str, FactNow]:
        self.calls += 1
        await self.scheduler.sleep(2 * HOP)
        return {key: FactNow(key, 1, {}) for key in keys}

    async def request_approval(self, action: Action) -> ApprovalNow:
        self.calls += 1
        await self.scheduler.sleep(2 * HOP)
        return ApprovalNow(f"APR-{action.action_id[2:]}", ApprovalStatus.APPROVED, FAR)

    async def approval(self, approval_id: str) -> ApprovalNow:
        self.calls += 1
        await self.scheduler.sleep(2 * HOP)
        return ApprovalNow(approval_id, ApprovalStatus.APPROVED, FAR)

    async def send(self, action: Action, attempt: Attempt, approval_id: str) -> Answer:
        self.calls += 1
        if self.lose_requests:
            self.lose_requests -= 1
            await self.scheduler.sleep(T)
            return TIMEOUT
        await self.scheduler.sleep(HOP)
        effect_id = f"PO-{len(self.effects) + 1:04d}"
        self.effects[effect_id] = approval_id
        if self.lose_answers:
            self.lose_answers -= 1
            await self.scheduler.sleep(T - HOP)
            return TIMEOUT
        await self.scheduler.sleep(HOP)
        return Answer(AnswerClass.COMMITTED, Reason.COMMITTED, effect_id)

    async def lookup(self, action: Action) -> ProbeAnswer:
        self.calls += 1
        await self.scheduler.sleep(2 * HOP)
        return ProbeAnswer(answered=True, effect_ids=tuple(self.effects), as_of_vt=0)


class Fresh:
    """Every basis is fresh."""

    def keys(self, action: Action) -> Sequence[str]:
        return ["quote/S-1/W-100", "budget/CC-OPS"]

    def check(self, action: Action, now: Mapping[str, FactNow], *, full: bool) -> BasisCheck:
        return BasisCheck(True, True if full else None)


ORDER = WriteCall(
    "REQ-1",
    "submit_purchase_order",
    "supplier/S-1",
    {"supplier_id": "S-1", "quantity": 100},
    "key",
    (),
    (),
)


class Gateway:
    """A C0 gateway on a scheduler, with its admin path and its admin API."""

    def __init__(self) -> None:
        self.scheduler = Scheduler()
        self.world = World(self.scheduler)
        self.store = MemoryStore(
            task_id="TASK-1",
            intents={"REQ-1": "submit_purchase_order"},
            contract=Contract.C0,
            settings=SETTINGS,
        )
        self.dispatcher = Dispatcher(self.scheduler, self.store, self.world, Fresh())
        self.admin = GatewayAdmin(self.scheduler, self.store, self.dispatcher)
        self.app = admin_app(self.admin, self.store, token=TOKEN)

    def escalate(self, **world: int) -> None:
        """Propose the order at vt 0 and run to vt 1,000: the gateway sends it at 160 and
        escalates it at 360, when no answer came."""
        for name, value in world.items():
            setattr(self.world, name, value)

        async def main() -> None:
            result = self.store.propose(ORDER, self.scheduler.now)
            assert isinstance(result, ActionResult)
            self.dispatcher.drive(result.action_id)
            await self.scheduler.run(deadline_ms=1_000)

        asyncio.run(main())
        (attempt,) = self.store.view("A-0001").attempts
        escalation = self.store.view("A-0001").escalation
        assert escalation is not None
        assert (attempt.sent_vt, escalation.opened_vt) == (SENT, OPENED)

    def state(self) -> ActionState:
        return self.store.view("A-0001").action.state


def escalated(**world: int) -> Gateway:
    """A gateway whose order is escalated, its answer lost unless `world` says otherwise."""
    gateway = Gateway()
    gateway.escalate(**({"lose_answers": 1} | world))
    return gateway


async def _send(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] = AUTH,
    raise_app_exceptions: bool = True,
    **kwargs: Any,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        return await client.request(method, path, headers=dict(headers), **kwargs)


def call(app: FastAPI, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """One request, outside any run."""
    return asyncio.run(_send(app, method, path, **kwargs))


def call_at(gateway: Gateway, vt: int, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """One request made in a scheduler event at `vt`; the run goes on until nothing is left.

    The request runs as a task the event spawns, as a client in the gateway's loop would.
    """
    responses: list[httpx.Response] = []

    async def request() -> None:
        responses.append(await _send(gateway.app, method, path, **kwargs))

    def spawn() -> None:
        gateway.scheduler.spawn("operator", request(), name=f"{method} {path}")

    async def main() -> None:
        gateway.scheduler.call_at(vt, spawn)
        await gateway.scheduler.run(deadline_ms=100_000)

    asyncio.run(main())
    (response,) = responses
    return response


# Authentication ------------------------------------------------------------------------------

EVERY_REQUEST = [
    ("GET", "/actions", None),
    ("GET", "/actions/A-0001", None),
    ("GET", "/grants", None),
    ("GET", "/events", None),
    ("GET", "/escalations", None),
    ("POST", RESOLVE, COMMITTED),
    ("GET", "/no-such-path", None),
]


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({}, "missing_token"),
        ({"Authorization": "Basic b3BzOmFkbWluLXRva2VuLTE="}, "wrong_scheme"),
        ({"Authorization": f"Token {TOKEN}"}, "wrong_scheme"),
        ({"Authorization": "Bearer admin-token-2"}, "invalid_token"),
    ],
    ids=["no header", "basic", "another scheme", "wrong token"],
)
@pytest.mark.parametrize(("method", "path", "body"), EVERY_REQUEST, ids=lambda v: str(v))
def test_every_request_needs_the_bearer_token(
    method: str, path: str, body: Mapping[str, str] | None, headers: dict[str, str], reason: str
) -> None:
    gateway = escalated()
    before = gateway.store.snapshot()
    response = call(gateway.app, method, path, headers=headers, json=body)
    assert response.status_code == 401
    assert response.json()["reason"] == reason
    challenge = 'Bearer realm="fivefold-admin"'
    if reason == "invalid_token":
        challenge += ', error="invalid_token"'
    assert response.headers["www-authenticate"] == challenge
    assert gateway.store.snapshot() == before, "a refused request changes nothing"


@pytest.mark.parametrize(
    ("header", "status"),
    [
        (f"Bearer {TOKEN}", 200),
        (f"bearer {TOKEN}", 200),
        (f"BEARER  {TOKEN}", 200),
        (f"Bearer {TOKEN}x", 401),
        (f"Bearer {TOKEN[:-1]}", 401),
        ("Bearer ", 401),
        ("Bearer", 401),
    ],
)
def test_the_scheme_takes_any_case_and_the_token_must_match_whole(header: str, status: int) -> None:
    response = call(escalated().app, "GET", "/actions", headers={"Authorization": header})
    assert response.status_code == status


@pytest.mark.parametrize("token", ["", "has space", "line\nbreak", "café", "=leading"])
def test_the_app_refuses_an_empty_token_or_one_rfc_6750_does_not_allow(token: str) -> None:
    gateway = Gateway()
    with pytest.raises(ValueError, match="token"):
        admin_app(gateway.admin, gateway.store, token=token)


def test_a_token_may_hold_every_character_rfc_6750_allows() -> None:
    gateway = Gateway()
    token = "aZ09-._~+/=="
    app = admin_app(gateway.admin, gateway.store, token=token)
    response = call(app, "GET", "/actions", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_a_deployment_reads_the_token_from_its_environment() -> None:
    assert TOKEN_VARIABLE == "FIVEFOLD_ADMIN_API_TOKEN"
    assert token_from_environment({TOKEN_VARIABLE: TOKEN}) == TOKEN
    for environ in ({}, {TOKEN_VARIABLE: ""}):
        with pytest.raises(ValueError, match=TOKEN_VARIABLE):
            token_from_environment(environ)


# Reading the records ---------------------------------------------------------------------------

ACTION = {
    "action_id": "A-0001",
    "intent_id": "REQ-1",
    "operation": "submit_purchase_order",
    "target": "supplier/S-1",
    "args": {"supplier_id": "S-1", "quantity": 100},
    "state": "outcome_unknown",
    "contract": "C0",
    "grant_id": "G-0001",
    "approval_id": "APR-0001",
    "effect_id": None,
    "created_vt": 0,
    "updated_vt": OPENED,
}

EVENTS = [
    {
        "seq": 1,
        "action_id": "A-0001",
        "kind": "transition",
        "from_state": None,
        "to_state": "proposed",
        "reason": "admitted",
        "details": {},
        "vt": 0,
    },
    {
        "seq": 2,
        "action_id": "A-0001",
        "kind": "transition",
        "from_state": "proposed",
        "to_state": "pending_approval",
        "reason": "approval_requested",
        "details": {"approval_id": "APR-0001"},
        "vt": 80,
    },
    {
        "seq": 3,
        "action_id": "A-0001",
        "kind": "transition",
        "from_state": "pending_approval",
        "to_state": "authorized",
        "reason": "approved",
        "details": {},
        "vt": 80,
    },
    {
        "seq": 4,
        "action_id": "A-0001",
        "kind": "transition",
        "from_state": "authorized",
        "to_state": "dispatched",
        "reason": "dispatched",
        "details": {"attempt_no": 1},
        "vt": SENT,
    },
    {
        "seq": 5,
        "action_id": "A-0001",
        "kind": "transition",
        "from_state": "dispatched",
        "to_state": "outcome_unknown",
        "reason": "timeout",
        "details": {"attempt_no": 1},
        "vt": OPENED,
    },
    {
        "seq": 6,
        "action_id": "A-0001",
        "kind": "note",
        "from_state": "outcome_unknown",
        "to_state": "outcome_unknown",
        "reason": "escalated",
        "details": {"escalation_id": "ESC-0001", "trigger": "at_once"},
        "vt": OPENED,
    },
]

OPEN_ESCALATION = {
    "escalation_id": "ESC-0001",
    "action_id": "A-0001",
    "intent_id": "REQ-1",
    "operation": "submit_purchase_order",
    "target": "supplier/S-1",
    "args": {"supplier_id": "S-1", "quantity": 100},
    "approval_id": "APR-0001",
    "opened_vt": OPENED,
    "last_sent_vt": SENT,
    "not_committed_from_vt": SENT + SETTLE,
}


def test_actions_lists_each_action_with_its_state_and_the_approval_its_grant_names() -> None:
    response = call(escalated().app, "GET", "/actions")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"actions": [ACTION]}


def test_an_action_whose_approval_is_not_filed_yet_names_no_grant_and_no_approval() -> None:
    gateway = Gateway()
    gateway.store.propose(ORDER, 0)
    (listed,) = call(gateway.app, "GET", "/actions").json()["actions"]
    assert listed["state"] == "proposed"
    assert (listed["grant_id"], listed["approval_id"]) == (None, None)


def test_one_action_comes_with_its_escalation_and_its_events_in_seq_order() -> None:
    response = call(escalated().app, "GET", "/actions/A-0001")
    assert response.status_code == 200
    assert response.json() == {
        "action": ACTION,
        "escalation": {
            "escalation_id": "ESC-0001",
            "action_id": "A-0001",
            "opened_vt": OPENED,
            "resolved_vt": None,
            "resolution": None,
            "effect_id": None,
            "actor": None,
        },
        "events": EVENTS,
    }


def test_an_unknown_action_is_404() -> None:
    response = call(escalated().app, "GET", "/actions/A-0009")
    assert response.status_code == 404
    assert response.json() == {"reason": "unknown_action", "message": "no action A-0009"}


def test_grants_lists_every_grant() -> None:
    response = call(escalated().app, "GET", "/grants")
    assert response.json() == {
        "grants": [
            {
                "grant_id": "G-0001",
                "action_id": "A-0001",
                "approval_id": "APR-0001",
                "state": "consumed",
                "requested_vt": 80,
                "consumed_vt": SENT,
            }
        ]
    }


def test_events_lists_the_runs_events_in_seq_order_from_a_given_seq() -> None:
    app = escalated().app
    assert call(app, "GET", "/events").json() == {"events": EVENTS}
    assert call(app, "GET", "/events", params={"from_seq": 5}).json() == {"events": EVENTS[4:]}
    assert call(app, "GET", "/events", params={"from_seq": 7}).json() == {"events": []}
    for bad in ("-1", "x", "1.5"):
        response = call(app, "GET", "/events", params={"from_seq": bad})
        assert response.status_code == 422
        assert response.json()["reason"] == "invalid_request"
        assert response.json()["message"].startswith("query.from_seq: ")


def test_escalations_lists_the_open_ones_as_the_admin_path_does() -> None:
    gateway = escalated()
    response = call(gateway.app, "GET", "/escalations")
    assert response.json() == {"escalations": [OPEN_ESCALATION]}
    (listed,) = gateway.admin.open_escalations()
    assert listed.not_committed_from_vt == SENT + SETTLE


# Resolving an escalation ------------------------------------------------------------------------


def test_committed_confirms_the_action_at_the_virtual_time_of_the_request() -> None:
    gateway = escalated()
    response = call_at(gateway, 2_000, "POST", RESOLVE, json=COMMITTED)
    assert response.status_code == 200
    assert response.json() == {
        "result": "action",
        "action_id": "A-0001",
        "intent_id": "REQ-1",
        "state": "confirmed",
        "effect_id": "PO-0001",
        "reason": "human_attested",
        "stale": [],
    }
    view = gateway.store.view("A-0001")
    last = view.events[-1]
    assert (last.to_state, last.reason, last.vt) == (S.CONFIRMED, Reason.HUMAN_ATTESTED, 2_000)
    assert last.details == {"effect_id": "PO-0001", "actor": "ops-1"}
    assert view.action.effect_id == "PO-0001"
    escalation = view.escalation
    assert escalation is not None
    assert (escalation.resolved_vt, escalation.resolution) == (2_000, Resolution.COMMITTED)
    assert (escalation.effect_id, escalation.actor) == ("PO-0001", "ops-1")
    assert call(gateway.app, "GET", "/escalations").json() == {"escalations": []}
    resolved = call(gateway.app, "GET", "/actions/A-0001").json()["escalation"]
    assert resolved["resolved_vt"] == 2_000 and resolved["actor"] == "ops-1"


def test_not_committed_is_409_before_the_settle_time_and_accepted_at_it() -> None:
    """0010, reading 8, and its consequence: the refusal says when it becomes possible."""
    gateway = escalated(lose_answers=0, lose_requests=1)
    due = SENT + SETTLE
    early = call_at(gateway, due - 1, "POST", RESOLVE, json=NOT_COMMITTED)
    assert early.status_code == 409
    assert early.json() == {
        "reason": "before_settle_time",
        "message": "A-0001: nothing can be attested as not committed before vt 1210,"
        " S after the last send",
        "not_committed_from_vt": due,
    }
    assert gateway.state() is S.OUTCOME_UNKNOWN
    response = call_at(gateway, due, "POST", RESOLVE, json=NOT_COMMITTED)
    assert response.status_code == 200
    assert (response.json()["state"], response.json()["reason"]) == ("failed", "human_attested")
    last = gateway.store.view("A-0001").events[-1]
    assert (last.to_state, last.reason, last.vt) == (S.FAILED, Reason.HUMAN_ATTESTED, due)


def test_a_resolved_escalation_is_409_and_says_how_it_was_resolved() -> None:
    gateway = escalated()
    call_at(gateway, 2_000, "POST", RESOLVE, json=COMMITTED)
    again = call_at(gateway, 3_000, "POST", RESOLVE, json=NOT_COMMITTED)
    assert again.status_code == 409
    assert again.json() == {
        "reason": "escalation_resolved",
        "message": "ESC-0001 is resolved already: committed, attested by ops-1 at vt 2000",
        "escalation": {
            "escalation_id": "ESC-0001",
            "action_id": "A-0001",
            "opened_vt": OPENED,
            "resolved_vt": 2_000,
            "resolution": "committed",
            "effect_id": "PO-0001",
            "actor": "ops-1",
        },
    }
    assert gateway.store.view("A-0001").events[-1].vt == 2_000, "nothing changed at 3,000"


def test_an_unknown_escalation_is_404() -> None:
    gateway = escalated()
    response = call(gateway.app, "POST", "/escalations/ESC-0009/resolve", json=COMMITTED)
    assert response.status_code == 404
    assert response.json() == {"reason": "unknown_escalation", "message": "no escalation ESC-0009"}
    assert gateway.state() is S.OUTCOME_UNKNOWN


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"content": b"not json", "headers": AUTH | {"content-type": "application/json"}},
        {"content": b'{"resolution": "committed", "effect_id": "PO-0001", "actor": "ops-1"}'},
        {"json": ["committed", "PO-0001", "ops-1"]},
        {"json": {}},
        {"json": {"resolution": "maybe", "actor": "ops-1"}},
        {"json": {"resolution": "committed", "actor": "ops-1"}},
        {"json": NOT_COMMITTED | {"effect_id": "PO-0001"}},
        {"json": COMMITTED | {"effect_id": "PO 0001"}},
        {"json": COMMITTED | {"actor": ""}},
        {"json": COMMITTED | {"actor": "ops\n1"}},
        {"json": COMMITTED | {"actor": "x" * 201}},
        {"json": COMMITTED | {"note": "extra"}},
    ],
    ids=[
        "not json",
        "no content type",
        "an array",
        "empty",
        "unknown resolution",
        "committed without an effect",
        "not committed with an effect",
        "an effect id with a space",
        "no actor",
        "a control character in the actor",
        "a long actor",
        "an unknown field",
    ],
)
def test_input_that_does_not_parse_is_422_and_changes_nothing(
    request_kwargs: dict[str, Any],
) -> None:
    gateway = escalated()
    before = gateway.store.snapshot()
    response = call(gateway.app, "POST", RESOLVE, **request_kwargs)
    assert response.status_code == 422
    assert response.json()["reason"] == "invalid_request"
    assert response.json()["message"]
    assert gateway.store.snapshot() == before


class RefusingAdmin(GatewayAdmin):
    """An admin path whose `resolve` raises what it is given."""

    def __init__(self, gateway: Gateway, error: Exception) -> None:
        super().__init__(gateway.scheduler, gateway.store, gateway.dispatcher)
        self._error = error

    def resolve(
        self,
        escalation_id: str,
        resolution: Resolution,
        effect_id: str | None = None,
        *,
        actor: str,
    ) -> ActionResult:
        raise self._error


@pytest.mark.parametrize(
    ("error", "status", "reason"),
    [
        (StepRefused("A-0001 is dispatched"), 409, "step_refused"),
        (ValueError("the actor contains a NUL character"), 422, "invalid_request"),
    ],
)
def test_any_other_refusal_of_the_admin_path_keeps_its_message(
    error: Exception, status: int, reason: str
) -> None:
    gateway = escalated()
    app = admin_app(RefusingAdmin(gateway, error), gateway.store, token=TOKEN)
    response = call(app, "POST", RESOLVE, json=COMMITTED)
    assert response.status_code == status
    assert response.json() == {"reason": reason, "message": str(error)}


# The event loop, the clock and the simulator ------------------------------------------------------


def test_an_attestation_over_http_ends_a_status_wait_at_once() -> None:
    """`get_action_status(wait_ms)` returns as soon as the action is final (0003): the
    request goes through `GatewayAdmin.resolve`, so the dispatcher's wake applies."""
    gateway = escalated()
    woke: list[tuple[int, ActionState]] = []

    async def waiter() -> None:
        await gateway.dispatcher.wait("A-0001", FINAL_STATES.__contains__, 60_000)
        woke.append((gateway.scheduler.now, gateway.state()))

    async def resolve() -> None:
        response = await _send(gateway.app, "POST", RESOLVE, json=COMMITTED)
        assert response.status_code == 200

    def human() -> None:
        gateway.scheduler.spawn("operator", resolve(), name="resolve")

    async def main() -> None:
        gateway.scheduler.spawn("gateway", waiter(), name="waiter")
        gateway.scheduler.call_at(5_000, human)
        await gateway.scheduler.run(deadline_ms=100_000)

    asyncio.run(main())
    assert woke == [(5_000, S.CONFIRMED)], "not at the end of the wait, 61,000"


def test_a_request_made_in_a_scheduler_event_finishes_before_the_next_event() -> None:
    """`settle` sees no timer and no real wait while the request runs, so it lets the
    request finish before the scheduler fires the event queued right after it."""
    gateway = escalated()
    seen: list[tuple[int, ActionState]] = []

    async def resolve() -> None:
        await _send(gateway.app, "POST", RESOLVE, json=COMMITTED)

    def human() -> None:
        gateway.scheduler.spawn("operator", resolve(), name="resolve")

    def look() -> None:
        seen.append((gateway.scheduler.now, gateway.state()))

    async def main() -> None:
        gateway.scheduler.call_at(2_000, human)
        gateway.scheduler.call_at(2_000, look)
        await gateway.scheduler.run(deadline_ms=100_000)

    asyncio.run(main())
    assert seen == [(2_000, S.CONFIRMED)]


def test_every_handler_is_a_coroutine_so_no_request_runs_in_a_worker_thread() -> None:
    """FastAPI runs a plain function in a worker thread, where `settle` cannot see it and
    the store would be used from two threads."""
    app = escalated().app
    assert app.routes and all(isinstance(route, APIRoute) for route in app.routes)
    for route in app.routes:
        assert isinstance(route, APIRoute)
        assert inspect.iscoroutinefunction(route.endpoint), route.path
    for handler in app.exception_handlers.values():
        assert inspect.iscoroutinefunction(handler), handler


def test_the_admin_api_never_contacts_the_simulator() -> None:
    gateway = escalated()
    calls = gateway.world.calls
    for method, path, body in EVERY_REQUEST:
        call(gateway.app, method, path, json=body)
    assert gateway.state() is S.CONFIRMED
    assert gateway.world.calls == calls


# Other errors -----------------------------------------------------------------------------------


def test_an_unknown_path_is_404_and_another_method_405() -> None:
    app = escalated().app
    missing = call(app, "GET", "/approvals")
    assert (missing.status_code, missing.json()["reason"]) == (404, "not_found")
    for method, path in (("DELETE", "/actions"), ("GET", RESOLVE), ("POST", "/grants")):
        response = call(app, method, path)
        assert (response.status_code, response.json()["reason"]) == (405, "method_not_allowed")
        assert response.headers["allow"]


def test_no_websocket_is_ever_accepted() -> None:
    """The app serves HTTP only: the router closes a WebSocket before accepting it, with
    the token or without it."""
    client = TestClient(escalated().app)
    for headers in ({}, AUTH):
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/actions", headers=dict(headers)),
        ):
            pass


class BrokenRecords:
    """A read path that fails."""

    def snapshot(self) -> Snapshot:
        raise RuntimeError("the database went away")


def test_an_internal_error_is_500_with_a_reason_and_no_details() -> None:
    gateway = escalated()
    app = admin_app(gateway.admin, BrokenRecords(), token=TOKEN)
    response = call(app, "GET", "/actions", raise_app_exceptions=False)
    assert response.status_code == 500
    assert response.json() == {
        "reason": "internal_error",
        "message": "the gateway could not serve this request",
    }
