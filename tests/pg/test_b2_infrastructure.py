"""Exercise B2's per-invocation worker and PostgreSQL boundaries (0020)."""

from __future__ import annotations

import asyncio
import select
import socket
from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url

from fivefold.agents.worker import WorkerEnd
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness import dbos_worker
from fivefold.harness.baseline_worker import Delivery, Reply, Request
from fivefold.harness.cells import cell_plan, load_expected
from fivefold.harness.dbos_worker import DBOSInfrastructureError, DBOSWorkers
from fivefold.harness.demo import DEV_001
from fivefold.harness.migrations import (
    CreatedDatabase,
    create_database,
    drop_database,
    drop_database_locked,
)
from fivefold.harness.models import Scripted
from fivefold.harness.run import InprocRun, RunSpec

pytestmark = pytest.mark.pg


def scenario_spec(*, plan: list[dict[str, Any]] | None = None) -> RunSpec:
    return RunSpec(load_scenario_file(DEV_001), "B2", Contract.C2, 7, plan=plan or [])


def test_worker_environment_and_recovery(
    run_database: Callable[[str], str], admin_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = next(cell for cell in load_expected().cells if cell.name == "F11-after_tool_call-C2-B2")
    spec = scenario_spec(plan=cell_plan(cell))
    launched: list[tuple[str, bool]] = []
    stored: set[tuple[str, str]] = set()
    launch = DBOSWorkers._launch
    following = DBOSWorkers._next
    spawn = asyncio.create_subprocess_exec

    async def observe_launch(workers: DBOSWorkers, worker: str, *, resume: bool) -> None:
        await launch(workers, worker, resume=resume)
        launched.append((worker, resume))

    async def observe_handoff(
        workers: DBOSWorkers, worker: str, reply: Reply | None, *, start: bool
    ) -> Delivery:
        recovering = worker in workers.recovering
        delivery = await following(workers, worker, reply, start=start)
        if (start or recovering) and isinstance(delivery, Request):
            database = make_url(workers.system_url).database
            with psycopg.connect(make_conninfo(admin_dsn, dbname=database)) as conn:
                status = sql.Identifier("dbos_" + worker.replace("-", "_"), "workflow_status")
                rows = conn.execute(
                    sql.SQL("SELECT executor_id, application_version FROM {}").format(status)
                ).fetchall()
            assert rows == [(worker, "fivefold-b2-v1")]
            stored.update(rows)
        return delivery

    async def observe_environment(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        if args[-1] == "fivefold.harness.dbos_process":
            assert kwargs["env"] == {}
        return await spawn(*args, **kwargs)

    with socket.socket() as sentinel:
        sentinel.bind(("127.0.0.1", 0))
        sentinel.listen()
        endpoint = f"127.0.0.1:{sentinel.getsockname()[1]}"
        variables = {
            "DBOS__CLOUD": "true",
            "DBOS_SYSTEM_DATABASE_URL": f"postgresql://sentinel@{endpoint}/sentinel",
            "DBOS_APP_NAME": "sentinel",
            "DBOS__VMID": "sentinel",
            "DBOS__APPVERSION": "sentinel",
            "DBOS__OTLP_TRACES_ENDPOINT": f"http://{endpoint}",
            "DBOS__OTLP_LOGS_ENDPOINT": f"http://{endpoint}",
            "DBOS__CONDUCTOR_APP_NAME": "sentinel",
            "DBOS__CONDUCTOR_KEY": "sentinel",
            "DBOS__CONDUCTOR_URL": f"ws://{endpoint}",
        }
        for name, value in variables.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_environment)
        monkeypatch.setattr(DBOSWorkers, "_launch", observe_launch)
        monkeypatch.setattr(DBOSWorkers, "_next", observe_handoff)
        result = asyncio.run(
            InprocRun(spec, run_database("b2_environment"), admin_dsn=admin_dsn).execute()
        )
        assert not select.select([sentinel], [], [], 0)[0]

    assert result.evaluation is not None and result.evaluation.verdict == "pass"
    assert launched == [("agent-1", False), ("agent-1", True)]
    assert stored == {("agent-1", "fivefold-b2-v1")}


def observe_state_creation(monkeypatch: pytest.MonkeyPatch) -> list[CreatedDatabase]:
    recorded: list[CreatedDatabase] = []

    def create(admin: str, name: str) -> CreatedDatabase:
        state = create_database(admin, name)
        recorded.append(state)
        return state

    monkeypatch.setattr(dbos_worker, "create_database", create)
    return recorded


def test_an_abandoned_workflow_cannot_be_resumed_by_a_new_invocation(
    run_database: Callable[[str], str],
    admin_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = scenario_spec()
    owned = observe_state_creation(monkeypatch)
    monkeypatch.setattr(
        dbos_worker, "mkdtemp", lambda *, prefix: mkdtemp(prefix=prefix, dir=tmp_path)
    )
    following = DBOSWorkers._next

    async def lose_parent(
        workers: DBOSWorkers, worker: str, reply: Reply | None, *, start: bool
    ) -> Delivery:
        delivery = await following(workers, worker, reply, start=start)
        assert start and isinstance(delivery, Request)
        raise DBOSInfrastructureError("test: abandoned Workflow")

    abandoned = InprocRun(spec, run_database("b2_abandoned"), admin_dsn=admin_dsn)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(DBOSWorkers, "_next", lose_parent)
            patch.setattr(dbos_worker, "_drop_state_database", lambda *args: False)
            with pytest.raises(DBOSInfrastructureError, match="abandoned Workflow"):
                asyncio.run(abandoned.execute())
        assert abandoned.result.evaluation is None
        with psycopg.connect(owned[0].dsn) as conn:
            query = "SELECT status, executor_id FROM dbos_agent_1.workflow_status"
            assert conn.execute(query).fetchall() == [("PENDING", "agent-1")]
        replay = InprocRun(spec, run_database("b2_fresh"), admin_dsn=admin_dsn)
        assert abandoned.spec.run_id == replay.spec.run_id
        result = asyncio.run(replay.execute())
        assert result.evaluation is not None and result.evaluation.verdict == "pass"
        assert owned[0].name != owned[1].name
        with psycopg.connect(owned[0].dsn) as conn:
            assert conn.execute(query).fetchall() == [("PENDING", "agent-1")]
    finally:
        for state in owned:
            drop_database(admin_dsn, state.name, oid=state.oid)


@pytest.mark.parametrize("stalls", [False, True])
def test_state_is_removed_on_success_or_expired_handoff(
    run_database: Callable[[str], str],
    admin_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    stalls: bool,
) -> None:
    owned = observe_state_creation(monkeypatch)
    invocation = InprocRun(
        scenario_spec(), run_database(f"b2_teardown_{int(stalls)}"), admin_dsn=admin_dsn
    )
    if stalls:
        monkeypatch.setattr(dbos_worker, "HOST_WAIT_S", 0.01)
        with pytest.raises(DBOSInfrastructureError, match="handoff timed out"):
            asyncio.run(invocation.execute())
        assert invocation.result.evaluation is None
    else:
        assert asyncio.run(invocation.execute()).evaluation is not None
    (state,) = owned
    with psycopg.connect(admin_dsn) as conn:
        assert (
            conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (state.name,)).fetchone()
            is None
        )


def test_teardown_drains_a_lingering_backend(
    admin_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as unrelated:
        with dbos_worker.state_database(admin_dsn, ("agent-1",)) as state:
            worker = psycopg.connect(make_conninfo(state.dsn, user="ff_dbos"), autocommit=True)

            def force_drop(conn: psycopg.Connection[Any], name: str, *, oid: int) -> bool:
                assert (name, oid) == (state.name, state.oid)
                rows = conn.execute(
                    "SELECT pid FROM pg_stat_activity WHERE datid = %s", (oid,)
                ).fetchall()
                if (worker.info.backend_pid,) in rows:
                    raise psycopg.errors.ObjectInUse("backend is still exiting")
                return drop_database_locked(conn, name, oid=oid)

            monkeypatch.setattr(dbos_worker, "drop_database_locked", force_drop)

        with worker, pytest.raises(psycopg.OperationalError):
            worker.execute("SELECT 1")
        assert (
            unrelated.execute(
                "SELECT oid FROM pg_database WHERE datname = %s", (state.name,)
            ).fetchone()
            is None
        )


def test_ff_dbos_cannot_read_or_write_sim(run_database: Callable[[str], str]) -> None:
    dsn = make_conninfo(run_database("b2_privileges"), user="ff_dbos")
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ff_dbos",)
        for statement in ("SELECT * FROM sim.effect", "INSERT INTO sim.effect DEFAULT VALUES"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_exhausted_step_ends_the_worker_and_still_scores(
    run_database: Callable[[str], str], admin_dsn: str
) -> None:
    cell = next(cell for cell in load_expected().cells if cell.name == "F12-agent_sim-C2-B2")
    (template,) = cell_plan(cell)
    plan = [{**template, "at": {**template["at"], "occurrence": attempt}} for attempt in (1, 2, 3)]
    run = InprocRun(scenario_spec(plan=plan), run_database("b2_exhausted"), admin_dsn=admin_dsn)
    result = asyncio.run(run.execute())
    assert result.ends["agent-1"] == (WorkerEnd.ERROR, "DBOS Step exhausted its three attempts.")
    assert result.evaluation is not None


def test_b2_refuses_a_cassette_before_connecting() -> None:
    scenario = load_scenario_file(DEV_001)
    spec = RunSpec(scenario, "B2", Contract.C2, 7, model=Scripted(record=True))
    with pytest.raises(ValueError, match="without a cassette"):
        InprocRun(spec, "dbname=unused")


def test_b2_requires_its_optional_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> Any:
        return None if name == "dbos" else find_spec(name)

    monkeypatch.setattr("importlib.util.find_spec", missing)
    with pytest.raises(RuntimeError, match=r"Install fivefold\[dbos\] to run B2"):
        asyncio.run(InprocRun(scenario_spec(), "dbname=unused").execute())
