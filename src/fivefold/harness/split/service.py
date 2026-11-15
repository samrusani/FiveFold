"""What the split topology's two services share: the run's records, clock and plan, and
some HTTP plumbing (0009).

A service opens its run from the run's directory: it connects to the run's database as
`ff_harness`, checks that the database holds the split run the directory names, with the
same scenario, and reads the run's seeded fault plan from `harness.run`. Its clock is a
`HostClock` with the run's origin, which the first service to start sets (migration
0013), so both services read the same virtual time on one host.

Checked in starlette 1.10.0: `Request.receive` is the ASGI receive callable, and after the
request's body is read, uvicorn 0.54.0's receive (`protocols/http/h11_impl.py`) returns
`http.disconnect` once the client closes the connection, and not before. Checked in
uvicorn 0.54.0 (`config.py`, `server.py`): `Config(app, host=, port=, lifespan=,
log_level=, timeout_graceful_shutdown=)` and `Server(config).run(sockets=)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
import uvicorn
from psycopg.rows import TupleRow
from starlette.requests import Request
from starlette.responses import JSONResponse

from fivefold.domain.scenario import Scenario, scenario_digest
from fivefold.faults.plan import PlannedFault, parse_plan
from fivefold.harness.clock import HostClock
from fivefold.harness.log import SharedPgHarnessLog
from fivefold.harness.scheduler import HostScheduler
from fivefold.harness.split.rundir import RunDirError, SplitRun

GRACEFUL_SHUTDOWN_S = 5
"""How long a service lets its open requests finish when it shuts down, in seconds."""


@dataclass
class OpenRun:
    """A service's hold on its run: the harness's connection, log, clock and plan."""

    run: SplitRun
    scenario: Scenario
    conn: psycopg.Connection[TupleRow]
    log: SharedPgHarnessLog
    scheduler: HostScheduler
    plan: tuple[PlannedFault, ...]

    def stop_records(self) -> None:
        """Write no more harness records: the run has stopped. Any later write fails."""
        self.conn.close()

    def close(self) -> None:
        """Stop the run's timers and tasks here, and close the harness's connection."""
        if not self.scheduler.stopped:
            self.scheduler.stop()
        self.conn.close()


def open_run(run: SplitRun, base_dsn: str) -> OpenRun:
    """Open `run` for a service. Raises `RunDirError` when the database holds another run."""
    scenario = run.scenario()
    conn = psycopg.connect(run.conninfo(base_dsn, "ff_harness"), autocommit=True)
    try:
        row = conn.execute(
            "SELECT run_id, scenario_digest, topology, params FROM harness.run"
        ).fetchone()
        if row is None or row[0] != run.run_id or row[2] != "split":
            raise RunDirError(f"database {run.database} holds no split run {run.run_id}")
        if row[1] != scenario_digest(scenario):
            raise RunDirError(f"{run.directory}'s scenario is not the one run {run.run_id} has")
        params: Mapping[str, Any] = row[3]
        plan = parse_plan(params.get("plan", []))
        clock = HostClock(clock_origin(conn))
        _ = clock.now
        scheduler = HostScheduler(clock)
        return OpenRun(run, scenario, conn, SharedPgHarnessLog(conn), scheduler, plan)
    except BaseException:
        conn.close()
        raise


def clock_origin(conn: psycopg.Connection[TupleRow]) -> int:
    """The run's clock origin; the first process to ask sets it to the host's clock now."""
    conn.execute(
        "INSERT INTO harness.clock_origin (origin_ms) VALUES (%s) ON CONFLICT DO NOTHING",
        (HostClock.host_ms(),),
    )
    row = conn.execute("SELECT origin_ms FROM harness.clock_origin").fetchone()
    if row is None:
        raise RuntimeError("harness.clock_origin is empty after it was set")
    return int(row[0])


async def until_gone(request: Request, stopping: asyncio.Event) -> None:
    """Hold `request` until its caller closes the connection, or the service stops.

    Read the request's body first: the receive callable gives the body before it reports
    a disconnect.
    """

    async def gone() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    waits = {asyncio.ensure_future(gone()), asyncio.ensure_future(stopping.wait())}
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()


def unavailable(detail: str) -> JSONResponse:
    """503: the service no longer takes this request, because the run has moved on."""
    return JSONResponse({"error": "unavailable", "detail": detail}, status_code=503)


def serve(app: Any, *, host: str, port: int) -> None:
    """Serve `app` with uvicorn until the process is told to stop."""
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        lifespan="on",
        log_level="info",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    uvicorn.Server(config).run()
