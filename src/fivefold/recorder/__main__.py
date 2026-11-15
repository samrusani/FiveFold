"""Record a run into a cassette, and play a cassette back (0009): `python -m fivefold.recorder`.

    python -m fivefold.recorder record --demo 1 --config G2 --cassette demo-1-g2.json
    python -m fivefold.recorder play demo-1-g2.json \\
        --scenario scenarios/dev/dev-001-single-po.yaml
    FIVEFOLD_LIVE=1 python -m fivefold.recorder record --demo 1 --config G2 \\
        --cassette live.json --live ollama:MODEL --spend-cap-usd CAP --prices PRICES

`record` runs a demo of record 0006, or a scenario file at a contract level with the
scenario's own fault list, with the scripted model, recording every model call. It runs in
a fresh run database (`--database`, default `fivefold_recorded`) and writes the cassette.

With `--live PROVIDER:MODEL`, `record` runs that live model instead (`fivefold.agents.live`).
It refuses, before it touches any database, unless the environment sets `FIVEFOLD_LIVE=1`,
`--spend-cap-usd` gives a cap above 0 USD, and the price file `--prices` prices the model.
`--param NAME=VALUE` sets one of the model's parameters, VALUE read as JSON, or as text when
it is not JSON. The cap holds across the command's runs; the command prints what the run
spent and how each agent worker ended, so a provider that could not be reached shows as a
worker's error. The cassette plays back with `play`, with no network.

`play` re-executes the run a cassette was recorded from, with the cassette's responses, in
another fresh run database (`--database`, default `fivefold_replayed`), and compares its
records with the original run's database (`--original`, default `fivefold_recorded`). It
exits with 0 only when the replay matches. It refuses to run in the original run's database
and only ever reads that one.

Each command drops and recreates the database it runs in, as the demos do. The admin
connection comes from `--dsn`, or `FIVEFOLD_ADMIN_DSN`. Only a live model's provider can
need a key, and it reads the key from the environment, never from this command. A
refused input, such as a scenario that changed or a fault plan the configuration does
not have, ends the command with one line on standard error and exit status 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from pydantic import JsonValue

from fivefold.agents.live import (
    CALL_LIMIT_S,
    SpendMeter,
    check_provider,
    parse_live_name,
    provider_model,
)
from fivefold.agents.replay import ReplayError
from fivefold.agents.worker import WorkerEnd
from fivefold.domain.contracts import Contract
from fivefold.domain.prices import load_price_file
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.demo import DB_DIR, DEMO_BY_KEY, verdict_line
from fivefold.harness.migrations import DEFAULT_ADMIN_DSN, bootstrap, migrate, recreate_database
from fivefold.harness.models import Live, check_live, check_live_flag
from fivefold.harness.run import CONFIGS, RunSpec
from fivefold.recorder.cassette import Cassette, plain_object
from fivefold.recorder.playback import RECORDS, PlaybackError, play, record, replay_spec

RECORDED = "fivefold_recorded"
REPLAYED = "fivefold_replayed"


def _fresh_database(admin_dsn: str, name: str) -> str:
    """Drop and recreate the database `name`, migrate it, and return its DSN."""
    dsn = recreate_database(admin_dsn, name)
    with psycopg.connect(dsn, autocommit=True) as conn:
        migrate(conn, DB_DIR / "migrations")
    return dsn


def _usd(text: str) -> Decimal:
    """An amount in USD, from the command line."""
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not an amount: {text!r}") from None


def _params(items: Sequence[str]) -> dict[str, JsonValue]:
    """The model's parameters from `NAME=VALUE` items, each VALUE as JSON or as text."""
    params: dict[str, JsonValue] = {}
    for item in items:
        name, equals, value = item.partition("=")
        if not name or not equals:
            raise ValueError(f"a model parameter is NAME=VALUE, not {item!r}")
        try:
            params[name] = json.loads(value)
        except json.JSONDecodeError:
            params[name] = value
    return params


def _live(args: argparse.Namespace) -> Live:
    """The live model `--live` names, with its spend cap and prices, checked before any
    database is touched: the flag, the provider and its parameters, the cap, and the
    model's price."""
    check_live_flag(os.environ)
    provider, model = parse_live_name(args.live)
    params = _params(args.param)
    check_provider(provider, params)
    plain_object(params, "the model's parameters")  # the cassette's form of them
    meter = SpendMeter(args.spend_cap_usd, load_price_file(args.prices))
    live = Live(provider, model, params, spend=meter, limit_s=args.call_limit_s)
    check_live(live, os.environ)
    provider_model(provider, model, params)  # their values; building the model sends nothing
    return live


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
        for worker, (end, text) in sorted(run.result.ends.items()):
            print(f"  {worker}: {end}" + ("" if end is WorkerEnd.FINISHED else f", {text}"))
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
    recording.add_argument(
        "--live",
        metavar="PROVIDER:MODEL",
        help="run this live model instead of the scripted one; needs FIVEFOLD_LIVE=1",
    )
    recording.add_argument(
        "--spend-cap-usd",
        type=_usd,
        metavar="USD",
        help="with --live: refuse model calls once the spend reaches this many USD",
    )
    recording.add_argument(
        "--prices", type=Path, metavar="PATH", help="with --live: the price file"
    )
    recording.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="with --live: a model parameter, VALUE as JSON; repeat for more",
    )
    recording.add_argument(
        "--call-limit-s",
        type=float,
        default=CALL_LIMIT_S,
        metavar="SECONDS",
        help=f"with --live: fail a model call that waits longer (default {CALL_LIMIT_S:g})",
    )

    playing = commands.add_parser("play", help="replay a cassette and compare the records")
    playing.add_argument("cassette", type=Path)
    playing.add_argument("--scenario", type=Path, required=True, help="the run's scenario file")
    playing.add_argument(
        "--original", default=RECORDED, help=f"the original run's database (default {RECORDED})"
    )
    playing.add_argument(
        "--database", default=REPLAYED, help=f"the replay's database (default {REPLAYED})"
    )

    args = parser.parse_args(argv)
    if args.command == "record" and args.scenario is not None and args.contract is None:
        parser.error("record --scenario needs --contract")
    if args.command == "record" and args.demo is not None and args.contract is not None:
        parser.error("a demo has its own contract level; leave out --contract")
    if args.command == "play" and args.database == args.original:
        parser.error("the replay needs its own database, not the original run's")
    if args.command == "record":
        live_options = args.spend_cap_usd is not None or args.prices is not None or args.param
        if args.live is None and live_options:
            parser.error("--spend-cap-usd, --prices and --param go with --live")
        if args.live is not None and (args.spend_cap_usd is None or args.prices is None):
            parser.error("--live needs --spend-cap-usd and --prices")
    try:
        live = _live(args) if args.command == "record" and args.live is not None else None
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            bootstrap(conn, DB_DIR / "bootstrap.sql")
        return _record(args, live) if args.command == "record" else _play(args)
    except (OSError, PlaybackError, ReplayError, ValueError) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
