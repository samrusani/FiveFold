"""Record a run into a cassette, play a cassette back, and branch a recorded run (0009):
`python -m fivefold.recorder`.

    python -m fivefold.recorder record --demo 1 --config G2 --cassette demo-1-g2.json
    python -m fivefold.recorder play demo-1-g2.json \\
        --scenario scenarios/dev/dev-001-single-po.yaml
    FIVEFOLD_LIVE=1 python -m fivefold.recorder record --demo 1 --config G2 \\
        --cassette live.json --live ollama:MODEL --spend-cap-usd CAP --prices PRICES
    python -m fivefold.recorder branch demo-1-g2.json \\
        --scenario scenarios/dev/dev-001-single-po.yaml --at 4 \\
        --cassette branch.json --result branch-result.json

`record` runs a demo of record 0006, or a scenario file at a contract level with the
scenario's own fault list, with the scripted model, recording every model call. It runs in
a fresh run database (`--database`, default `fivefold_recorded`) and writes the cassette.

With `--live PROVIDER:MODEL`, `record` runs that live model instead (`fivefold.agents.live`).
It refuses, before it touches any database, unless the environment sets `FIVEFOLD_LIVE=1`,
`--spend-cap-usd` gives a cap above 0 USD, and the price file `--prices` prices the model.
`--param NAME=VALUE` sets one of the model's parameters, VALUE read as JSON, or as text when
it is not JSON. The study command takes the same options (`fivefold.harness.live_options`).
The cap holds across the command's runs; the command prints what the run spent and how
each agent worker ended, so a provider that could not be reached shows as a worker's
error. The cassette plays back with `play`, with no network.

`play` re-executes the run a cassette was recorded from, with the cassette's responses, in
another fresh run database (`--database`, default `fivefold_replayed`), and compares its
records with the original run's database (`--original`, default `fivefold_recorded`). It
exits with 0 only when the replay matches. It refuses to run in the original run's database
and only ever reads that one.

`branch` runs an experimental branch of the run a cassette was recorded from, the parent:
the same run, whose first K model calls (`--at K`) come from the parent's cassette, with
the replay's checks, and whose later calls go to the scripted model, or with `--live` and
the live options, as `record --live` takes them, to a live model. The branch is a new run,
in a fresh database (`--database`, default `fivefold_branched`), with its own run id, the
parent's with `.branch-K` added. It writes its own cassette (`--cassette`), holding all its
calls, the replayed ones included, whose header names the parent's run id and the branch
point, and its result JSON (`--result`), for `python -m fivefold.evaluator report`. `play`
replays the branch's cassette against the branch's database as any other. Before it
touches any database, `branch` refuses a K beyond the parent's cassette, a scenario other
than the one the parent was recorded on, a live run without the flag, the spend cap or the
price file, and outputs that name the parent's cassette or each other.

Each command drops the database it runs in, if it exists, and makes it again as a copy of
the migrated template database (`fivefold.harness.migrations.create_run_database`), as the
demos do; a database named as the template is refused. The admin connection comes from
`--dsn`, or `FIVEFOLD_ADMIN_DSN`. Only a live model's provider can need a key, and it
reads the key from the environment, never from this command. A refused input, such as a
scenario that changed or a fault plan the configuration does not have, ends the command
with one line on standard error and exit status 1. So does a template that cannot be
copied.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from fivefold.agents.replay import ReplayError
from fivefold.agents.worker import WorkerEnd
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.evaluator import result_json
from fivefold.harness.demo import DB_DIR, DEMO_BY_KEY, verdict_line
from fivefold.harness.live_options import add_live_options, check_live_options, live_from_args
from fivefold.harness.migrations import (
    DEFAULT_ADMIN_DSN,
    MigrationError,
    bootstrap_server,
    create_run_database,
)
from fivefold.harness.models import SCRIPTED, Live
from fivefold.harness.run import CONFIGS, RunResult, RunSpec
from fivefold.recorder.cassette import Cassette
from fivefold.recorder.playback import (
    RECORDS,
    VIEWER,
    PlaybackError,
    branch_spec,
    play,
    record,
    replay_spec,
)

RECORDED = "fivefold_recorded"
REPLAYED = "fivefold_replayed"
BRANCHED = "fivefold_branched"


def _fresh_database(admin_dsn: str, name: str) -> str:
    """Drop the database `name` if it exists, make it again as a copy of the migrated
    template database, and return its DSN."""
    return create_run_database(admin_dsn, name, DB_DIR / "migrations", replace=True).dsn


def _record(args: argparse.Namespace, live: Live | None) -> int:
    if args.demo is not None:
        spec = DEMO_BY_KEY[args.demo].spec(args.config, args.seed)
    else:
        scenario = load_scenario_file(args.scenario)
        spec = RunSpec(scenario, args.config, Contract(args.contract), args.seed)
    if live is not None:
        spec = replace(spec, model=live)
    dsn = _fresh_database(args.dsn, args.database)
    run, cassette = record(spec, dsn)
    cassette.save(args.cassette)
    print(f"Recorded {spec.run_id} in {args.database}: {len(cassette.calls)} model calls.")
    print(f"  verdict: {verdict_line(run.result.evaluation)}")
    print(f"  cassette: {args.cassette}, sha256 {cassette.digest()}")
    if live is not None and live.spend is not None:
        print(f"  model: {live.name}; {live.spend.summary()}")
        _print_ends(run.result)
    return 0


def _print_ends(result: RunResult) -> None:
    """How each agent worker ended, one line each."""
    for worker, (end, text) in sorted(result.ends.items()):
        print(f"  {worker}: {end}" + ("" if end is WorkerEnd.FINISHED else f", {text}"))


def _same_file(path: Path, other: Path) -> bool:
    """Whether `path` and `other` name one file: the same path once resolved, or one file
    through a hard link when both exist."""
    if path.resolve() == other.resolve():
        return True
    return path.exists() and other.exists() and path.samefile(other)


def _branch_spec(args: argparse.Namespace, live: Live | None) -> tuple[Cassette, RunSpec]:
    """The parent's cassette and the branch to run, checked before any database is
    touched: the parent's cassette, the scenario, the branch point and the outputs."""
    for output, name in ((args.cassette, "--cassette"), (args.result, "--result")):
        if _same_file(output, args.parent):
            raise ValueError(f"{name} {output} would replace the parent's cassette")
    if _same_file(args.cassette, args.result):
        raise ValueError(f"--cassette and --result name the same file, {args.cassette}")
    parent = Cassette.load(args.parent)
    scenario = load_scenario_file(args.scenario)
    return parent, branch_spec(parent, scenario, args.at, SCRIPTED if live is None else live)


def _holds_run(admin_dsn: str, name: str, run_id: str) -> bool:
    """Whether the database `name` exists and holds the run `run_id`, read as `ff_viewer`.
    A database that does not exist, or holds no run records, holds no run."""
    try:
        with psycopg.connect(make_conninfo(admin_dsn, dbname=name, user=VIEWER)) as conn:
            conn.read_only = True
            found = conn.execute(
                "SELECT 1 FROM harness.run WHERE run_id = %s", (run_id,)
            ).fetchone()
    except (psycopg.OperationalError, psycopg.errors.UndefinedTable):
        return False
    return found is not None


def _branch(args: argparse.Namespace, parent: Cassette, spec: RunSpec, live: Live | None) -> int:
    if _holds_run(args.dsn, args.database, parent.run.run_id):
        raise ValueError(
            f"the database {args.database} holds the parent run {parent.run.run_id};"
            " give --database another name, so the branch does not replace its parent"
        )
    dsn = _fresh_database(args.dsn, args.database)
    run, cassette = record(spec, dsn)
    evaluation = run.result.evaluation
    if evaluation is None:
        raise RuntimeError("the branch was never evaluated")
    cassette.save(args.cassette)
    args.result.write_bytes(result_json(evaluation).encode("utf-8"))
    print(
        f"Branched {spec.run_id} from {parent.run.run_id} at branch point {args.at} in"
        f" {args.database}: {len(cassette.calls)} model calls, the first"
        f" {run.models.served} replayed from the parent's cassette."
    )
    print(f"  model after the branch point: {run.models.name}")
    print(f"  verdict: {verdict_line(evaluation)}")
    print(f"  cassette: {args.cassette}, sha256 {cassette.digest()}")
    print(f"  result: {args.result}")
    if live is not None and live.spend is not None:
        print(f"  spend: {live.spend.summary()}")
    _print_ends(run.result)
    return 0


def _play(args: argparse.Namespace) -> int:
    cassette = Cassette.load(args.cassette)
    scenario = load_scenario_file(args.scenario)
    replay_spec(cassette, scenario)  # refuse a scenario that does not fit before any change
    original = make_conninfo(args.dsn, dbname=args.original)
    replay = _fresh_database(args.dsn, args.database)
    playback = play(cassette, scenario, original=original, replay=replay)
    print(
        f"Replayed {playback.run_id} in {args.database} from the cassette"
        f" {playback.cassette[:16]} ({playback.calls} model calls)."
    )
    print(f"  verdict: {playback.verdict}")
    differences = {difference.records: difference for difference in playback.differences}
    for name, _ in RECORDS:
        difference = differences.get(name)
        if difference is None:
            print(f"  {name}: identical, sha256 {playback.digests[name][:16]}")
            continue
        print(f"  {name}: differs from row {difference.row}")
        print(f"    original: {difference.original}")
        print(f"    replay:   {difference.replay}")
    if playback.matches:
        print(f"The replay matches the original run in {args.original}.")
        return 0
    print(f"The replay does not match the original run in {args.original}.")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Record or play back a run. Returns the process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m fivefold.recorder",
        description="Record a run's model calls into a cassette, or play a cassette back.",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_ADMIN_DSN),
        help="admin connection string (default: FIVEFOLD_ADMIN_DSN)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    recording = commands.add_parser("record", help="run with the scripted model and record it")
    source = recording.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", choices=sorted(DEMO_BY_KEY), help="a demo of record 0006")
    source.add_argument("--scenario", type=Path, help="a scenario file, with its own faults")
    recording.add_argument(
        "--contract", choices=[str(level) for level in Contract], help="for a scenario file"
    )
    recording.add_argument("--config", choices=CONFIGS, required=True)
    recording.add_argument("--seed", type=int, default=7, help="the run seed (default 7)")
    recording.add_argument("--cassette", type=Path, required=True, help="where to write it")
    recording.add_argument(
        "--database", default=RECORDED, help=f"the run's database (default {RECORDED})"
    )
    add_live_options(recording)

    playing = commands.add_parser("play", help="replay a cassette and compare the records")
    playing.add_argument("cassette", type=Path)
    playing.add_argument("--scenario", type=Path, required=True, help="the run's scenario file")
    playing.add_argument(
        "--original", default=RECORDED, help=f"the original run's database (default {RECORDED})"
    )
    playing.add_argument(
        "--database", default=REPLAYED, help=f"the replay's database (default {REPLAYED})"
    )

    branching = commands.add_parser(
        "branch",
        help="replay a cassette's first K model calls, then switch model, as a new run",
    )
    branching.add_argument("parent", type=Path, help="the parent run's cassette")
    branching.add_argument(
        "--scenario", type=Path, required=True, help="the parent run's scenario file"
    )
    branching.add_argument(
        "--at",
        type=int,
        required=True,
        metavar="K",
        help="the branch point: how many of the parent's model calls to replay",
    )
    branching.add_argument(
        "--cassette", type=Path, required=True, help="where to write the branch's cassette"
    )
    branching.add_argument(
        "--result", type=Path, required=True, help="where to write the branch's result JSON"
    )
    branching.add_argument(
        "--database", default=BRANCHED, help=f"the branch's database (default {BRANCHED})"
    )
    add_live_options(branching)

    args = parser.parse_args(argv)
    if args.command == "record" and args.scenario is not None and args.contract is None:
        parser.error("record --scenario needs --contract")
    if args.command == "record" and args.demo is not None and args.contract is not None:
        parser.error("a demo has its own contract level; leave out --contract")
    if args.command == "play" and args.database == args.original:
        parser.error("the replay needs its own database, not the original run's")
    if args.command in ("record", "branch"):
        check_live_options(parser, args)
    try:
        live = live_from_args(args, os.environ) if args.command in ("record", "branch") else None
        branch = _branch_spec(args, live) if args.command == "branch" else None
        bootstrap_server(args.dsn, DB_DIR)
        if branch is not None:
            return _branch(args, *branch, live)
        return _record(args, live) if args.command == "record" else _play(args)
    except (OSError, MigrationError, PlaybackError, ReplayError, ValueError) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
