"""The split topology's simulator as an HTTP service, end to end (0008, 0009).

Each test starts a split run and serves its simulator with uvicorn on 127.0.0.1, in a
thread with its own event loop, as a separate process would run it. The scripted agent
reaches it over the network in B0, with the agent's credential. Then the run ends after
its drain window and the evaluator scores it.

The scenario is dev-001 with shorter timings, so a run takes seconds of wall time: T is
800 ms and D 1,600 ms, so the drain window is 3,300 ms. Every wait has a deadline, and
the checks ask what happened, never how long it took.
"""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
import yaml
from psycopg.conninfo import make_conninfo

from fivefold.agents.remote import RemoteAgent, run_remote_agent
from fivefold.agents.worker import WorkerEnd
from fivefold.domain.contracts import Contract
from fivefold.harness.migrations import drop_database, registry_database
from fivefold.harness.registry import forget
from fivefold.harness.split.__main__ import main
from fivefold.harness.split.credentials import AGENT, GATEWAY, HARNESS
from fivefold.harness.split.lifecycle import start_run
from fivefold.harness.split.rundir import Config, SplitRun, read_run
from fivefold.harness.split.simulator import SimulatorService

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

    def scenario(self, **params: int) -> Path:
        """dev-001 with fast timings, written to a file."""
        data = yaml.safe_load(DEV_001.read_text(encoding="utf-8"))
        data["params"] = {**FAST, **params}
        data["faults"] = []
        path = self.root / "scenario.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def start(self, config: Config, plan: list[dict[str, Any]]) -> SplitRun:
        return start_run(
            admin_dsn=self.admin,
            scenario_path=self.scenario(),
            config=config,
            contract=Contract.C2,
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


def _order_plan(point: str, **extra: Any) -> list[dict[str, Any]]:
    match: dict[str, str] = {"api": ORDER}
    if point.endswith(".answer"):
        match["answer"] = "committed"
    return [{"at": {"point": point, "match": match, "occurrence": 1}, **extra}]


# B0 over HTTP ------------------------------------------------------------------------------


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


def test_the_command_line_serves_a_service(split: Split) -> None:
    run = split.start("B0", [])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [
        sys.executable,
        "-m",
        "fivefold.harness.split",
        "--run-dir",
        str(run.directory),
        "--dsn",
        split.admin,
        "serve",
        "simulator",
        "--port",
        str(port),
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        health = _wait_for(lambda: _health(f"http://127.0.0.1:{port}"), START_S)
    finally:
        process.terminate()
        output, _ = process.communicate(timeout=START_S)
    assert health == {"run_id": run.run_id, "status": "serving", "failures": 0}, output.decode()
    # uvicorn shuts down gracefully, then raises the signal it caught again
    # (`Server.capture_signals`), so the process ends by SIGTERM.
    assert process.returncode == -signal.SIGTERM, output.decode()
    assert "Application shutdown complete" in output.decode()


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
