"""The split topology's command line: `python -m fivefold.harness.split` (0009).

    python -m fivefold.harness.split start --scenario PATH --config G2 --contract C2 --seed 7
    python -m fivefold.harness.split serve simulator
    python -m fivefold.harness.split serve gateway
    python -m fivefold.harness.split end
    python -m fivefold.harness.split score --out result.json

`start` makes a run and its directory (`fivefold.harness.split.lifecycle`), and prints what
the agent needs. `serve` runs one of the run's services until it is stopped. `end` ends the
run after the drain window, and `score` scores it with the evaluator.

The gateway serves its admin API at `/admin` when `FIVEFOLD_ADMIN_API_TOKEN` holds the
API's bearer token: a human resolves escalations there.

Every command reads the run's directory from `--run-dir`, or `FIVEFOLD_RUN_DIR`, or
`split-run`. The database server comes from `FIVEFOLD_ADMIN_DSN`, a superuser connection
for `start`; the services and the other commands connect as their own roles on the same
server. The services' addresses come from `--gateway-url` and `--simulator-url`, or
`FIVEFOLD_GATEWAY_URL` and `FIVEFOLD_SIMULATOR_URL`.

A refused input, such as a plan the split topology cannot run, ends the command with one
line on standard error and exit status 1.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import yaml

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import ScenarioError
from fivefold.evaluator import result_json
from fivefold.harness.migrations import DEFAULT_ADMIN_DSN, MigrationError
from fivefold.harness.split.credentials import AGENT
from fivefold.harness.split.gateway import DEFAULT_ALLOWED_HOSTS, GatewayService, admin_api
from fivefold.harness.split.lifecycle import (
    DEFAULT_MODEL,
    DEFAULT_TEMPLATE,
    end_run,
    score_run,
    start_run,
)
from fivefold.harness.split.rundir import Config, RunDirError, read_run
from fivefold.harness.split.service import serve
from fivefold.harness.split.simulator import SimulatorService

DEFAULT_RUN_DIR = "split-run"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8000"
DEFAULT_SIMULATOR_URL = "http://127.0.0.1:8100"
GATEWAY_PORT = 8000
SIMULATOR_PORT = 8100


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fivefold.harness.split",
        description="Start, serve, end and score a split run, for your own agent.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path(os.environ.get("FIVEFOLD_RUN_DIR", DEFAULT_RUN_DIR)),
        help="the run's directory (default: FIVEFOLD_RUN_DIR, or split-run)",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_ADMIN_DSN),
        help="the database server (default: FIVEFOLD_ADMIN_DSN, or the local Compose one)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="make a run and its directory")
    start.add_argument("--scenario", type=Path, required=True, help="the scenario file")
    start.add_argument("--config", choices=["B0", "G1", "G2"], required=True)
    start.add_argument("--contract", choices=[str(level) for level in Contract], required=True)
    start.add_argument("--seed", type=int, default=7, help="the run's seed (default: 7)")
    start.add_argument(
        "--plan",
        help="a YAML file holding the fault plan, a list as a scenario's `faults` is, or"
        " `none` for no fault (default: the scenario's own plan)",
    )
    start.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="the model your agent uses, as the run records it (default: unknown)",
    )
    start.add_argument("--database", help="the run database's name (default: from the run id)")
    start.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE,
        help=f"the migrated template database (default: {DEFAULT_TEMPLATE})",
    )
    start.add_argument(
        "--replace",
        action="store_true",
        help="replace a run already in the run directory, and a database of the same name",
    )

    service = commands.add_parser("serve", help="run one of the run's services")
    service.add_argument("service", choices=["simulator", "gateway"])
    service.add_argument("--host", default="127.0.0.1", help="the address to listen on")
    service.add_argument("--port", type=int, help="the port (gateway 8000, simulator 8100)")
    service.add_argument(
        "--simulator-url",
        default=os.environ.get("FIVEFOLD_SIMULATOR_URL", DEFAULT_SIMULATOR_URL),
        help="where the gateway reaches the simulator",
    )
    service.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="another Host header the gateway's MCP endpoint accepts, such as name:*",
    )

    end = commands.add_parser("end", help="end the run after the drain window")
    end.add_argument(
        "--gateway-url",
        default=os.environ.get("FIVEFOLD_GATEWAY_URL", DEFAULT_GATEWAY_URL),
    )
    end.add_argument(
        "--simulator-url",
        default=os.environ.get("FIVEFOLD_SIMULATOR_URL", DEFAULT_SIMULATOR_URL),
    )

    score = commands.add_parser("score", help="score the run with the evaluator")
    score.add_argument("--out", type=Path, help="also write the run's result JSON here")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point. Returns the process exit status."""
    args = _parser().parse_args(argv)
    try:
        return _command(args)
    except (
        RunDirError,
        ScenarioError,
        MigrationError,
        ValueError,
        OSError,
        yaml.YAMLError,
        httpx.HTTPError,
        psycopg.Error,
    ) as error:
        print(f"split: {error}", file=sys.stderr)
        return 1


def _command(args: argparse.Namespace) -> int:
    run_dir: Path = args.run_dir
    dsn: str = args.dsn
    if args.command == "start":
        run = start_run(
            admin_dsn=dsn,
            scenario_path=args.scenario,
            config=cast(Config, args.config),
            contract=Contract(args.contract),
            seed=args.seed,
            run_dir=run_dir,
            plan=_plan(args.plan),
            model=args.model,
            database=args.database,
            template=args.template,
            replace=args.replace,
        )
        print(f"started {run.run_id} in database {run.database}; its directory is {run_dir}")
        if run.has_gateway:
            print("next: serve the simulator and the gateway, then point your agent at the")
            print(f"gateway's MCP endpoint, {DEFAULT_GATEWAY_URL}/mcp. In {run.config} your agent")
            print("holds no simulator credential (0008).")
        else:
            print("next: serve the simulator, then point your agent at its API,")
            print(f"{DEFAULT_SIMULATOR_URL}/api/CALL, with the header")
            print(f"Authorization: Bearer {run.secret(AGENT)}")
        return 0
    run = read_run(run_dir)
    if args.command == "serve":
        if args.service == "simulator":
            app: Any = SimulatorService(run, base_dsn=dsn).app()
            port = SIMULATOR_PORT if args.port is None else args.port
        else:
            hosts = (*DEFAULT_ALLOWED_HOSTS, *args.allowed_host)
            app = GatewayService(
                run,
                base_dsn=dsn,
                simulator_url=args.simulator_url,
                allowed_hosts=hosts,
                admin=admin_api(),
            ).app()
            port = GATEWAY_PORT if args.port is None else args.port
        serve(app, host=args.host, port=port)
        return 0
    if args.command == "end":
        stopped_vt = asyncio.run(
            end_run(
                run,
                admin_dsn=dsn,
                simulator_url=args.simulator_url,
                gateway_url=args.gateway_url,
            )
        )
        print(f"ended {run.run_id}: it stopped at {stopped_vt} virtual ms")
        return 0
    evaluation = score_run(run, admin_dsn=dsn)
    failing = [str(result.property) for result in evaluation.properties if result.verdict == "fail"]
    detail = f" (failing properties: {', '.join(failing)})" if failing else ""
    print(f"{run.run_id}: {evaluation.verdict}{detail}")
    if args.out is not None:
        args.out.write_text(result_json(evaluation), encoding="utf-8")
    return 0


def _plan(argument: str | None) -> list[dict[str, Any]] | None:
    """The plan a `--plan` argument names: None for the scenario's own, [] for `none`."""
    if argument is None:
        return None
    if argument == "none":
        return []
    data = yaml.safe_load(Path(argument).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(entry, dict) for entry in data):
        raise ValueError(f"{argument} must hold a list of fault entries, as a scenario's faults")
    return cast(list[dict[str, Any]], data)


if __name__ == "__main__":
    raise SystemExit(main())
