"""A split run's lifecycle: start it, end it, score it (0004, 0008, 0009).

`start_run` makes the run, before its services start:

1. It loads the scenario and checks the fault plan: the scenario's own, or one given. The
   plan must fit the configuration and the run's timing (`fivefold.faults.plan`), and the
   split topology must be able to run it (`fivefold.harness.split.faults`). Seeded
   parameters are drawn from the run's seed, as in the inproc topology.
2. It makes the run's database from the migrated template database (0009), first
   migrating the template, or making it if it does not exist yet.
3. It records the run in `harness.run`, with topology `split`, model adapter `external`
   and the model the user names, and in the run registry, for the viewer (0012). It sets
   up the simulator's run with its credentials: the gateway in G1 and G2, the agent
   worker in B0 (0008).
4. It makes the run's secrets, and writes the run's directory (`rundir`).

`end_run` ends the run once the user's agent is done. It tells both services that the
agents' phase is over (G2 then closes the run, 0003), waits the drain window in wall
time, `2 x (D + max_hop_latency) + L` (0009), and stops the gateway, then the simulator,
which records the stop time. Last, it drops every message whose fate no service logged:
nothing delivered it. A request is dropped as `receiver_down`, an answer as
`caller_timed_out`: its caller stopped waiting before it arrived.

`score_run` scores the stopped run with the evaluator, which works on any run's database.

Checked in httpx 0.28.1: `AsyncClient(timeout=)` and `post(url, headers=)`, and
`Response.raise_for_status()`, which raises `HTTPStatusError` for a 4xx or 5xx status.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

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
    bootstrap_server,
    check_database_name,
    database_lock,
    drop_database_locked,
    migrate,
    recreate_database,
)
from fivefold.harness.registry import RegistryEntry, register
from fivefold.harness.rng import Generators
from fivefold.harness.run import drain_window_ms
from fivefold.harness.scheduler import HostScheduler
from fivefold.harness.split.credentials import AGENT, GATEWAY, HARNESS, Callers, issue
from fivefold.harness.split.faults import check_split
from fivefold.harness.split.rundir import (
    RUN_FILE,
    Config,
    RunDirError,
    SplitRun,
    clear_run,
    read_run,
    write_run,
)
from fivefold.sim import SimConfig, setup_run

ROOT = Path(__file__).resolve().parents[4]
DB_DIR = ROOT / "db"

DEFAULT_TEMPLATE = "fivefold_template"
"""The migrated template database that run databases are made from (0009)."""

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
    template: str = DEFAULT_TEMPLATE,
    replace: bool = False,
) -> SplitRun:
    """Make a split run and its directory; see the module. `plan` defaults to the
    scenario's fault list. With `replace`, a run already in `run_dir`, and a database of
    the same name, are replaced; without it they are refused."""
    scenario_text = scenario_path.read_text(encoding="utf-8")
    scenario = load_scenario(scenario_text)
    entries = list(plan) if plan is not None else _plan_of(scenario)
    planned = parse_plan(entries)
    check_configuration(planned, gateway=config != "B0")
    check_run(planned, params=scenario.params, contract=contract)
    check_split(planned, scenario)
    planned = seed_parameters(planned, Generators(seed).get("faults"))
    run_id = run_id_of(scenario, config, contract, seed)
    name = database if database is not None else default_database(run_id)
    check_database_name(name)
    check_database_name(template)
    if (run_dir / RUN_FILE).exists():
        if not replace:
            held = read_run(run_dir)
            raise RunDirError(
                f"{run_dir} holds run {held.run_id}: end and score it, then start again with"
                " replace, or choose another run directory"
            )
        clear_run(run_dir)
    dsn = _run_database(admin_dsn, name, template, replace=replace)
    run = SplitRun(run_dir, run_id, name, config, contract, seed)
    with psycopg.connect(make_conninfo(dsn, user="ff_harness"), autocommit=True) as conn:
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
    credentials = (GATEWAY,) if config != "B0" else (AGENT,)
    simulator = SimConfig.preset(
        contract, credentials=credentials, params=scenario.params.contract_params()
    )
    setup_run(make_conninfo(dsn, user="ff_sim"), scenario, simulator, vt=0)
    secrets = issue()
    write_run(run, scenario_text=scenario_text, secrets=secrets, callers=Callers.of(secrets))
    return run


def _plan_of(scenario: Scenario) -> list[Mapping[str, Any]]:
    """The scenario's fault list; a random plan is not built yet (0006)."""
    if isinstance(scenario.faults, dict):
        raise ValueError("random fault plans are not built yet (0006)")
    return list(scenario.faults)


def _run_database(admin_dsn: str, name: str, template: str, *, replace: bool) -> str:
    """Make the run's database from the migrated template; return its admin DSN. The
    component roles and the run registry are made first, if they are missing.

    The run database is replaced, when asked, and copied from the template under the
    database lock that every create and drop takes (`fivefold.harness.migrations`), so
    a study's cleanup never sees the name half changed.
    """
    bootstrap_server(admin_dsn, DB_DIR)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        found = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (template,)).fetchone()
    if found is None:
        recreate_database(admin_dsn, template)
    with psycopg.connect(make_conninfo(admin_dsn, dbname=template), autocommit=True) as conn:
        migrate(conn, DB_DIR / "migrations")
    with database_lock(admin_dsn) as conn:
        if replace:
            drop_database_locked(conn, name)
        try:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(name), sql.Identifier(template)
                )
            )
        except psycopg.errors.DuplicateDatabase:
            raise RunDirError(
                f"database {name} already exists: replace it, or name another"
            ) from None
    return make_conninfo(admin_dsn, dbname=name)


async def end_run(
    run: SplitRun, *, admin_dsn: str, simulator_url: str, gateway_url: str | None = None
) -> int:
    """End the run; see the module. Returns the virtual time it stopped at."""
    if run.ended:
        raise RunDirError(f"run {run.run_id} has already ended")
    if run.has_gateway and gateway_url is None:
        raise ValueError(f"run {run.run_id} is {run.config}: give the gateway's address")
    secret = run.secret(HARNESS)
    async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT_S) as client:
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
