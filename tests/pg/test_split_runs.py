"""The split topology end to end: the simulator and the gateway as HTTP services (0009).

Each test starts a split run, serves the simulator, and in G modes the gateway, with
uvicorn on 127.0.0.1, each in a thread with its own event loop, as separate processes
would run. The scripted agent reaches them over the network: the gateway's MCP endpoint
over streamable HTTP in G2, the simulator's API in B0. Then the run ends after its drain
window and the evaluator scores it.

The scenario is dev-001 with shorter timings, so a run takes seconds of wall time: T is
800 ms and D 1,600 ms, so the drain window is 3,300 ms. Every wait has a deadline, and
the checks ask what happened, never how long it took.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import mcp_types as types
import psycopg
import pytest
import uvicorn
import yaml
from mcp.shared.exceptions import MCPError
from psycopg.conninfo import make_conninfo
from starlette.types import Receive, Scope, Send

from fivefold.agents.remote import HttpGatewayConnection, RemoteAgent, RunOver, run_remote_agent
from fivefold.agents.worker import WorkerEnd
from fivefold.domain.contracts import Contract
from fivefold.gateway.admin import GatewayAdmin
from fivefold.gateway.admin_api import TOKEN_VARIABLE
from fivefold.gateway.records import ActionResult
from fivefold.harness.clock import HostClock
from fivefold.harness.log import SharedPgHarnessLog
from fivefold.harness.migrations import (
    create_database,
    create_run_database,
    database_lock,
    drop_database,
    registry_database,
)
from fivefold.harness.registry import forget
from fivefold.harness.scheduler import HostScheduler
from fivefold.harness.split import __main__ as split_main
from fivefold.harness.split.__main__ import main
from fivefold.harness.split.credentials import AGENT, GATEWAY, HARNESS
from fivefold.harness.split.gateway import AdminFactory, GatewayService, admin_api
from fivefold.harness.split.lifecycle import default_database, end_run, score_run, start_run
from fivefold.harness.split.rundir import Config, RunDirError, SplitRun, read_run
from fivefold.harness.split.service import open_run
from fivefold.harness.split.simulator import SimulatorService
from fivefold.paths import DB_DIR

pytestmark = pytest.mark.pg

ROOT = Path(__file__).resolve().parents[2]
DEV_001 = ROOT / "scenarios" / "dev" / "dev-001-single-po.yaml"

FAST = {
    "max_hop_latency_ms": 50,
    "commit_deadline_ms": 1_600,
    "status_lag_ms": 0,
    "dispatch_timeout_ms": 800,
    "think_ms": 0,
    "tool_wait_ms": 1_000,
    "status_probe_interval_ms": 300,
    "approval_poll_interval_ms": 100,
}
"""dev-001's timings, shortened so a split run takes seconds of wall time."""

START_S = 30.0
"""How long a service may take to start, in wall-clock seconds."""

AGENT_S = 120.0
"""How long the scripted agent may take to finish, in wall-clock seconds."""

ORDER = "submit_purchase_order"


# Services in threads --------------------------------------------------------------------------


@dataclass
class Service:
    """A service served by uvicorn in a thread of this process, on its own event loop."""

    server: uvicorn.Server
    thread: threading.Thread
    url: str

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=START_S)
        assert not self.thread.is_alive(), f"the service at {self.url} did not stop"


def serve(app: Any) -> Service:
    """Serve `app` on a free port of 127.0.0.1; return once it accepts connections."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(app, lifespan="on", log_level="warning", timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + START_S
    while not server.started:
        assert thread.is_alive(), "the service failed to start"
        assert time.monotonic() < deadline, "the service did not start in time"
        time.sleep(0.01)
    return Service(server, thread, f"http://127.0.0.1:{sock.getsockname()[1]}")


# Runs ------------------------------------------------------------------------------------------


@dataclass
class Split:
    """Where a test's split runs go: its databases, under the test run's prefix."""

    admin: str
    template: str
    database: str
    root: Path

    def scenario(self, scenario_id: str | None = None, **params: int) -> Path:
        """dev-001 with fast timings, written to a file; under another id if given."""
        data = yaml.safe_load(DEV_001.read_text(encoding="utf-8"))
        data["params"] = {**FAST, **params}
        data["faults"] = []
        name = "scenario.yaml"
        if scenario_id is not None:
            data["id"] = scenario_id
            name = f"scenario-{scenario_id}.yaml"
        path = self.root / name
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def start(
        self, config: Config, plan: list[dict[str, Any]], contract: Contract = Contract.C2
    ) -> SplitRun:
        return start_run(
            admin_dsn=self.admin,
            scenario_path=self.scenario(),
            config=config,
            contract=contract,
            seed=7,
            run_dir=self.root / "run",
            plan=plan,
            model="scripted",
            database=self.database,
            template=self.template,
        )

    def connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        return psycopg.connect(make_conninfo(self.admin, dbname=self.database))


@pytest.fixture(scope="module")
def template(admin_dsn: str, db_prefix: str) -> Iterator[str]:
    """The migrated template database the module's runs are made from."""
    name = f"{db_prefix}_splittpl"
    yield name
    drop_database(admin_dsn, name)


@pytest.fixture
def split(admin_dsn: str, db_prefix: str, template: str, tmp_path: Path) -> Iterator[Split]:
    name = f"{db_prefix}_split"
    drop_database(admin_dsn, name)
    yield Split(admin_dsn, template, name, tmp_path)
    drop_database(admin_dsn, name)
    forget(admin_dsn, name)


def _registered(split: Split) -> list[tuple[Any, ...]]:
    """The run registry's row for the test's run database (0012)."""
    with psycopg.connect(make_conninfo(split.admin, dbname=registry_database())) as conn:
        return conn.execute(
            "SELECT run_id, scenario_id, config, contract, seed FROM registry.run"
            " WHERE database_name = %s",
            (split.database,),
        ).fetchall()


@dataclass
class Ran:
    """A whole split run: how the agent ended, and when the run stopped."""

    run: SplitRun
    end: tuple[WorkerEnd, str]
    stopped_vt: int
    health: list[dict[str, Any]]


def run_split(split: Split, config: Config, plan: list[dict[str, Any]]) -> Ran:
    """Start a run, serve it, run the scripted agent against it, and end it."""
    run = split.start(config, plan)
    services = [serve(SimulatorService(run, base_dsn=split.admin).app())]
    simulator = services[0].url
    gateway: str | None = None
    try:
        if run.has_gateway:
            app = GatewayService(run, base_dsn=split.admin, simulator_url=simulator).app()
            services.append(serve(app))
            gateway = services[1].url
        agent = RemoteAgent(
            scenario=run.scenario(),
            config=config,
            contract=run.contract,
            gateway_url=None if gateway is None else f"{gateway}/mcp",
            simulator_url=simulator,
            secret=run.secret(AGENT),
        )
        end = asyncio.run(asyncio.wait_for(run_remote_agent(agent), AGENT_S))
        stopped_vt = asyncio.run(
            end_run(run, admin_dsn=split.admin, simulator_url=simulator, gateway_url=gateway)
        )
        health = [httpx.get(f"{service.url}/health").json() for service in services]
    finally:
        for service in reversed(services):
            service.stop()
    return Ran(run, end, stopped_vt, health)


def _order_plan(point: str, **extra: Any) -> list[dict[str, Any]]:
    match: dict[str, str] = {"api": ORDER}
    if point.endswith(".answer"):
        match["answer"] = "committed"
    return [{"at": {"point": point, "match": match, "occurrence": 1}, **extra}]


def _check_records(split: Split, ran: Ran) -> None:
    """What every split run's records hold once it has ended."""
    assert ran.end[0] is WorkerEnd.FINISHED, ran.end
    assert all(health["failures"] == 0 for health in ran.health), ran.health
    assert all(health["status"] == "stopped" for health in ran.health), ran.health
    with split.connect() as conn:
        row = conn.execute("SELECT topology, model_adapter, model FROM harness.run").fetchone()
        stop = conn.execute("SELECT stopped_vt FROM harness.run_stop").fetchone()
        unfated = conn.execute(
            "SELECT count(*) FROM harness.message_log WHERE fate IS NULL"
        ).fetchone()
        # Recovery latency reads each delivery's wall-clock time (0014).
        unstamped = conn.execute(
            "SELECT count(*) FROM harness.message_log"
            " WHERE (fate = 'delivered') <> (delivered_wall_at IS NOT NULL)"
        ).fetchone()
        # The gateway's settle bounds start from each attempt's send time, the evaluator's
        # from the logged request's (0004, 0005): they must be one number.
        writes = conn.execute(
            "SELECT a.attempt_id, a.sent_vt, m.sent_vt FROM gw.attempt a"
            " JOIN harness.message_log m ON m.body->>'client_ref' = a.attempt_id"
            " WHERE m.hop = 'gw_sim' AND m.kind = 'request' AND m.copy_of IS NULL"
        ).fetchall()
    assert row == ("split", "external", "scripted")
    assert stop == (ran.stopped_vt,)
    assert unfated == (0,)
    assert unstamped == (0,)
    if ran.run.has_gateway:
        assert writes, "the gateway sent a write"
        assert all(attempt == logged for _, attempt, logged in writes), writes
    assert _registered(split) == [(ran.run.run_id, "dev-001-single-po", ran.run.config, "C2", 7)]


# G2 and B0 over HTTP ------------------------------------------------------------------------------


def test_g2_over_http_recovers_a_lost_reply_and_places_one_order(split: Split) -> None:
    ran = run_split(split, "G2", [{"fault": "F2", **_order_plan("net.gw_sim.answer")[0]}])
    _check_records(split, ran)
    evaluation = score_run(ran.run, admin_dsn=split.admin)
    assert str(evaluation.verdict) == "pass"
    with split.connect() as conn:
        effects = conn.execute("SELECT effect_id FROM sim.effect").fetchall()
        fault = conn.execute(
            "SELECT f.fault, f.point, f.occurrence, f.intent_id, m.api, m.answer_class, m.fate,"
            " m.drop_reason FROM harness.fault_log f"
            " JOIN harness.message_log m ON m.seq = f.target_seq"
        ).fetchall()
        report = conn.execute("SELECT accepted_by, valid FROM harness.final_report").fetchall()
    assert effects == [("PO-0001",)]
    assert fault == [
        ("F2", "net.gw_sim.answer", 1, "REQ-1", ORDER, "committed", "dropped", "fault")
    ]
    assert report == [("gateway", True)]


def test_b0_over_http_orders_twice_after_a_lost_reply(split: Split) -> None:
    plan = split.root / "plan.yaml"
    plan.write_text(
        yaml.safe_dump([{"fault": "F2", **_order_plan("net.agent_sim.answer")[0]}]),
        encoding="utf-8",
    )
    common = ["--run-dir", str(split.root / "run"), "--dsn", split.admin]
    assert (
        main(
            [
                *common,
                "start",
                "--scenario",
                str(split.scenario()),
                "--config",
                "B0",
                "--contract",
                "C2",
                "--plan",
                str(plan),
                "--model",
                "scripted",
                "--database",
                split.database,
                "--template",
                split.template,
            ]
        )
        == 0
    )
    run = read_run(split.root / "run")
    simulator = serve(SimulatorService(run, base_dsn=split.admin).app())
    try:
        agent = RemoteAgent(
            scenario=run.scenario(),
            config="B0",
            contract=run.contract,
            simulator_url=simulator.url,
            secret=run.secret(AGENT),
        )
        end = asyncio.run(asyncio.wait_for(run_remote_agent(agent), AGENT_S))
        assert main([*common, "end", "--simulator-url", simulator.url]) == 0
        health = httpx.get(f"{simulator.url}/health").json()
    finally:
        simulator.stop()
    out = split.root / "result.json"
    assert main([*common, "score", "--out", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    failing = [prop["property"] for prop in result["properties"] if prop["verdict"] == "fail"]
    assert (result["verdict"], failing) == ("fail", [1, 2, 3])
    assert end[0] is WorkerEnd.FINISHED
    assert health == {"run_id": run.run_id, "status": "stopped", "failures": 0}
    with split.connect() as conn:
        effects = conn.execute("SELECT effect_id FROM sim.effect ORDER BY commit_seq").fetchall()
        faults = conn.execute(
            "SELECT fault, point, occurrence, intent_id FROM harness.fault_log"
        ).fetchall()
        report = conn.execute("SELECT accepted_by, valid FROM harness.final_report").fetchall()
        # Recovery latency reads each delivery's wall-clock time (0014).
        unstamped = conn.execute(
            "SELECT count(*) FROM harness.message_log"
            " WHERE (fate = 'delivered') <> (delivered_wall_at IS NOT NULL)"
        ).fetchone()
    # The framework resent the order after the lost reply: a second order on the first
    # approval. Under a heavy load a resend can itself time out, and be resent again.
    assert effects[:2] == [("PO-0001",), ("PO-0002",)]
    assert faults == [("F2", "net.agent_sim.answer", 1, "REQ-1")]
    assert report == [("sink", True)]
    assert unstamped == (0,)
    assert _registered(split) == [(run.run_id, "dev-001-single-po", "B0", "C2", 7)]


# Faults on their counters ----------------------------------------------------------------------


def test_a_fault_fires_on_its_counter(split: Split) -> None:
    plan = [
        {
            "fault": "F12",
            "at": {"point": "net.gw_sim.request", "match": {"api": "get_quote"}, "occurrence": 2},
        }
    ]
    ran = run_split(split, "G2", plan)
    _check_records(split, ran)
    assert str(score_run(ran.run, admin_dsn=split.admin).verdict) == "pass"
    with split.connect() as conn:
        quotes = conn.execute(
            "SELECT seq, fate, drop_reason FROM harness.message_log"
            " WHERE hop = 'gw_sim' AND kind = 'request' AND api = 'get_quote' ORDER BY seq"
        ).fetchall()
        faults = conn.execute(
            "SELECT fault, occurrence, target_seq, intent_id FROM harness.fault_log"
        ).fetchall()
    assert len(quotes) >= 3
    assert [fate for _, fate, _ in quotes].count("dropped") == 1
    assert quotes[1][1:] == ("dropped", "fault")
    # A read names no intent, so the fault touched none (0014).
    assert faults == [("F12", 2, quotes[1][0], None)]


@pytest.mark.parametrize("hop", ["gw_sim", "agent_gw"])
def test_a_request_delivered_twice_is_one_order_in_g2(split: Split, hop: str) -> None:
    point = f"net.{hop}.request"
    ran = run_split(split, "G2", [{"fault": "F4", **_order_plan(point)[0]}])
    _check_records(split, ran)
    assert str(score_run(ran.run, admin_dsn=split.admin).verdict) == "pass"
    with split.connect() as conn:
        effects = conn.execute("SELECT effect_id FROM sim.effect").fetchall()
        copies = conn.execute(
            "SELECT c.hop, c.api, c.fate, a.fate, a.drop_reason FROM harness.message_log c"
            " LEFT JOIN harness.message_log a ON a.correlation = c.seq"
            " WHERE c.copy_of IS NOT NULL"
        ).fetchall()
        fault = conn.execute(
            "SELECT fault, point, params ? 'copy_seq', intent_id FROM harness.fault_log"
        ).fetchall()
    assert effects == [("PO-0001",)]
    assert copies == [(hop, ORDER, "delivered", "dropped", "answer_to_copy")]
    assert fault == [("F4", point, True, "REQ-1")]


# A human's attestation over the admin API ---------------------------------------------------------

ADMIN_TOKEN = "admin-api-token-for-the-tests"


@dataclass
class Attested:
    """A run whose escalation a human resolved over the admin API, attesting a commit."""

    run: SplitRun
    end: tuple[WorkerEnd, str]
    escalation_id: str
    first: httpx.Response
    again: httpx.Response
    """The same attestation, sent again."""


def _attested_run(
    split: Split, monkeypatch: pytest.MonkeyPatch, effect_of: Callable[[str], str]
) -> Attested:
    """Demo 3 in G2, over HTTP: at C0 the order's answer is lost and the gateway escalates.
    A human attests through the admin API that the order committed, as the effect that
    `effect_of` names for the approval the escalation cites, and the run ends."""
    monkeypatch.setenv(TOKEN_VARIABLE, ADMIN_TOKEN)
    factory: AdminFactory | None = admin_api()
    assert factory is not None
    plan = [{"fault": "F2", **_order_plan("net.gw_sim.answer")[0]}]
    run = split.start("G2", plan, contract=Contract.C0)
    simulator = serve(SimulatorService(run, base_dsn=split.admin).app())
    gateway = serve(
        GatewayService(run, base_dsn=split.admin, simulator_url=simulator.url, admin=factory).app()
    )

    async def human() -> tuple[str, httpx.Response, httpx.Response]:
        headers = {"authorization": f"Bearer {ADMIN_TOKEN}"}
        async with httpx.AsyncClient(base_url=f"{gateway.url}/admin", headers=headers) as client:
            deadline = time.monotonic() + AGENT_S
            while not (listed := (await client.get("/escalations")).json()["escalations"]):
                assert time.monotonic() < deadline, "no escalation opened"
                await asyncio.sleep(0.05)
            (escalation,) = listed
            body = {
                "resolution": "committed",
                "effect_id": effect_of(escalation["approval_id"]),
                "actor": "ops-1",
            }
            path = f"/escalations/{escalation['escalation_id']}/resolve"
            first = await client.post(path, json=body)
            again = await client.post(path, json=body)
        return str(escalation["escalation_id"]), first, again

    async def both() -> tuple[tuple[WorkerEnd, str], tuple[str, httpx.Response, httpx.Response]]:
        agent = RemoteAgent(
            scenario=run.scenario(),
            config="G2",
            contract=run.contract,
            gateway_url=f"{gateway.url}/mcp",
        )
        working = asyncio.ensure_future(run_remote_agent(agent))
        attested = await human()
        return await working, attested

    try:
        end, (escalation_id, first, again) = asyncio.run(asyncio.wait_for(both(), AGENT_S))
        asyncio.run(
            end_run(
                run, admin_dsn=split.admin, simulator_url=simulator.url, gateway_url=gateway.url
            )
        )
    finally:
        gateway.stop()
        simulator.stop()
    return Attested(run, end, escalation_id, first, again)


def _effect_citing(split: Split, approval_id: str) -> str:
    """A human's check of the world, out of band (0008): the effect citing the approval."""
    with split.connect() as conn:
        row = conn.execute(
            "SELECT effect_id FROM sim.effect WHERE approval_id = %s", (approval_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_a_humans_attestation_over_http_is_evidence(
    split: Split, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Demo 3 in G2: a human checks the ledger and attests the order through the admin
    API, and the agent then reports it done. The evaluator counts an attestation only from
    the harness's actor log (0004), so the gateway logs it there, as the scripted resolver
    would."""
    attested = _attested_run(split, monkeypatch, partial(_effect_citing, split))
    run, end, escalation_id = attested.run, attested.end, attested.escalation_id
    first, again = attested.first, attested.again
    assert end[0] is WorkerEnd.FINISHED, end
    assert first.status_code == 200, first.text
    assert (first.json()["state"], first.json()["effect_id"]) == ("confirmed", "PO-0001")
    assert again.status_code == 409, again.text
    evaluation = score_run(run, admin_dsn=split.admin)
    verdicts = {result.property: str(result.verdict) for result in evaluation.properties}
    assert str(evaluation.verdict) == "pass", verdicts
    assert evaluation.metrics["human_interventions"] == 1
    with split.connect() as conn:
        attestations = conn.execute(
            "SELECT actor, action, subject, details FROM harness.actor_log"
        ).fetchall()
        report = conn.execute("SELECT report FROM harness.final_report").fetchall()
    details = {
        "resolution": "committed",
        "intent_id": "REQ-1",
        "action_id": "A-0001",
        "effect_id": "PO-0001",
    }
    assert attestations == [("resolver", "attest", escalation_id, details)]
    ((claims,),) = report
    assert [(c["status"], c["effect_ids"]) for c in claims["claims"]] == [("done", ["PO-0001"])]


def test_score_refuses_an_attestation_of_an_effect_the_ledger_lacks_in_one_line(
    split: Split, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The admin path takes the human's word: the real world offers the gateway no ground
    truth to check it against. The evaluator refuses records whose attestation names an
    effect the ledger lacks (`RecordsError`), and `score` says so as it says every other
    refusal: in one line, with exit status 1."""
    attested = _attested_run(split, monkeypatch, lambda _: "PO-0042")
    assert attested.end[0] is WorkerEnd.FINISHED, attested.end
    assert attested.first.status_code == 200, attested.first.text
    assert attested.first.json()["effect_id"] == "PO-0042"
    capsys.readouterr()
    common = ["--run-dir", str(attested.run.directory), "--dsn", split.admin]
    assert main([*common, "score"]) == 1
    error = capsys.readouterr().err
    assert error.count("\n") == 1, error
    assert "the evaluator cannot score the run" in error
    assert "names effect PO-0042, which the ledger lacks" in error


@dataclass
class Resolved:
    """An escalation of demo 3 that a human resolved while the gateway stopped: the admin
    API's answer, and what the gateway's and the harness's records hold afterwards."""

    answer: httpx.Response
    resolved_vt: int | None
    """The gateway's record of the escalation's resolution."""
    attestations: int
    """The harness's actor-log rows, which the evaluator counts (0004)."""
    records_closed: bool
    """Whether the gateway's connection for harness records closed."""


Human = Callable[
    [GatewayService, httpx.AsyncClient, str, dict[str, Any]], Awaitable[httpx.Response]
]
"""What the human does at the escalation: given the gateway, an admin API client, the
gateway's address and the escalation, it resolves it and returns the admin API's answer."""


def _resolve_while_stopping(
    split: Split, monkeypatch: pytest.MonkeyPatch, human: Human
) -> Resolved:
    """Demo 3 in G2: at C0 the order's answer is lost and the gateway escalates; `human`
    resolves the escalation as the gateway stops."""
    monkeypatch.setenv(TOKEN_VARIABLE, ADMIN_TOKEN)
    factory = admin_api()
    plan = [{"fault": "F2", **_order_plan("net.gw_sim.answer")[0]}]
    run = split.start("G2", plan, contract=Contract.C0)
    simulator = serve(SimulatorService(run, base_dsn=split.admin).app())
    service = GatewayService(run, base_dsn=split.admin, simulator_url=simulator.url, admin=factory)
    gateway = serve(service.app())

    async def acts() -> httpx.Response:
        agent = RemoteAgent(
            scenario=run.scenario(),
            config="G2",
            contract=run.contract,
            gateway_url=f"{gateway.url}/mcp",
        )
        working = asyncio.ensure_future(run_remote_agent(agent))
        headers = {"authorization": f"Bearer {ADMIN_TOKEN}"}
        async with httpx.AsyncClient(base_url=f"{gateway.url}/admin", headers=headers) as client:
            deadline = time.monotonic() + AGENT_S
            while not (listed := (await client.get("/escalations")).json()["escalations"]):
                assert time.monotonic() < deadline, "no escalation opened"
                await asyncio.sleep(0.05)
            (escalation,) = listed
            answer = await human(service, client, gateway.url, escalation)
        # The gateway has stopped. A status call the agent still waits on would end only
        # at the agent's own timeout, so the agent is cancelled.
        working.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await working
        return answer

    try:
        answer = asyncio.run(asyncio.wait_for(acts(), AGENT_S))
        records_closed = service.serving.opened.conn.closed
    finally:
        gateway.stop()
        simulator.stop()
    with split.connect() as conn:
        (resolved_vt,) = conn.execute("SELECT resolved_vt FROM gw.escalation").fetchone() or (None,)
        (attestations,) = conn.execute("SELECT count(*) FROM harness.actor_log").fetchone() or (0,)
    return Resolved(answer, resolved_vt, attestations, records_closed)


def _attestation(split: Split, escalation: dict[str, Any]) -> dict[str, str]:
    return {
        "resolution": "committed",
        "effect_id": _effect_citing(split, escalation["approval_id"]),
        "actor": "ops-1",
    }


def test_no_attestation_commits_once_the_gateway_has_begun_to_stop(
    split: Split, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolution whose body arrives once stop has begun committed in the gateway's
    records; then its actor-log row failed, since stop had closed the harness's
    connection, so the admin API answered 500 and the evaluator never saw the
    attestation. Now no resolution commits once stop has begun: 503, and nothing
    recorded."""

    async def human(
        service: GatewayService, client: httpx.AsyncClient, url: str, escalation: dict[str, Any]
    ) -> httpx.Response:
        admin = service.serving.admin
        assert admin is not None
        inside = threading.Event()

        async def holding(scope: Scope, receive: Receive, send: Send) -> None:
            inside.set()  # past the gateway's own check that the run has not stopped
            await admin(scope, receive, send)

        service.serving.admin = holding
        released = asyncio.Event()
        body = json.dumps(_attestation(split, escalation)).encode()

        async def sent_late() -> AsyncIterator[bytes]:
            await released.wait()
            yield body

        path = f"/escalations/{escalation['escalation_id']}/resolve"
        headers = {"content-type": "application/json"}
        resolving = asyncio.ensure_future(client.post(path, content=sent_late(), headers=headers))
        assert await asyncio.to_thread(inside.wait, START_S), "the request never reached the API"
        harness = {"authorization": f"Bearer {service.run.secret(HARNESS)}"}
        async with httpx.AsyncClient() as control:
            stopped = await control.post(f"{url}/harness/stop", headers=harness)
        assert stopped.json() == {"status": "stopped"}
        released.set()
        return await resolving

    resolved = _resolve_while_stopping(split, monkeypatch, human)
    assert resolved.answer.status_code == 503, resolved.answer.text
    assert resolved.answer.json()["reason"] == "unavailable"
    assert (resolved.resolved_vt, resolved.attestations) == (None, 0)
    assert resolved.records_closed


def test_stop_lets_a_resolution_log_its_attestation_before_closing_the_records(
    split: Split, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop closes the harness's records only once every resolution in flight has logged
    its attestation. The test begins stop inside the resolution, between its commit in the
    gateway's records and its actor-log row."""

    async def human(
        service: GatewayService, client: httpx.AsyncClient, url: str, escalation: dict[str, Any]
    ) -> httpx.Response:
        commit = GatewayAdmin.resolve

        def commit_then_stop(admin: GatewayAdmin, *args: Any, **kwargs: Any) -> ActionResult:
            result = commit(admin, *args, **kwargs)
            service.stop()
            return result

        monkeypatch.setattr(GatewayAdmin, "resolve", commit_then_stop)
        path = f"/escalations/{escalation['escalation_id']}/resolve"
        return await client.post(path, json=_attestation(split, escalation))

    resolved = _resolve_while_stopping(split, monkeypatch, human)
    assert resolved.answer.status_code == 200, resolved.answer.text
    assert resolved.resolved_vt is not None
    assert resolved.attestations == 1
    assert resolved.records_closed


# Trust boundaries and the run's end --------------------------------------------------------------


def test_the_simulator_refuses_the_agents_identity_in_g_modes(split: Split) -> None:
    run = split.start("G2", [])
    simulator = serve(SimulatorService(run, base_dsn=split.admin).app())
    order = {
        "intent_id": "REQ-1",
        "approval_id": "APR-0001",
        "basis": {"quote/S-1/W-100": 1, "budget/CC-OPS": 1},
        "supplier_id": "S-1",
        "sku": "W-100",
        "quantity": 100,
        "unit_price_minor": 540,
        "currency": "USD",
        "cost_center": "CC-OPS",
    }
    quote = {"supplier_id": "S-1", "sku": "W-100"}

    def post(call: str, body: dict[str, Any], holder: str | None) -> httpx.Response:
        headers = {} if holder is None else {"authorization": f"Bearer {run.secret(holder)}"}
        return httpx.post(f"{simulator.url}/api/{call}", json=body, headers=headers)

    try:
        written = post(ORDER, order, AGENT)
        read = post("get_quote", quote, AGENT)
        anonymous = post(ORDER, order, None)
        harness = post(ORDER, order, HARNESS)
        forged = httpx.post(
            f"{simulator.url}/api/{ORDER}", json=order, headers={"authorization": "Bearer forged"}
        )
        gateway = post("get_quote", quote, GATEWAY)
        claims = [{"intent_id": "REQ-1", "status": "done", "effect_ids": ["PO-0001"]}]
        reported = post("submit_final_report", {"claims": claims}, AGENT)
    finally:
        simulator.stop()
    assert written.status_code == 403
    assert (written.json()["answer"], written.json()["reason"]) == ("rejected", "forbidden")
    assert read.status_code == 403
    assert (read.json()["answer"], read.json()["reason"]) == ("refused", "forbidden")
    for refused in (anonymous, harness, forged):
        assert refused.status_code == 401
        assert (refused.json()["answer"], refused.json()["reason"]) == ("rejected", "forbidden")
    assert gateway.status_code == 200
    assert gateway.json()["answer"] == "fact"
    # In G modes reports go through the gateway: the agent cannot report to the sink.
    assert reported.status_code == 403
    assert (reported.json()["result"], reported.json()["reason"]) == ("refused", "forbidden")
    with split.connect() as conn:
        reports = conn.execute("SELECT count(*) FROM harness.final_report").fetchone()
        effects = conn.execute("SELECT count(*) FROM sim.effect").fetchone()
        writes = conn.execute("SELECT count(*) FROM sim.write").fetchone()
        reads = conn.execute("SELECT caller FROM sim.read_log").fetchall()
        hops = conn.execute(
            "SELECT DISTINCT hop, sender FROM harness.message_log WHERE kind = 'request'"
        ).fetchall()
    assert reports == (0,)
    assert effects == (0,)
    assert writes == (0,)
    assert reads == [(GATEWAY,)]
    assert sorted(hops) == [("agent_sim", AGENT), ("gw_sim", GATEWAY)]


def test_once_the_agents_phase_is_over_the_scripted_agent_is_told_so(split: Split) -> None:
    """The gateway refuses an agent's MCP request once the agents' phase is over. It said
    so with -32000, which mcp 2.3.0 reads as a closed connection, so the scripted agent
    reconnected and its framework retried. It now gets a code the SDK does not use, and
    the agent's call raises `RunOver`, which ends the worker."""
    run = split.start("G2", [])
    simulator = serve(SimulatorService(run, base_dsn=split.admin).app())
    gateway = serve(GatewayService(run, base_dsn=split.admin, simulator_url=simulator.url).app())

    async def told() -> Exception:
        scheduler = HostScheduler(HostClock(HostClock.host_ms()))
        connection = HttpGatewayConnection(scheduler, f"{gateway.url}/mcp", timeout_ms=5_000)
        headers = {"authorization": f"Bearer {run.secret(HARNESS)}"}
        try:
            listed = await connection.list_tools()
            async with httpx.AsyncClient() as client:
                drained = await client.post(f"{gateway.url}/harness/drain", headers=headers)
            assert drained.json() == {"status": "draining"}
            try:
                await connection.call_tool(listed.tools[0].name, {}, 5_000)
            except Exception as error:
                return error
            raise AssertionError("the gateway served the agent after the agents' phase")
        finally:
            connection.close()

    try:
        error = asyncio.run(asyncio.wait_for(told(), AGENT_S))
    finally:
        gateway.stop()
        simulator.stop()
    assert isinstance(error, RunOver), repr(error)
    assert "the run's agents' phase is over" in str(error)


def test_the_simulator_refuses_a_body_holding_a_nul_before_logging_it(split: Split) -> None:
    """PostgreSQL's JSONB cannot hold \\u0000. A B0 report holding one failed in the log,
    and the simulator answered 500 before the sink could record the report and refuse it.
    The simulator now refuses such a body as it refuses a malformed one: 422, not logged."""
    run = split.start("B0", [])
    simulator = serve(SimulatorService(run, base_dsn=split.admin).app())
    headers = {"authorization": f"Bearer {run.secret(AGENT)}"}
    claims = [{"intent_id": "REQ-1", "status": "done", "effect_ids": ["PO-0001\u0000"]}]
    try:
        report = httpx.post(
            f"{simulator.url}/api/submit_final_report", json={"claims": claims}, headers=headers
        )
        quote = httpx.post(
            f"{simulator.url}/api/get_quote",
            json={"supplier_id": "S-1", "sku": "W-100\u0000"},
            headers=headers,
        )
        health = httpx.get(f"{simulator.url}/health").json()
    finally:
        simulator.stop()
    for refused in (report, quote):
        assert refused.status_code == 422, refused.text
        assert "NUL" in refused.json()["detail"]
    assert health == {"run_id": run.run_id, "status": "serving", "failures": 0}
    with split.connect() as conn:
        messages = conn.execute("SELECT count(*) FROM harness.message_log").fetchone()
        reports = conn.execute("SELECT count(*) FROM harness.final_report").fetchone()
    assert (messages, reports) == ((0,), (0,))


def test_the_gateway_refuses_an_mcp_message_holding_a_nul_before_logging_it(
    split: Split,
) -> None:
    """The gateway logs each MCP message the agent sends before it serves it, so one
    holding \\u0000 failed in the log, and the agent got the database's error message,
    with code 0. The gateway now refuses it with invalid params, before logging it."""
    run = split.start("G2", [])
    simulator = serve(SimulatorService(run, base_dsn=split.admin).app())
    gateway = serve(GatewayService(run, base_dsn=split.admin, simulator_url=simulator.url).app())

    async def refused() -> Exception:
        scheduler = HostScheduler(HostClock(HostClock.host_ms()))
        connection = HttpGatewayConnection(scheduler, f"{gateway.url}/mcp", timeout_ms=5_000)
        try:
            await connection.list_tools()
            arguments = {"supplier_id": "S-1", "sku": "W-100\u0000"}
            try:
                await connection.call_tool("get_quote", arguments, 5_000)
            except Exception as error:
                return error
            raise AssertionError("the gateway served a message holding a NUL")
        finally:
            connection.close()

    try:
        error = asyncio.run(asyncio.wait_for(refused(), AGENT_S))
        health = httpx.get(f"{gateway.url}/health").json()
    finally:
        gateway.stop()
        simulator.stop()
    assert isinstance(error, MCPError), repr(error)
    assert error.code == types.INVALID_PARAMS
    assert "NUL" in error.message
    assert health["failures"] == 0
    with split.connect() as conn:
        quotes = conn.execute(
            "SELECT count(*) FROM harness.message_log WHERE api = 'get_quote'"
        ).fetchone()
    assert quotes == (0,)


def _start(
    split: Split,
    run_dir: Path,
    *,
    replace: bool = False,
    admin: str | None = None,
    scenario_id: str | None = None,
) -> SplitRun:
    return start_run(
        admin_dsn=split.admin if admin is None else admin,
        scenario_path=split.scenario(scenario_id),
        config="B0",
        contract=Contract.C2,
        seed=7,
        run_dir=run_dir,
        plan=[],
        model="scripted",
        database=split.database,
        template=split.template,
        replace=replace,
    )


def _database_oid(split: Split) -> int | None:
    with psycopg.connect(split.admin) as conn:
        row = conn.execute(
            "SELECT oid FROM pg_database WHERE datname = %s", (split.database,)
        ).fetchone()
    return None if row is None else int(row[0])


def test_start_refuses_a_database_of_the_same_name_unless_it_may_replace_it(
    split: Split,
) -> None:
    """A database that already has the run's name is left as it was, unless the person
    asks to replace it and it is this run's. A database that is no run database stays,
    and so does the template; a copy of the template that holds no run yet, as a start
    that failed before recording its run leaves it, is replaced."""
    other = create_database(split.admin, split.database)
    with pytest.raises(RunDirError, match="already exists: replace it, or name another"):
        _start(split, split.root / "refused")
    with pytest.raises(RunDirError, match="is not a run database"):
        _start(split, split.root / "refused", replace=True)
    assert _database_oid(split) == other.oid
    drop_database(split.admin, split.database)
    create_run_database(split.admin, split.database, DB_DIR / "migrations", template=split.template)
    unrecorded = _database_oid(split)
    _start(split, split.root / "replaced", replace=True)
    replaced = _database_oid(split)
    assert replaced is not None and replaced != unrecorded
    template = Split(split.admin, split.template, split.template, split.root)
    with pytest.raises(RunDirError, match="is the template"):
        _start(template, split.root / "template", replace=True)


def test_replace_drops_only_a_database_that_holds_this_run(
    split: Split, capsys: pytest.CaptureFixture[str]
) -> None:
    """Scenario ids `sample-a` and `sample_a` give one default database name, and a
    person can name one database for two runs. `start` with replace dropped whatever
    database had the name, another run's too. It now drops one only when it holds this
    run, and refuses in one line otherwise, keeping the database."""
    assert default_database("sample-a.B0.C2.7") == default_database("sample_a.B0.C2.7")
    first = _start(split, split.root / "first", scenario_id="sample-a")
    kept = _database_oid(split)
    capsys.readouterr()
    argv = ["--run-dir", str(split.root / "second"), "--dsn", split.admin, "start"]
    argv += ["--scenario", str(split.scenario("sample_a")), "--config", "B0"]
    argv += ["--contract", "C2", "--plan", "none", "--database", split.database]
    argv += ["--template", split.template, "--replace"]
    assert main(argv) == 1
    error = capsys.readouterr().err
    assert error.count("\n") == 1, error
    assert f"holds run {first.run_id}" in error
    assert _database_oid(split) == kept
    with split.connect() as conn:
        assert conn.execute("SELECT run_id FROM harness.run").fetchall() == [(first.run_id,)]
    assert not (split.root / "second" / "run.json").exists()
    # The run that the database holds may replace it.
    _start(split, split.root / "first", scenario_id="sample-a", replace=True)
    assert _database_oid(split) not in (None, kept)


def test_start_copies_the_run_database_under_the_database_lock(
    split: Split, db_prefix: str, await_database_lock: Callable[[str], None]
) -> None:
    """The run database is copied from the template under the lock that every create and
    drop takes (`fivefold.harness.migrations.database_lock`), so a study's drop and its
    registry cleanup never interleave with it."""
    _start(split, split.root / "first")  # makes and migrates the template
    drop_database(split.admin, split.database)
    waiter = f"{db_prefix}_split_start"
    admin = make_conninfo(split.admin, application_name=waiter)
    started: list[SplitRun] = []
    thread = threading.Thread(
        target=lambda: started.append(_start(split, split.root / "second", admin=admin))
    )
    with database_lock(split.admin):
        thread.start()
        await_database_lock(waiter)
        assert _database_oid(split) is None, "the copy waits for the lock"
    thread.join(timeout=60)
    assert len(started) == 1 and _database_oid(split) is not None


def test_the_agents_phase_ends_then_the_run_stops(split: Split) -> None:
    run = split.start("B0", [])
    simulator = serve(SimulatorService(run, base_dsn=split.admin).app())
    secret = run.secret(HARNESS)
    quote = {"supplier_id": "S-1", "sku": "W-100"}

    def call() -> httpx.Response:
        headers = {"authorization": f"Bearer {run.secret(AGENT)}"}
        return httpx.post(f"{simulator.url}/api/get_quote", json=quote, headers=headers)

    def control(action: str, token: str) -> httpx.Response:
        headers = {"authorization": f"Bearer {token}"}
        return httpx.post(f"{simulator.url}/harness/{action}", headers=headers)

    try:
        before = call()
        # A status wait is at most the run's max_wait_ms (0005): the default, 60,000.
        too_long = httpx.post(
            f"{simulator.url}/api/get_approval_status",
            json={"approval_id": "APR-0001", "wait_ms": 60_001},
            headers={"authorization": f"Bearer {run.secret(AGENT)}"},
        )
        refused_control = control("stop", run.secret(AGENT))
        drained = control("drain", secret)
        during = call()
        stopped = control("stop", secret)
        again = control("stop", secret)
        after = call()
    finally:
        simulator.stop()
    assert before.status_code == 200
    assert too_long.status_code == 422, too_long.text
    assert "max_wait_ms is 60000" in too_long.json()["detail"]
    assert refused_control.status_code == 401
    assert drained.json() == {"status": "draining"}
    assert during.status_code == 503
    assert stopped.json() == again.json()
    assert after.status_code == 503
    with split.connect() as conn:
        stop = conn.execute("SELECT stopped_vt FROM harness.run_stop").fetchone()
    assert stop == (stopped.json()["stopped_vt"],)


@pytest.mark.parametrize("how", ["ended", "stopped"])
def test_a_service_refuses_a_run_that_has_ended_in_one_line(
    split: Split,
    how: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A service keeps its fault counters, the agents' phase and the approver's timers in
    memory, so one started again would fire `occurrence: 1` faults again. A run has ended
    once `end` wrote `ended`, or once the simulator recorded its stop, which `end` does
    first; `serve` then refuses in one line, and a service's start refuses too."""
    run = split.start("G2", [])
    if how == "ended":
        run.mark_ended(1_234)
    else:
        with psycopg.connect(run.conninfo(split.admin, "ff_harness"), autocommit=True) as conn:
            SharedPgHarnessLog(conn).run_stopped(1_234)
    with pytest.raises(RunDirError, match="has ended"):
        open_run(run, split.admin)

    def served(*_: object, **__: object) -> None:
        raise AssertionError("a run that has ended was served")

    monkeypatch.setattr(split_main, "serve", served)
    common = ["--run-dir", str(run.directory), "--dsn", split.admin, "serve"]
    for service in ("simulator", "gateway"):
        capsys.readouterr()
        assert main([*common, service]) == 1
        error = capsys.readouterr().err
        assert error.count("\n") == 1, error
        assert f"run {run.run_id} has ended" in error


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def test_the_command_line_serves_both_services(split: Split) -> None:
    run = split.start("G2", [])
    simulator_port, gateway_port = _free_port(), _free_port()
    common = [
        sys.executable,
        "-m",
        "fivefold.harness.split",
        "--run-dir",
        str(run.directory),
        "--dsn",
        split.admin,
        "serve",
    ]
    commands = [
        [*common, "simulator", "--port", str(simulator_port)],
        [
            *common,
            "gateway",
            "--port",
            str(gateway_port),
            "--simulator-url",
            f"http://127.0.0.1:{simulator_port}",
        ],
    ]
    processes: list[subprocess.Popen[bytes]] = []
    healths: list[dict[str, Any]] = []
    try:
        for command, port in zip(commands, (simulator_port, gateway_port), strict=True):
            processes.append(
                subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            )
            healths.append(_wait_for(partial(_health, f"http://127.0.0.1:{port}"), START_S))
    finally:
        for process in reversed(processes):
            process.terminate()
        outputs = [process.communicate(timeout=START_S)[0].decode() for process in processes]
    expected = {"run_id": run.run_id, "status": "serving", "failures": 0}
    assert healths == [expected, expected], outputs
    for process, output in zip(processes, outputs, strict=True):
        # uvicorn shuts down gracefully, then raises the signal it caught again
        # (`Server.capture_signals`), so the process ends by SIGTERM.
        assert process.returncode == -signal.SIGTERM, output
        assert "Application shutdown complete" in output, output


def _health(url: str) -> dict[str, Any] | None:
    try:
        answer: dict[str, Any] = httpx.get(f"{url}/health").json()
    except httpx.HTTPError:
        return None
    return answer


def _wait_for[T](check: Callable[[], T | None], limit_s: float) -> T:
    """Ask `check` until it gives something, for at most `limit_s` wall-clock seconds."""
    deadline = time.monotonic() + limit_s
    while True:
        found = check()
        if found is not None:
            return found
        assert time.monotonic() < deadline, "the condition did not hold in time"
        time.sleep(0.05)


def test_stopping_answers_held_tool_calls(split: Split, monkeypatch: pytest.MonkeyPatch) -> None:
    """Held calls receive the existing stopped error without waiting for their timeout."""
    run = split.start("G2", [])
    simulator = serve(SimulatorService(run, base_dsn=split.admin).app())
    service = GatewayService(run, base_dsn=split.admin, simulator_url=simulator.url)
    gateway = serve(service.app())
    entered = threading.Event()
    count = 0

    async def hold(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        await asyncio.Event().wait()
        return {}

    monkeypatch.setattr(service.serving.tools, "call", hold)

    async def exercise() -> None:
        scheduler = HostScheduler(HostClock(HostClock.host_ms()))
        connection = HttpGatewayConnection(scheduler, f"{gateway.url}/mcp", timeout_ms=10_000)
        await connection.list_tools()
        calls = [
            asyncio.create_task(
                connection.call_tool(
                    "get_action_status", {"action_id": "A-0001", "wait_ms": 60_000}, 65_000
                )
            )
            for _ in range(2)
        ]
        try:
            assert await asyncio.to_thread(entered.wait, START_S), "calls were not held"
            headers = {"authorization": f"Bearer {run.secret(HARNESS)}"}
            async with httpx.AsyncClient() as client:
                stopped = await client.post(f"{gateway.url}/harness/stop", headers=headers)
            assert stopped.json() == {"status": "stopped"}
            for call in calls:
                with pytest.raises(RunOver) as error:
                    await asyncio.wait_for(call, timeout=5)
                assert isinstance(error.value.__cause__, MCPError)
                assert error.value.__cause__.code == -32019
        finally:
            for call in calls:
                call.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            connection.close()

    try:
        asyncio.run(asyncio.wait_for(exercise(), AGENT_S))
    finally:
        gateway.stop()
        simulator.stop()
