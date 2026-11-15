"""Recorded playback and experimental branches (0009, Playback and branching).

`record` runs a run with the scripted adapter, wrapped by the recorder, with the live
adapter, which always records, or as an experimental branch, which always records too, and
returns the run and its cassette. `play` re-executes the run a cassette was recorded from,
in a fresh run database: the same scenario, configuration, contract level, seed, fault plan
and workers, with the replay adapter serving the cassette, so it needs no network. The
replay adapter fails the run if a request's hash differs from the recorded one, or if the
run asks for more or fewer calls than the cassette holds (0009). `play` then compares the
replay's records with the original run's, row by row, ignoring wall-clock columns and
fields, whose names end in `wall_at` or `wall_ms` (0009, Determinism contract, as amended
by 0014):

- the harness's message log, fault log, actor log, final reports, stop time and model
  calls, whose token counts a replay serves from the cassette;
- the simulator's effects ledger and approval events;
- the gateway's action events;
- the evaluation JSON, without the run summary's model adapter, which a replay records as
  `replay` by design, and without its wall-clock fields at any depth: recovery latency by
  the wall clock (0014).

Before it runs anything, `play` checks that the scenario is the one the cassette was
recorded on, that the original run recorded this very cassette, and that the replay's
database holds no run yet. It reads both databases only as `ff_viewer`, which can only
select (0008), in read-only transactions, so it never writes to the original run's
database. The replay itself writes only to its own database, through the run's components,
each with its own role.

`branch_spec` is an experimental branch of the run a cassette was recorded from: the run
`replay_spec` gives, whose first K model calls the branch adapter replays before it hands
every later call to the scripted model or a live one (`fivefold.agents.branch`). The branch
is a new run, with its own run id, and its own cassette, which `play` replays as any other:
its header names the parent's run id and the branch point, and the replay records them too.

The command line is `python -m fivefold.recorder` (`fivefold.recorder.__main__`).

Checked in psycopg 3.3.9: `Connection.read_only` (`_connection_base.py`), `errors.
UndefinedTable` (`errors.py`), and jsonb values arriving as Python data (`types/json.py`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any, Final, LiteralString

import psycopg
from psycopg.conninfo import make_conninfo
from pydantic import ValidationError

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, scenario_digest
from fivefold.evaluator import WALL_CLOCK_SUFFIXES as EVALUATOR_WALL_CLOCK_SUFFIXES
from fivefold.evaluator import without_wall_clock
from fivefold.harness.models import SCRIPTED, Branch, Live, Replay, Scripted
from fivefold.harness.run import Config, InprocRun, RunSpec
from fivefold.recorder.cassette import Cassette, RunHeader, canonical_json, sha256

VIEWER: Final = "ff_viewer"
"""The role playback reads with: it can only select (0008)."""

RECORDS: Final[tuple[tuple[str, LiteralString], ...]] = (
    ("message log", "SELECT to_jsonb(t) FROM harness.message_log t ORDER BY seq"),
    ("fault log", "SELECT to_jsonb(t) FROM harness.fault_log t ORDER BY seq"),
    ("actor log", "SELECT to_jsonb(t) FROM harness.actor_log t ORDER BY seq"),
    ("final reports", "SELECT to_jsonb(t) FROM harness.final_report t ORDER BY seq"),
    ("run stop", "SELECT to_jsonb(t) FROM harness.run_stop t ORDER BY run_id"),
    ("model calls", "SELECT to_jsonb(t) FROM harness.model_call t ORDER BY call_no"),
    ("effects ledger", "SELECT to_jsonb(t) FROM sim.effect t ORDER BY commit_seq"),
    ("approval events", "SELECT to_jsonb(t) FROM sim.approval_event t ORDER BY seq"),
    ("action events", "SELECT to_jsonb(t) FROM gw.action_event t ORDER BY seq"),
    ("evaluation", "SELECT to_jsonb(t) FROM eval.run_result t ORDER BY run_id"),
)
"""The records a playback compares, by name, each with the query that reads it in order."""

WALL_CLOCK_SUFFIXES: Final = EVALUATOR_WALL_CLOCK_SUFFIXES
"""Wall-clock columns end in one of these, and are for metrics only (0009). They are the
evaluator's, which leaves fields with these endings out of a result too (0014)."""


class PlaybackError(Exception):
    """A playback refused before it ran: the scenario, the original run or the replay's
    database does not fit the cassette."""


@dataclass(frozen=True)
class Difference:
    """Where the replay's records first differ from the original run's."""

    records: str
    """The name of the records, as in `RECORDS`."""
    row: int
    """The first row that differs, counted from 1."""
    original: str | None
    """That row of the original run, as canonical JSON; None past its last row."""
    replay: str | None
    """That row of the replay; None past its last row."""


@dataclass(frozen=True)
class Playback:
    """What a playback found."""

    run_id: str
    cassette: str
    """The cassette's digest."""
    calls: int
    """The model calls the replay served: all of the cassette's."""
    verdict: str
    """The replay's verdict, from its own evaluation."""
    digests: dict[str, str]
    """The SHA-256 of each of the replay's records, by name, as canonical JSON lines."""
    differences: tuple[Difference, ...]

    @property
    def matches(self) -> bool:
        """Whether the replay's records match the original run's."""
        return not self.differences


def record(spec: RunSpec, dsn: str) -> tuple[InprocRun, Cassette]:
    """Run `spec`, recording every model call, in the fresh, migrated run database `dsn`;
    return the run and its cassette. A live model and a branch record as they run; any
    other spec runs with the scripted adapter, wrapped by the recorder."""
    model = spec.model if isinstance(spec.model, Live | Branch) else Scripted(record=True)
    run = InprocRun(replace(spec, model=model), dsn)
    asyncio.run(run.execute())
    if run.models.cassette is None:
        raise RuntimeError("the run recorded no cassette")
    return run, run.models.cassette


def replay_spec(cassette: Cassette, scenario: Scenario) -> RunSpec:
    """The run `cassette` was recorded from, on `scenario`, with the replay adapter.

    Raises `PlaybackError` if `scenario` is not the one the cassette was recorded on.
    """
    run = cassette.run
    if scenario.id != run.scenario_id:
        raise PlaybackError(f"the cassette was recorded on {run.scenario_id}, not {scenario.id}")
    digest = scenario_digest(scenario)
    if digest != run.scenario_digest:
        raise PlaybackError(
            f"scenario {scenario.id} changed since the recording: its digest is {digest},"
            f" and the cassette's is {run.scenario_digest}"
        )
    config: Config = run.config
    plan, workers = run.params.get("plan"), run.params.get("workers")
    if not isinstance(plan, list) or not isinstance(workers, int) or isinstance(workers, bool):
        raise PlaybackError("the cassette's run names no fault plan or no number of workers")
    entries = [entry for entry in plan if isinstance(entry, dict)]
    if len(entries) != len(plan):
        raise PlaybackError("the cassette's fault plan holds an entry that is not an object")
    spec = RunSpec(
        scenario,
        config,
        Contract(run.contract),
        run.seed,
        plan=entries,
        workers=workers,
        model=Replay(cassette),
    )
    if spec.run_id != run.run_id:
        raise PlaybackError(f"the cassette names run {run.run_id}, but it would be {spec.run_id}")
    return spec


def branch_spec(
    parent: Cassette, scenario: Scenario, point: int, model: Scripted | Live = SCRIPTED
) -> RunSpec:
    """An experimental branch (0009) of the run `parent` was recorded from, on `scenario`:
    the parent's first `point` model calls replayed from `parent`, then `model`.

    Raises `PlaybackError` if `scenario` is not the one the parent was recorded on, and
    `BranchError` if `point` is not from 0 to the number of calls `parent` holds.
    """
    spec = replay_spec(parent, scenario)
    return replace(spec, model=Branch(parent, point, model))


def play(cassette: Cassette, scenario: Scenario, *, original: str, replay: str) -> Playback:
    """Re-execute the run `cassette` was recorded from in the database `replay`, and
    compare its records with the original run's, in the database `original`.

    `replay` must be a fresh, migrated run database. Raises `PlaybackError` before running
    anything if the scenario, the original run or `replay` does not fit the cassette, and
    `ReplayError` if the replay fails (0009).
    """
    spec = replay_spec(cassette, scenario)
    _check_original(original, cassette)
    _check_fresh(replay)
    run = InprocRun(spec, replay)
    asyncio.run(run.execute())
    evaluation = run.result.evaluation
    replayed = records(replay)
    return Playback(
        run_id=spec.run_id,
        cassette=cassette.digest(),
        calls=run.models.served,
        verdict=str(evaluation.verdict) if evaluation is not None else "not evaluated",
        digests={name: sha256("\n".join(rows)) for name, rows in replayed.items()},
        differences=compare(records(original), replayed),
    )


def records(dsn: str) -> dict[str, list[str]]:
    """The records a playback compares, of the run in the database `dsn`, row by row as
    canonical JSON, without wall-clock columns and fields, read as `ff_viewer`."""
    with _viewer(dsn) as conn:
        return {
            name: [_comparable(name, row[0]) for row in conn.execute(query)]
            for name, query in RECORDS
        }


def compare(original: dict[str, list[str]], replay: dict[str, list[str]]) -> tuple[Difference, ...]:
    """Where each of the replay's records first differs from the original run's."""
    differences: list[Difference] = []
    for name, _ in RECORDS:
        before, after = original[name], replay[name]
        if before == after:
            continue
        row = next(
            (
                number
                for number, pair in enumerate(zip(before, after, strict=False), start=1)
                if pair[0] != pair[1]
            ),
            min(len(before), len(after)) + 1,
        )
        differences.append(
            Difference(
                records=name,
                row=row,
                original=before[row - 1] if row <= len(before) else None,
                replay=after[row - 1] if row <= len(after) else None,
            )
        )
    return tuple(differences)


def _comparable(name: str, row: object) -> str:
    """A row as canonical JSON, without wall-clock columns; the evaluation also without
    its wall-clock fields, at any depth, and the model adapter its run summary names."""
    if not isinstance(row, dict):
        raise TypeError(f"a row of the {name} is not a JSON object: {row!r}")
    data: dict[str, Any] = {
        column: value for column, value in row.items() if not column.endswith(WALL_CLOCK_SUFFIXES)
    }
    if name == "evaluation":
        data = {column: without_wall_clock(value) for column, value in data.items()}
    result = data.get("result")
    if name == "evaluation" and isinstance(result, dict) and isinstance(result.get("run"), dict):
        summary = {key: value for key, value in result["run"].items() if key != "model_adapter"}
        data["result"] = {**result, "run": summary}
    return canonical_json(data)


def _viewer(dsn: str) -> psycopg.Connection[Any]:
    """A read-only connection to the database `dsn`, as `ff_viewer`."""
    conn = psycopg.connect(make_conninfo(dsn, user=VIEWER))
    conn.read_only = True
    return conn


def _check_original(dsn: str, cassette: Cassette) -> None:
    """Refuse unless the run in the database `dsn` recorded `cassette`."""
    with _viewer(dsn) as conn:
        rows = conn.execute(
            "SELECT run_id, scenario_id, scenario_digest, config, contract, seed, topology,"
            " params, model_adapter, model, cassette_digest, parent_run_id, branch_point"
            " FROM harness.run"
        ).fetchall()
    if len(rows) != 1:
        raise PlaybackError("the original run's database holds no run")
    row = rows[0]
    digest = cassette.digest()
    if row[10] != digest:
        raise PlaybackError(
            f"the original run recorded the cassette {row[10]}, not this one, {digest}"
        )
    try:
        header = RunHeader(
            run_id=row[0],
            scenario_id=row[1],
            scenario_digest=row[2],
            config=row[3],
            contract=row[4],
            seed=row[5],
            topology=row[6],
            params=row[7],
            parent_run_id=row[11],
            branch_point=row[12],
        )
    except ValidationError as error:
        raise PlaybackError(f"the original run's record does not read as a run: {error}") from None
    if header != cassette.run:
        fields = sorted(
            field
            for field in RunHeader.model_fields
            if getattr(header, field) != getattr(cassette.run, field)
        )
        raise PlaybackError(f"the original run differs from the cassette's in {', '.join(fields)}")
    if (row[8], row[9]) != (cassette.model.adapter, cassette.model.name):
        raise PlaybackError(f"the original run's model is {row[8]} {row[9]}, not the cassette's")


def _check_fresh(dsn: str) -> None:
    """Refuse a replay database that is not migrated or already holds a run."""
    try:
        with _viewer(dsn) as conn:
            row = conn.execute("SELECT count(*) FROM harness.run").fetchone()
    except psycopg.errors.UndefinedTable:
        raise PlaybackError("the replay's database is not migrated") from None
    if row is None or row[0] != 0:
        raise PlaybackError("the replay's database already holds a run; give it a fresh one")
