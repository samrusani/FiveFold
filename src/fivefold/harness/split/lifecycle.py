"""A split run's lifecycle: start it, end it, score it (0004, 0008, 0009).

`start_run` makes the run, before its services start:

1. It loads the scenario and checks the fault plan: the scenario's own, or one given. The
   plan must be a fixed list, not a random plan, must fit the configuration and the run's
   timing (`fivefold.faults.plan`), and the split topology must be able to run it
   (`fivefold.harness.split.faults`). Seeded parameters are drawn from the run's seed, as
   in the inproc topology.
2. It reserves a new owner-only run directory, then copies the migrated template
   database (0009). It migrates the template or makes it if it does not exist yet
   (`fivefold.harness.migrations.create_run_database`). The template is
   FIVEFOLD_TEMPLATE_DB, or `fivefold_template`, unless the start names another. A
   database that already has the run's name is refused; with replace it is dropped
   first, but only with FiveFold's marker or matching migration history, and if
   `harness.run` holds this run or no run yet,
   as a copy of the template whose start failed before recording its run. Run ids that
   differ only in case or punctuation, such as those of scenarios `sample-a` and
   `sample_a`, get one default name, so replace never drops a database that holds
   another run, or one that is no run database. The check and the drop happen under the
   database lock.
3. It records the run in `harness.run`, with topology `split`, model adapter `external`
   and the model the user names, and in the run registry, for the viewer (0012). It sets
   up the simulator's run with its credentials: the gateway in G1 and G2, the agent
   worker in B0 (0008).
4. It makes the run's secrets, and writes the files in the run's directory (`rundir`).

`end_run` ends the run once the user's agent is done. It tells both services that the
agents' phase is over (G2 then closes the run, 0003), waits the drain window in wall
time, `2 x (D + max_hop_latency) + L` (0009), and stops the gateway, then the simulator,
which records the stop time. Last, it drops every message whose fate no service logged:
nothing delivered it. A request is dropped as `receiver_down`, an answer as
`caller_timed_out`: its caller stopped waiting before it arrived. A stopped service
answers a repeated drain with HTTP 200, so a retry can finish interrupted cleanup.

`score_run` scores the stopped run with the evaluator, which works on any run's database.

Checked in httpx 0.28.1: `AsyncClient(timeout=, trust_env=False)` and `post(url, headers=)`, and
`Response.raise_for_status()`, which raises `HTTPStatusError` for a 4xx or 5xx status.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.conninfo import make_conninfo

from fivefold.db_connection import role_dsn
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario, scenario_digest
from fivefold.evaluator import RunResult as Evaluation
from fivefold.evaluator import evaluate_run
from fivefold.faults.plan import (
    check_configuration,
    check_run,
    parse_plan,
    seed_parameters,
)
from fivefold.harness.clock import HostClock
from fivefold.harness.log import RunRecord, SharedPgHarnessLog
from fivefold.harness.migrations import (
    Connection,
    DatabaseExists,
    MigrationError,
    bootstrap_server,
    check_database_name,
    create_run_database,
    drop_database,
    template_database,
)
from fivefold.harness.registry import RegistryEntry, register
from fivefold.harness.rng import Generators
from fivefold.harness.run import drain_window_ms
from fivefold.harness.scheduler import HostScheduler
from fivefold.harness.split.credentials import AGENT, GATEWAY, HARNESS, Callers, issue
from fivefold.harness.split.faults import SplitRefused, check_split
from fivefold.harness.split.rundir import (
    Config,
    RunDirError,
    SplitRun,
    new_run_directory,
    write_run,
)
from fivefold.paths import DB_DIR
from fivefold.sim import SimConfig, setup_run

DEFAULT_MODEL = "unknown"
"""The model a split run records when the user names none."""

CONTROL_TIMEOUT_S = 30.0
"""How long `end` waits for a service to answer a control request, in wall-clock seconds."""


MAX_NAME_BYTES = 63
"""PostgreSQL keeps the first 63 bytes of a name (NAMEDATALEN - 1)."""


def default_database(run_id: str) -> str:
    """A run database's name made from its run id: `fivefold_split_` and the id, in
    lowercase letters, digits and underscores.

    A name longer than 63 bytes keeps its start, then an underscore and the first 8 hex
    digits of the run id's SHA-256, as the tests' database names do (`tests/conftest.py`).
    Cut short without the hash, the names of runs that differ only at the end, such as two
    seeds of a scenario with a long id, were one, and `start` with replace dropped the
    other run's database.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", run_id.lower()).strip("_")
    name = f"fivefold_split_{slug}"
    if len(name.encode()) <= MAX_NAME_BYTES:
        return name
    digest = hashlib.sha256(run_id.encode()).hexdigest()[:8]
    head = name[: MAX_NAME_BYTES - len(digest) - 1].rstrip("_")
    return f"{head}_{digest}"


def run_id_of(scenario: Scenario, config: Config, contract: Contract, seed: int) -> str:
    """A run's id, as the inproc topology makes it: the run's choices."""
    return f"{scenario.id}.{config}.{contract}.{seed}"


def start_run(
    *,
    admin_dsn: str,
    scenario_path: Path,
    config: Config,
    contract: Contract,
    seed: int,
    run_dir: Path,
    plan: Sequence[Mapping[str, Any]] | None = None,
    model: str = DEFAULT_MODEL,
    database: str | None = None,
    template: str | None = None,
    replace: bool = False,
) -> SplitRun:
    """Make a split run and its directory; see the module. `plan` defaults to the
    scenario's fault list, and `template` to `template_database()`. The run directory
    must be new. With `replace`, the database of the same name may be replaced if
    it has FiveFold's marker or matching migration history and holds this run or no
    run yet. A failed start rolls back its directory and files."""
    scenario_text = scenario_path.read_text(encoding="utf-8")
    scenario = load_scenario(scenario_text)
    if scenario.memory_enabled:
        raise SplitRefused("the split topology does not support memory-enabled scenarios (0023)")
    entries = list(plan) if plan is not None else _plan_of(scenario)
    planned = parse_plan(entries)
    check_configuration(planned, gateway=config != "B0")
    check_run(planned, params=scenario.params, contract=contract)
    check_split(planned, scenario)
    planned = seed_parameters(planned, Generators(seed).get("faults"))
    run_id = run_id_of(scenario, config, contract, seed)
    name = database if database is not None else default_database(run_id)
    template = template_database() if template is None else template
    check_database_name(name)
    check_database_name(template)
    if name == template:
        raise RunDirError(f"database {name} is the template run databases are made from")
    with (
        new_run_directory(run_dir) as reserved,
        _run_database(admin_dsn, name, template, run_id=run_id, replace=replace) as dsn,
    ):
        run = SplitRun(run_dir, run_id, name, config, contract, seed)
        with psycopg.connect(role_dsn(dsn, "ff_harness"), autocommit=True) as conn:
            SharedPgHarnessLog(conn).run_recorded(
                RunRecord(
                    run_id=run_id,
                    scenario_id=scenario.id,
                    scenario_digest=scenario_digest(scenario),
                    config=config,
                    contract=str(contract),  # type: ignore[arg-type]
                    seed=seed,
                    model_adapter="external",
                    model=model,
                    params={
                        "params": scenario.params.model_dump(mode="json"),
                        "plan": [entry.model_dump(mode="json") for entry in planned],
                        "workers": 1,
                        "agent_workers": 1,
                    },
                    topology="split",
                )
            )
        credentials = (GATEWAY,) if config != "B0" else (AGENT,)
        simulator = SimConfig.preset(
            contract, credentials=credentials, params=scenario.params.contract_params()
        )
        setup_run(role_dsn(dsn, "ff_sim"), scenario, simulator, vt=0)
        secrets = issue()
        write_run(
            run,
            reserved,
            scenario_text=scenario_text,
            secrets=secrets,
            callers=Callers.of(secrets),
        )
        register(
            dsn,
            RegistryEntry(
                database=name,
                run_id=run_id,
                scenario_id=scenario.id,
                config=config,
                contract=str(contract),
                seed=seed,
            ),
        )
    return run


def _plan_of(scenario: Scenario) -> list[Mapping[str, Any]]:
    """The scenario's fault list. A random plan is refused (`fivefold.harness.split.faults`)."""
    if isinstance(scenario.faults, dict):
        raise SplitRefused(
            "the split topology runs a fixed fault plan only: give the scenario a list of"
            " faults, or a plan file; a random plan runs in the inproc topology (0006)"
        )
    return list(scenario.faults)


@contextmanager
def _run_database(
    admin_dsn: str, name: str, template: str, *, run_id: str, replace: bool
) -> Iterator[str]:
    """Make the run's database from the migrated template; return its admin DSN. The
    component roles and the run registry are made first, if they are missing.

    The template is made or migrated forward, the run database is replaced, when asked
    and when it is run `run_id`'s (`_check_this_runs`), and the template is copied, in one
    hold of the database lock that every create and drop takes
    (`fivefold.harness.migrations.create_run_database`), so a study's cleanup never sees
    the name half changed, and no other start makes it in between.
    """
    bootstrap_server(admin_dsn, DB_DIR)

    def check_this_runs(conn: Connection) -> None:
        if replace:
            _check_this_runs(conn, admin_dsn, name, run_id)

    try:
        created = create_run_database(
            admin_dsn,
            name,
            DB_DIR / "migrations",
            template=template,
            replace=replace,
            before_copy=check_this_runs,
        )
    except DatabaseExists:
        raise RunDirError(f"database {name} already exists: replace it, or name another") from None
    except MigrationError as error:
        raise RunDirError(str(error)) from error
    try:
        yield created.dsn
    except BaseException:
        drop_database(admin_dsn, created.name, oid=created.oid)
        raise


def _check_this_runs(conn: Connection, admin_dsn: str, name: str, run_id: str) -> None:
    """Allow replacement if `harness.run` holds this run or no run yet.
    `conn` holds the database lock.

    Raises `RunDirError`, and keeps the database, when it holds another run or is no run
    database: it has no `harness.run`.
    """
    row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if row is None:
        return
    with psycopg.connect(make_conninfo(admin_dsn, dbname=name), autocommit=True) as found:
        table = found.execute("SELECT to_regclass('harness.run')").fetchone()
        if table is None or table[0] is None:
            raise RunDirError(
                f"database {name} is not a run database, and replace drops only this run's:"
                " name another"
            )
        held = sorted({str(run[0]) for run in found.execute("SELECT run_id FROM harness.run")})
    others = [held_id for held_id in held if held_id != run_id]
    if others:
        raise RunDirError(
            f"database {name} holds run {others[0]}, and replace drops only this run's:"
            " name another"
        )


async def end_run(
    run: SplitRun, *, admin_dsn: str, simulator_url: str, gateway_url: str | None = None
) -> int:
    """End the run; see the module. Returns the virtual time it stopped at."""
    if run.ended:
        raise RunDirError(f"run {run.run_id} has already ended")
    if run.has_gateway and gateway_url is None:
        raise ValueError(f"run {run.run_id} is {run.config}: give the gateway's address")
    secret = run.secret(HARNESS)
    async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT_S, trust_env=False) as client:
        if gateway_url is not None and run.has_gateway:
            await _control(client, gateway_url, "drain", secret)
        await _control(client, simulator_url, "drain", secret)
        # The drain window passes in wall time: in the split topology the run's virtual
        # time follows the host's clock (0009).
        await HostScheduler(HostClock(HostClock.host_ms())).sleep(drain_window_ms(run.scenario()))
        if gateway_url is not None and run.has_gateway:
            await _control(client, gateway_url, "stop", secret)
        stopped = await _control(client, simulator_url, "stop", secret)
    stopped_vt = int(stopped["stopped_vt"])
    drop_unlogged(run, admin_dsn)
    run.mark_ended(stopped_vt)
    return stopped_vt


async def _control(client: httpx.AsyncClient, url: str, action: str, secret: str) -> dict[str, Any]:
    response = await client.post(
        f"{url.rstrip('/')}/harness/{action}", headers={"authorization": f"Bearer {secret}"}
    )
    response.raise_for_status()
    answer: dict[str, Any] = response.json()
    return answer


def drop_unlogged(run: SplitRun, admin_dsn: str) -> int:
    """Drop every message whose fate no service logged; return how many there were."""
    with psycopg.connect(run.conninfo(admin_dsn, "ff_harness"), autocommit=True) as conn:
        updated = conn.execute(
            "UPDATE harness.message_log SET fate = 'dropped', drop_reason = CASE kind"
            " WHEN 'answer' THEN 'caller_timed_out' ELSE 'receiver_down' END"
            " WHERE fate IS NULL"
        )
        return updated.rowcount


def score_run(run: SplitRun, *, admin_dsn: str) -> Evaluation:
    """Score the stopped run with the evaluator, as `ff_eval`, and store its result."""
    if not run.ended:
        raise RunDirError(f"run {run.run_id} has not ended: end it first")
    return evaluate_run(run.conninfo(admin_dsn, "ff_eval"), run.scenario())
