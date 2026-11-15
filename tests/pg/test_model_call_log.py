"""The harness's model-call log in whole runs (0004, 0009).

Every model call of every worker gets a row once it is answered, numbered in the run in
the order of the run's cassette. Scripted replies report no usage, so their tokens are
null, and each call takes exactly `think_ms` of virtual time. The evaluator's role can
read the rows, as it will for record 0004's token and cost metrics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.demo import DEV_001, demo_1_spec
from fivefold.harness.run import Config, InprocRun, RunSpec
from fivefold.recorder.playback import record

pytestmark = pytest.mark.pg

SCENARIO = load_scenario_file(DEV_001)
CALLS: LiteralString = (
    "SELECT call_no, worker, asked_vt, answered_vt, model, input_tokens, output_tokens,"
    " refused, wall_ms FROM harness.model_call ORDER BY call_no"
)


def calls(dsn: str) -> list[tuple[Any, ...]]:
    """The run's model calls, read as the evaluator's role."""
    with psycopg.connect(make_conninfo(dsn, user="ff_eval")) as conn:
        return conn.execute(CALLS).fetchall()


@pytest.mark.parametrize("config", ["B0", "G1", "G2"])
def test_a_scripted_run_logs_each_model_call_with_no_tokens(
    run_database: Callable[[str], str], config: Config
) -> None:
    """Demo 1's seven calls: each takes `think_ms`, names the scripted model and reports no
    tokens. The calls after F11's kill come from the resumed worker, under its name."""
    dsn = run_database(f"calls_{config.lower()}")
    asyncio.run(InprocRun(demo_1_spec(config, 7), dsn).execute())
    logged = calls(dsn)
    assert [row[0] for row in logged] == list(range(1, 8))
    assert {(row[1], row[4], row[5], row[6], row[7]) for row in logged} == {
        ("agent-1", "scripted", None, None, None)
    }
    assert all(row[3] - row[2] == SCENARIO.params.think_ms and row[8] >= 0 for row in logged)
    assert [row[2] for row in logged] == sorted(row[2] for row in logged)


def test_two_workers_calls_are_numbered_in_the_order_of_the_cassette(
    run_database: Callable[[str], str],
) -> None:
    """F9 starts a second worker, whose calls interleave with the first's: the log numbers
    them in the order the cassette keeps them."""
    f9 = {
        "fault": "F9",
        "variant": "different_args",
        "at": {"point": "agent.step", "match": {"worker": "agent-1"}, "occurrence": 2},
    }
    dsn = run_database("calls_f9")
    _, cassette = record(RunSpec(SCENARIO, "G2", Contract.C2, 7, plan=[f9]), dsn)
    logged = calls(dsn)
    assert [row[1] for row in logged] == [call.worker for call in cassette.calls]
    assert {row[1] for row in logged} == {"agent-1", "agent-2"}
    assert [row[0] for row in logged] == list(range(1, len(cassette.calls) + 1))
