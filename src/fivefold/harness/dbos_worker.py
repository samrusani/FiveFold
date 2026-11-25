"""Give B2 one private DBOS database per episode and hand Steps to FiveFold."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Any

import psycopg
from langchain_core.messages import HumanMessage
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import URL

from fivefold.harness.baseline_worker import (
    BaselineInfrastructureError,
    BaselineWorkers,
    Delivery,
    Done,
    Reply,
    Request,
    _encode,
)
from fivefold.harness.migrations import (
    CreatedDatabase,
    create_database,
    database_lock,
    drop_database_locked,
)

if TYPE_CHECKING:
    from fivefold.harness.run import InprocRun

HOST_WAIT_S = 30
ROLE = "ff_dbos"
BACKEND_EXIT_MS = 30_000


class DBOSInfrastructureError(BaselineInfrastructureError):
    """The B2 episode stopped before it could be scored."""


def _schema(worker: str) -> str:
    return f"dbos_{worker.replace('-', '_')}"


def _drop_state_database(admin_dsn: str, state: CreatedDatabase) -> bool:
    with database_lock(admin_dsn) as conn:
        try:
            return drop_database_locked(conn, state.name, oid=state.oid)
        except psycopg.errors.ObjectInUse:
            # A killed child can leave server backends that are still exiting.
            conn.execute(
                "SELECT pg_terminate_backend(pid, %s) FROM pg_stat_activity WHERE datid = %s",
                (BACKEND_EXIT_MS, state.oid),
            )
            return drop_database_locked(conn, state.name, oid=state.oid)


@contextmanager
def state_database(admin_dsn: str, workers: tuple[str, ...]) -> Iterator[CreatedDatabase]:
    """Allocate and record owned state before any worker can launch (0020)."""
    directory = Path(mkdtemp(prefix="fivefold_dbos_"))
    owner = directory / "owner.json"
    created: CreatedDatabase | None = None
    try:
        created = create_database(admin_dsn, directory.name)
        with owner.open("x", encoding="utf-8") as file:
            json.dump({"name": created.name, "oid": created.oid}, file)
        with psycopg.connect(created.dsn) as conn:
            role = conn.execute(
                """
                SELECT rolcanlogin AND NOT (rolsuper OR rolcreatedb OR rolcreaterole
                                              OR rolreplication OR rolbypassrls)
                       AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid)
                FROM pg_roles WHERE rolname = %s
                """,
                (ROLE,),
            ).fetchone()
            if role != (True,):
                raise DBOSInfrastructureError("B2 needs the unprivileged ff_dbos login role")
            database = sql.Identifier(created.name)
            login = sql.Identifier(ROLE)
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database))
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, login))
            for worker in workers:
                schema = sql.Identifier(_schema(worker))
                conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
                conn.execute(
                    sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(schema, login)
                )
        yield created
    finally:
        if created is None or _drop_state_database(admin_dsn, created):
            owner.unlink(missing_ok=True)
            directory.rmdir()


def _state_url(state: CreatedDatabase) -> str:
    # Copy only routing and server verification options. The admin password and
    # role settings do not belong in the DBOS process.
    allowed = {"host", "hostaddr", "port", "sslmode", "sslrootcert", "connect_timeout"}
    connection = conninfo_to_dict(state.dsn)
    url = URL.create(
        "postgresql+psycopg",
        username=ROLE,
        password=os.environ.get("FIVEFOLD_DBOS_PASSWORD"),
        database=state.name,
        query={
            key: str(value)
            for key, value in connection.items()
            if key in allowed and value is not None
        },
    )
    return url.render_as_string(hide_password=False)


class DBOSWorkers(BaselineWorkers):
    """A subprocess and stable executor ID for each virtual worker."""

    def __init__(self, run: InprocRun, state: CreatedDatabase) -> None:
        super().__init__(run, DBOSInfrastructureError, "DBOS workflow failed outside virtual I/O")
        self.system_url = _state_url(state)
        self.children: dict[str, asyncio.subprocess.Process] = {}
        self.recovering: set[str] = set()

    async def work(self, worker: str, *, resume: bool) -> None:
        if resume:
            self.recovering.add(worker)
        await super().work(worker, resume=resume)

    async def _wait_for_handoff(self, worker: str, reply: Reply | None, *, start: bool) -> Delivery:
        async with self.handoff:
            try:
                return await asyncio.wait_for(
                    self._next(worker, reply, start=start), timeout=HOST_WAIT_S
                )
            except TimeoutError:
                raise DBOSInfrastructureError(f"DBOS handoff timed out for {worker}") from None
            except (OSError, ValueError, TypeError, KeyError):
                raise DBOSInfrastructureError(f"DBOS child handoff failed for {worker}") from None

    async def _next(self, worker: str, reply: Reply | None, *, start: bool) -> Delivery:
        resume = worker in self.recovering
        if resume:
            self.recovering.remove(worker)
            await self._kill(worker)
        if start or resume:
            await self._launch(worker, resume=resume)
        elif reply is not None:
            await self._send(worker, {"retry": reply[2] is not None, "result": reply[1]})

        child = self.children[worker]
        assert child.stdout is not None
        raw = await child.stdout.readline()
        if not raw:
            raise DBOSInfrastructureError(f"DBOS child closed its handoff for {worker}")
        message = json.loads(raw)
        kind = message["kind"]
        if kind == "done":
            if resume or await child.wait() != 0:
                raise DBOSInfrastructureError(f"DBOS child ended unexpectedly for {worker}")
            del self.children[worker]
            return Done(message["result"])
        if kind not in {"model", "tool", "report"}:
            raise DBOSInfrastructureError(f"DBOS child sent an invalid handoff for {worker}")
        step = Request(kind, message["id"], message["attempt"], b"", message["payload"])
        if resume:
            valid = reply is not None and step.activity_id == reply[0].activity_id
            valid = valid and step.attempt == 1
        elif reply is not None and reply[2] is not None:
            valid = (
                step.activity_id == reply[0].activity_id and step.attempt == reply[0].attempt + 1
            )
        else:
            valid = step.attempt == 1 and (
                reply is None or step.activity_id != reply[0].activity_id
            )
        if not valid:
            raise DBOSInfrastructureError(f"DBOS Step sequence differed from FiveFold for {worker}")
        return step

    async def _launch(self, worker: str, *, resume: bool) -> None:
        scenario = self.run.spec.scenario
        self.children[worker] = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "fivefold.harness.dbos_process",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={},
            limit=1_000_000,
        )
        await self._send(
            worker,
            {
                "worker": worker,
                "schema": _schema(worker),
                "resume": resume,
                "system_database_url": self.system_url,
                "input": {
                    "initial": _encode(HumanMessage(scenario.task)),
                    "max_steps": scenario.budget.max_agent_steps,
                    "max_tokens": scenario.budget.max_tokens,
                },
            },
        )

    async def _send(self, worker: str, message: dict[str, Any]) -> None:
        stream = self.children[worker].stdin
        assert stream is not None
        stream.write((json.dumps(message) + "\n").encode("utf-8"))
        await stream.drain()

    async def _kill(self, worker: str) -> None:
        child = self.children[worker]
        if child.returncode is None:
            with suppress(ProcessLookupError):
                child.kill()
        await child.wait()
        del self.children[worker]

    async def stop(self) -> None:
        for worker in tuple(self.children):
            await self._kill(worker)
