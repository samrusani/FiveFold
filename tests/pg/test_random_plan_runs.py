"""Whole runs with a random fault plan (0006, Plans; 0009, Determinism contract).

A run draws its random plan from its seed, fires the list it drew, and records that list
in `harness.run` as it records a fixed plan. So the same seed gives the same run, byte for
byte, the recorded list run as a fixed plan gives that run again, and playback and the
report see the faults the run had.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario_file
from fivefold.evaluator.report import plan_label, planned_faults
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import InprocRun, RunSpec, run_plan
from fivefold.recorder.cassette import sha256
from fivefold.recorder.playback import compare, play, record, records

pytestmark = pytest.mark.pg

DEV = load_scenario_file(DEV_001)

PLAN: dict[str, Any] = {"random": {"from": ["F2", "F4", "F11", "F12"], "max": 3}}

SEED = 12
"""A seed whose plan holds three faults, all of which fire in G2 at C2."""

DRAWN: list[dict[str, Any]] = [
    {
        "fault": "F11",
        "variant": "after_tool_call",
        "at": {
            "point": "agent.after_tool_call",
            "match": {"tool": "submit_purchase_order", "intent_id": "REQ-1"},
            "occurrence": 1,
        },
        "params": {"restart_delay_ms": 7397},
    },
    {
        "fault": "F2",
        "variant": None,
        "at": {
            "point": "net.gw_sim.answer",
            "match": {"api": "submit_purchase_order", "answer": "committed", "intent_id": "REQ-1"},
            "occurrence": 1,
        },
        "params": {},
    },
    {
        "fault": "F12",
        "variant": None,
        "at": {
            "point": "net.gw_sim.request",
            "match": {"api": "submit_purchase_order", "intent_id": "REQ-1"},
            "occurrence": 1,
        },
        "params": {},
    },
]
"""The plan seed 12 draws: demo 1's two faults, with the agent's restart delay drawn, and
F12, which drops the gateway's first request, so F2 drops the committed answer to its
retry."""


def execute(spec: RunSpec, dsn: str) -> InprocRun:
    run = InprocRun(spec, dsn)
    asyncio.run(run.execute())
    return run


def recorded_plan(dsn: str) -> Any:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT params FROM harness.run").fetchone()
    assert row is not None
    return row[0]["plan"]


def fired(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute("SELECT DISTINCT fault FROM harness.fault_log ORDER BY fault")
        return [str(row[0]) for row in rows]


def test_a_seed_gives_one_run_and_the_plan_it_records_gives_it_again(
    run_database: Callable[[str], str],
) -> None:
    """The run fires and records the list its seed draws; a second run from the seed gives
    the same records, and so does the recorded list, given as a fixed plan."""
    spec = RunSpec(DEV, "G2", Contract.C2, SEED, plan=PLAN)
    assert [entry.model_dump(mode="json") for entry in run_plan(spec)] == DRAWN
    first, again = run_database("rnd_first"), run_database("rnd_again")
    run = execute(spec, first)
    execute(spec, again)
    assert recorded_plan(first) == DRAWN, "recorded as a fixed plan is"
    assert fired(first) == ["F11", "F12", "F2"], "each drawn fault fired"
    assert run.result.evaluation is not None
    assert str(run.result.evaluation.verdict) == "pass"
    assert compare(records(first), records(again)) == ()
    digests = {name: sha256("\n".join(rows)) for name, rows in records(first).items()}
    assert digests == {name: sha256("\n".join(rows)) for name, rows in records(again).items()}
    fixed = run_database("rnd_fixed")
    execute(RunSpec(DEV, "G2", Contract.C2, SEED, plan=DRAWN), fixed)
    assert compare(records(first), records(fixed)) == ()


def test_another_seed_draws_another_plan_and_run(run_database: Callable[[str], str]) -> None:
    first, other = run_database("rnd_seed_12"), run_database("rnd_seed_13")
    execute(RunSpec(DEV, "G2", Contract.C2, SEED, plan=PLAN), first)
    execute(RunSpec(DEV, "G2", Contract.C2, SEED + 1, plan=PLAN), other)
    assert recorded_plan(other) != recorded_plan(first)
    assert compare(records(first), records(other)) != ()


def with_random_faults(scenario: Scenario) -> Scenario:
    """`scenario`, with the random plan as its own fault plan."""
    return scenario.model_copy(update={"faults": PLAN})


def test_a_scenarios_random_plan_plays_back_and_reports_its_faults(
    run_database: Callable[[str], str],
) -> None:
    """A scenario can hold the random plan itself. Playback re-runs the plan the run
    recorded, and the report names its faults, not the random plan."""
    scenario = with_random_faults(DEV)
    original, replay = run_database("rnd_rec"), run_database("rnd_play")
    run, cassette = record(RunSpec(scenario, "G2", Contract.C2, SEED), original)
    assert cassette.run.params["plan"] == DRAWN
    playback = play(cassette, scenario, original=original, replay=replay)
    assert playback.differences == ()
    assert playback.verdict == "pass"
    assert recorded_plan(replay) == DRAWN
    evaluation = run.result.evaluation
    assert evaluation is not None
    label = plan_label(planned_faults(evaluation))
    assert label.startswith("F11 after_tool_call at agent.after_tool_call when intent_id=REQ-1")
    assert label.endswith(
        " + F12 at net.gw_sim.request when api=submit_purchase_order, intent_id=REQ-1"
    )
