"""The study of milestone M4, episode by episode: `python -m fivefold.harness.study`.

M4 pilots 10 to 20 live episodes to measure the real cost, sets the spend cap, then runs
B0, G1 and G2 across the contract levels (docs/plan.md; spec, Testing). A study runs a
matrix of episodes on one scenario and saves each run's result JSON in an output
directory, for `python -m fivefold.evaluator report`. An episode is one run (0009): a
cell, a seed and the study's model.

    python -m fivefold.harness.study --out study --cells F2 --contracts C0 C2 --episodes 2
    python -m fivefold.evaluator report study --csv study.csv --markdown study.md

Its inputs:

- `--scenario`: the scenario file, dev-001 by default.
- `--configs` and `--contracts`: the configurations and contract levels, by default B0,
  G1 and G2, and C0 to C3. Each level must be one the scenario lists.
- `--cells`: the cells to run in those configurations and levels. The cells are those of
  `faults/expected.yaml` (0006), and `all`, the default, names every one. A fault, such
  as `F2`, names its cells; a fault and variant, such as `F11-after_tool_call`, names
  that variant's; a cell's own name, such as `F2-C2-G2`, names that cell. `none` names
  the cells with no fault, one in each configuration and level: the scenario's task with
  an empty fault plan. Each cell's fault plan, and any change it makes to the scenario,
  come from `fivefold.harness.cells`.
- `--episodes N` and `--seed S`: each cell runs N times, with the seeds S, S+1, ...,
  S+N-1. S defaults to the seed of `faults/expected.yaml`.
- The model: the scripted one by default, or `--live PROVIDER:MODEL` with
  `--spend-cap-usd`, `--prices` and any `--param`, as `record --live` takes them
  (`fivefold.harness.live_options`).
- `--out DIR`: the output directory. It must be new or empty, so a study never replaces
  another's files.
- `--database-prefix` and `--keep-databases`: episode N runs in a fresh, migrated
  database named `PREFIX_NNNN` (default prefix `fivefold_study`), dropped once the
  episode ends unless `--keep-databases` is given. The admin connection comes from
  `--dsn`, or `FIVEFOLD_ADMIN_DSN`.

Episodes run in a fixed order: by seed, then by cell. Cells go by fault (no fault first,
then F1 to F12), variant, contract level and configuration. So a study that stops partway
has run every cell once before it runs any cell twice.

Its outputs, in DIR:

- `CELL-seed-SEED.json`: each episode's result JSON, such as `F2-C2-G2-seed-7.json` or
  `none-C2-B0-seed-8.json`. A cell's name holds its fault, variant, contract level and
  configuration, so no two episodes share a file. The same inputs with the scripted model
  give the same bytes (0009).
- `cassettes/CELL-seed-SEED.json`: each live episode's cassette (0009). It replays the
  run with no network. `python -m fivefold.recorder play` compares the replay with the
  original run's database, so it needs the episode's database kept. The report reads only
  the `.json` files directly in DIR, so it never reads a cassette.
- `spend-cap/CELL-seed-SEED.json`: the result of an episode the spend cap cut short.

A live study is refused before it touches any database unless the environment sets
`FIVEFOLD_LIVE=1`, the spend cap is above 0 USD, and the price file prices the model. One
spend meter serves every episode, so the cap holds across the study. Once the spend
reaches the cap, the next model call is refused, and its worker ends as `budget`
(`fivefold.agents.live`). An episode with a refused call is cut short, and its verdict
tells more about the cap than about the stack: its result goes to `spend-cap/`, which a
report over DIR leaves out, and its cassette goes to `cassettes/` as usual. The study
stops once the spend reaches the cap, since every later episode's first call would be
refused.

The command prints a line for each episode: its verdict, the verdict `faults/expected.yaml`
expects when it covers the scenario, how each agent worker ended, and in a live study the
episode's model calls and the spend so far. It exits with status 0 when it ran every
episode, or when the spend cap stopped it. A refused input, such as an unknown cell or an
output directory that is not empty, ends the command with one line on standard error and
exit status 1, before any database is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psycopg

from fivefold.agents.worker import SPEND_CAP_TEXT, WorkerEnd
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario_file
from fivefold.evaluator import RunResult as Evaluation
from fivefold.evaluator import result_json
from fivefold.faults.plan import check_configuration, check_run, parse_plan
from fivefold.harness.cells import Cell, Expected, cell_plan, cell_scenario, load_expected
from fivefold.harness.demo import DB_DIR, DEV_001
from fivefold.harness.live_options import add_live_options, check_live_options, live_from_args
from fivefold.harness.migrations import (
    DEFAULT_ADMIN_DSN,
    MigrationError,
    bootstrap,
    check_database_name,
    drop_database,
    migrate,
    recreate_database,
)
from fivefold.harness.models import SCRIPTED, Live, ModelSpec
from fivefold.harness.run import CONFIGS, GATEWAY_CONFIGS, Config, InprocRun, RunSpec
from fivefold.recorder.cassette import Cassette

ALL: Final = "all"
"""The `--cells` name of every cell of `faults/expected.yaml`."""

NO_FAULT: Final = "none"
"""The `--cells` name of the cells with no fault, and the start of each one's name."""

CASSETTES: Final = "cassettes"
"""The subdirectory of the output directory that holds live episodes' cassettes."""

SPEND_CAP_DIR: Final = "spend-cap"
"""The subdirectory that holds the results of episodes the spend cap cut short."""

DEFAULT_PREFIX: Final = "fivefold_study"
"""The default start of each episode's database name."""

MAX_SEED: Final = 2**63 - 1
"""The largest seed `harness.run` can record: its seed column is a bigint."""


# Cells ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NoFault:
    """A cell with no fault: the scenario's task, with an empty fault plan, in one
    contract level and configuration."""

    contract: Contract
    config: Config

    @property
    def name(self) -> str:
        """A short id such as `none-C2-G2`."""
        return f"{NO_FAULT}-{self.contract}-{self.config}"


StudyCell = Cell | NoFault
"""A cell of a study: a cell of `faults/expected.yaml`, or a cell with no fault."""


def as_config(name: str) -> Config:
    """The configuration `name`; raises `ValueError` for one this build does not run."""
    for config in CONFIGS:
        if config == name:
            return config
    raise ValueError(f"no configuration {name!r}; this build runs {', '.join(CONFIGS)}")


def selector_names(cell: StudyCell) -> frozenset[str]:
    """The `--cells` names that name `cell`. A cell of `faults/expected.yaml` has `all`,
    its fault, its fault and variant when it has a variant, and its own name. A cell with
    no fault has `none` and its own name."""
    if isinstance(cell, NoFault):
        return frozenset({NO_FAULT, cell.name})
    names = {ALL, cell.fault, cell.name}
    if cell.variant is not None:
        names.add(f"{cell.fault}-{cell.variant}")
    return frozenset(names)


def cell_order(cell: StudyCell) -> tuple[int, int, str, str, int, int]:
    """A cell's place in the study's order: no fault first, then by fault number,
    variant, contract level and configuration."""
    contract = list(Contract).index(cell.contract)
    config = CONFIGS.index(as_config(cell.config))
    if isinstance(cell, NoFault):
        return (0, 0, "", "", contract, config)
    number = re.fullmatch(r"F(\d+)", cell.fault)
    return (1, int(number[1]) if number else 0, cell.fault, cell.variant or "", contract, config)


def select_cells(
    expected: Iterable[Cell],
    names: Sequence[str],
    *,
    configs: Sequence[Config],
    contracts: Sequence[Contract],
) -> tuple[StudyCell, ...]:
    """The cells that `names` name (`selector_names`) in `configs` and `contracts`, each
    once, in the study's order (`cell_order`).

    Raises `ValueError` for a name that names no cell there.
    """
    candidates: list[StudyCell] = [
        cell for cell in expected if cell.config in configs and cell.contract in contracts
    ]
    candidates += [NoFault(contract, config) for contract in contracts for config in configs]
    chosen: dict[str, StudyCell] = {}
    for name in names:
        matched = [cell for cell in candidates if name in selector_names(cell)]
        if not matched:
            raise ValueError(
                f"no cell {name!r} in {', '.join(configs)} at {', '.join(map(str, contracts))}:"
                f" give {ALL}, {NO_FAULT}, a fault such as F2, a fault and variant such as"
                " F11-after_tool_call, or a cell such as F2-C2-G2 (faults/expected.yaml)"
            )
        for cell in matched:
            chosen.setdefault(cell.name, cell)
    return tuple(sorted(chosen.values(), key=cell_order))


def expected_outcome(cell: StudyCell, expected: Expected, scenario: Scenario) -> str | None:
    """The outcome `faults/expected.yaml` expects of `cell`, such as `fail (1, 2, 3)`; None
    for a cell with no fault, or when the file is for another scenario."""
    if isinstance(cell, NoFault) or expected.scenario != scenario.id:
        return None
    return outcome_text(cell.verdict, cell.failing)


def outcome_text(verdict: str, failing: Iterable[int]) -> str:
    """A verdict and its failing properties, as `faults/expected.yaml` gives them."""
    numbers = ", ".join(str(number) for number in failing)
    return f"{verdict} ({numbers})" if numbers else verdict


# Episodes ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Episode:
    """One run of the study: a cell and a seed (0009). `number` is its place in the
    study's order, from 1."""

    number: int
    cell: StudyCell
    seed: int

    @property
    def name(self) -> str:
        """The name of its files: the cell's name and the seed, such as `F2-C2-G2-seed-7`."""
        return f"{self.cell.name}-seed-{self.seed}"

    def spec(self, scenario: Scenario, model: ModelSpec = SCRIPTED) -> RunSpec:
        """The run: the cell's scenario and fault plan, in its configuration and contract
        level, with the episode's seed and `model`."""
        config = as_config(self.cell.config)
        if isinstance(self.cell, NoFault):
            return RunSpec(scenario, config, self.cell.contract, self.seed, plan=[], model=model)
        return RunSpec(
            cell_scenario(self.cell, scenario),
            config,
            self.cell.contract,
            self.seed,
            plan=cell_plan(self.cell),
            model=model,
        )


def study_seeds(first: int, count: int) -> tuple[int, ...]:
    """The seeds of each cell's episodes: `first`, `first + 1`, ..., `count` of them."""
    if count < 1:
        raise ValueError(f"a study runs at least 1 episode per cell, not {count}")
    if first < 0 or first + count - 1 > MAX_SEED:
        raise ValueError(
            f"a seed is from 0 to {MAX_SEED}, so the seeds cannot run from {first}"
            f" to {first + count - 1}"
        )
    return tuple(range(first, first + count))


def study_episodes(cells: Sequence[StudyCell], seeds: Sequence[int]) -> tuple[Episode, ...]:
    """Every cell with every seed, in the study's order: by seed, then by cell, numbered
    from 1. Raises `ValueError` if two episodes would share a name."""
    pairs = [(cell, seed) for seed in seeds for cell in cells]
    episodes = tuple(
        Episode(number, cell, seed) for number, (cell, seed) in enumerate(pairs, start=1)
    )
    names = [episode.name for episode in episodes]
    if len(set(names)) != len(names):
        raise ValueError("two episodes of the study would share a name")
    return episodes


def database_name(prefix: str, episode: Episode) -> str:
    """The database episode runs in: the prefix and its number, such as `fivefold_study_0001`."""
    return f"{prefix}_{episode.number:04d}"


@dataclass(frozen=True)
class Study:
    """What a study runs: the scenario, the episodes in order, and the model."""

    scenario: Scenario
    episodes: tuple[Episode, ...]
    model: ModelSpec
    expected: Expected
    """The expected outcomes, which name the cells; they apply when they cover the scenario."""

    def describe(self) -> str:
        """The study in one line: its scenario, model, episodes, cells and seeds."""
        if isinstance(self.model, Live):
            model = f"the live model {self.model.name}"
        else:
            model = "the scripted model"
        cells = len({episode.cell.name for episode in self.episodes})
        seeds = sorted({episode.seed for episode in self.episodes})
        seed_text = f"seed {seeds[0]}" if len(seeds) == 1 else f"seeds {seeds[0]} to {seeds[-1]}"
        return (
            f"Study of {self.scenario.id} with {model}: {len(self.episodes)} episodes,"
            f" {cells} cells, {seed_text}."
        )


def check_study(study: Study, prefix: str) -> None:
    """Refuse a study that could not run to its end, before any database is touched.

    Each episode's contract level must be one the scenario lists, its fault plan must fit
    its configuration and timing as a run checks them (`fivefold.faults.plan`), and its
    database name must be valid. Raises `ValueError`.
    """
    scenario = study.scenario
    for episode in study.episodes:
        spec = episode.spec(scenario)
        if spec.contract not in scenario.contracts:
            levels = ", ".join(map(str, scenario.contracts))
            raise ValueError(f"{scenario.id} runs at {levels}, not at {spec.contract}")
        try:
            plan = parse_plan(spec.plan or [])
            check_configuration(plan, gateway=spec.config in GATEWAY_CONFIGS)
            check_run(plan, params=spec.scenario.params, contract=spec.contract)
            check_database_name(database_name(prefix, episode))
        except (ValueError, MigrationError) as error:
            raise ValueError(f"episode {episode.name}: {error}") from None


def check_out(out: Path) -> None:
    """Refuse an output directory that is a file, or a directory that holds anything."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"{out} is not a new or empty directory; give the study its own")


# Running -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """How an episode ended, and where its files went."""

    episode: Episode
    evaluation: Evaluation
    ends: dict[str, tuple[WorkerEnd, str]]
    """How each agent worker ended, and why."""
    result: Path
    cassette: Path | None
    """The live episode's cassette; None with the scripted model."""
    calls: int | None
    """The live episode's model calls, refused ones included; None with the scripted model."""
    database: str | None
    """The episode's database, when it is kept."""

    @property
    def cut(self) -> bool:
        """Whether the spend cap refused one of its model calls."""
        return cut_by_spend_cap(self.ends)


def cut_by_spend_cap(ends: dict[str, tuple[WorkerEnd, str]]) -> bool:
    """Whether a worker ended because the spend cap refused its call."""
    return any(end is WorkerEnd.BUDGET and text == SPEND_CAP_TEXT for end, text in ends.values())


def run_episode(
    admin_dsn: str, study: Study, episode: Episode, *, out: Path, database: str, keep: bool
) -> Outcome:
    """Run `episode` in a fresh, migrated database `database`, and save its result, and
    its cassette when the model is live, in `out`. The database is dropped once the run
    ends, unless `keep`."""
    spec = episode.spec(study.scenario, study.model)
    dsn = recreate_database(admin_dsn, database)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            migrate(conn, DB_DIR / "migrations")
        run = InprocRun(spec, dsn)
        asyncio.run(run.execute())
    finally:
        if not keep:
            drop_database(admin_dsn, database)
    evaluation = run.result.evaluation
    if evaluation is None:
        raise RuntimeError(f"episode {episode.name} was not evaluated")
    ends = dict(run.result.ends)
    cassette: Cassette | None = None
    if isinstance(spec.model, Live):
        cassette = run.models.cassette
        if cassette is None:
            raise RuntimeError(f"episode {episode.name} recorded no cassette")
    folder = out / SPEND_CAP_DIR if cut_by_spend_cap(ends) else out
    folder.mkdir(parents=True, exist_ok=True)
    result = folder / f"{episode.name}.json"
    result.write_bytes(result_json(evaluation).encode("utf-8"))
    saved: Path | None = None
    if cassette is not None:
        (out / CASSETTES).mkdir(parents=True, exist_ok=True)
        saved = out / CASSETTES / f"{episode.name}.json"
        cassette.save(saved)
    return Outcome(
        episode=episode,
        evaluation=evaluation,
        ends=ends,
        result=result,
        cassette=saved,
        calls=None if cassette is None else len(cassette.calls),
        database=database if keep else None,
    )


def run_study(
    admin_dsn: str, study: Study, *, out: Path, prefix: str, keep: bool = False
) -> Iterator[Outcome]:
    """Run the study's episodes in order, each in its own database, and yield how each
    ended. A live study stops once its spend reaches the cap: every later episode's first
    model call would be refused."""
    meter = study.model.spend if isinstance(study.model, Live) else None
    for episode in study.episodes:
        if meter is not None and meter.reached():
            return
        database = database_name(prefix, episode)
        try:
            outcome = run_episode(admin_dsn, study, episode, out=out, database=database, keep=keep)
        except Exception as error:
            error.add_note(f"in episode {episode.number}, {episode.name}")
            raise
        yield outcome


# The command ---------------------------------------------------------------------------------


def episode_line(study: Study, outcome: Outcome) -> str:
    """One line on how an episode ended."""
    episode, evaluation = outcome.episode, outcome.evaluation
    failing = [prop.property for prop in evaluation.properties if str(prop.verdict) == "fail"]
    line = f"[{episode.number}/{len(study.episodes)}] {episode.name}: "
    line += outcome_text(str(evaluation.verdict), failing)
    expected = expected_outcome(episode.cell, study.expected, study.scenario)
    if expected is not None:
        line += f", expected {expected}"
    ends = ", ".join(
        f"{worker} {end}" + ("" if end is WorkerEnd.FINISHED else f" ({text})")
        for worker, (end, text) in sorted(outcome.ends.items())
    )
    line += f"; {ends}"
    meter = study.model.spend if isinstance(study.model, Live) else None
    if outcome.calls is not None and meter is not None:
        line += f"; {outcome.calls} model calls, {meter.spent_usd} USD spent so far"
    if outcome.cut:
        line += f"; cut short by the spend cap, so its result is in {SPEND_CAP_DIR}/"
    if outcome.database is not None:
        line += f"; kept in the database {outcome.database}"
    return line


def plan_study(args: argparse.Namespace, model: ModelSpec) -> Study:
    """The study the command's arguments describe, checked before anything runs."""
    expected = load_expected()
    scenario = load_scenario_file(args.scenario)
    configs = [config for config in CONFIGS if config in args.configs]
    contracts = [contract for contract in Contract if str(contract) in args.contracts]
    cells = select_cells(expected.cells, args.cells, configs=configs, contracts=contracts)
    first = expected.seed if args.seed is None else args.seed
    episodes = study_episodes(cells, study_seeds(first, args.episodes))
    study = Study(scenario=scenario, episodes=episodes, model=model, expected=expected)
    check_study(study, args.database_prefix)
    return study


def main(argv: Sequence[str] | None = None) -> int:
    """Run a study. Returns the process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m fivefold.harness.study",
        description="Run a matrix of episodes and save each run's result for the report.",
    )
    parser.add_argument(
        "--out", type=Path, required=True, metavar="DIR", help="a new or empty directory"
    )
    parser.add_argument(
        "--scenario", type=Path, default=DEV_001, help="the scenario file (default: dev-001)"
    )
    parser.add_argument(
        "--configs",
        nargs="+",
        choices=CONFIGS,
        default=list(CONFIGS),
        metavar="CONFIG",
        help=f"configurations among {', '.join(CONFIGS)} (default: all three)",
    )
    levels = [str(contract) for contract in Contract]
    parser.add_argument(
        "--contracts",
        nargs="+",
        choices=levels,
        default=levels,
        metavar="LEVEL",
        help=f"contract levels among {', '.join(levels)} (default: all four)",
    )
    parser.add_argument(
        "--cells",
        nargs="+",
        default=[ALL],
        metavar="CELL",
        help=f"{ALL}, {NO_FAULT}, a fault (F2), a fault and variant (F11-after_tool_call),"
        f" or a cell (F2-C2-G2) of faults/expected.yaml (default: {ALL})",
    )
    parser.add_argument(
        "--episodes", type=int, default=1, metavar="N", help="episodes per cell (default 1)"
    )
    parser.add_argument(
        "--seed",
        type=int,
        metavar="S",
        help="the first seed; each cell runs with S, S+1, ... (default: faults/expected.yaml's)",
    )
    parser.add_argument(
        "--database-prefix",
        default=DEFAULT_PREFIX,
        metavar="PREFIX",
        help=f"episode N runs in the database PREFIX_NNNN (default {DEFAULT_PREFIX})",
    )
    parser.add_argument(
        "--keep-databases", action="store_true", help="keep each episode's database"
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_ADMIN_DSN),
        help="admin connection string (default: FIVEFOLD_ADMIN_DSN)",
    )
    add_live_options(parser)
    args = parser.parse_args(argv)
    check_live_options(parser, args)
    out: Path = args.out
    try:
        live = live_from_args(args, os.environ)
        study = plan_study(args, SCRIPTED if live is None else live)
        check_out(out)
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            bootstrap(conn, DB_DIR / "bootstrap.sql")
        print(study.describe(), flush=True)
        ran = 0
        try:
            for outcome in run_study(
                args.dsn, study, out=out, prefix=args.database_prefix, keep=args.keep_databases
            ):
                ran += 1
                print(episode_line(study, outcome), flush=True)
        finally:
            if live is not None and live.spend is not None:
                print(f"Model {live.name}; {live.spend.summary()}.")
        print(end_line(study, ran, out))
    except (OSError, ValueError, MigrationError) as error:
        notes = "".join(f" ({note})" for note in getattr(error, "__notes__", ()))
        print(f"study failed: {error}{notes}", file=sys.stderr)
        return 1
    return 0


def end_line(study: Study, ran: int, out: Path) -> str:
    """How many episodes ran, where their results are, and how to report them."""
    total = len(study.episodes)
    if ran < total:
        text = f"The spend cap stopped the study after {ran} of {total} episodes."
    else:
        text = f"Ran all {total} episodes."
    return (
        f"{text} Results are in {out}. Report them with: python -m fivefold.evaluator"
        f" report {out} --csv runs.csv --markdown report.md"
    )


if __name__ == "__main__":
    sys.exit(main())
