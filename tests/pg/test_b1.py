"""B1's accepted outcomes and record-by-record determinism (0018, 0009)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest

from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.cells import Cell, cell_plan, cell_scenario, load_expected
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import InprocRun, RunSpec
from fivefold.harness.temporal_worker import TemporalHost
from fivefold.recorder.cassette import sha256
from fivefold.recorder.playback import RECORDS, compare, records

pytestmark = pytest.mark.pg
EXPECTED = load_expected()
CELLS = tuple(cell for cell in EXPECTED.cells if cell.config == "B1")
# CI always runs every cell, plus both host pauses for these retry/concurrency paths.
DETERMINISM_CELLS = {
    "F2-C2-B1",
    "F5-beyond_deadline-C3-B1",
    "F9-same_args-C2-B1",
    "F11-after_tool_call-C2-B1",
}


@pytest.fixture(scope="module")
def temporal_host() -> Iterator[TemporalHost]:
    with TemporalHost() as host:
        yield host


def test_b1_has_the_same_72_fault_plans_as_b0() -> None:
    def plans(config: str) -> dict[str, list[dict[str, object]]]:
        return {
            cell.name.rsplit("-", 1)[0]: cell_plan(cell)
            for cell in EXPECTED.cells
            if cell.config == config
        }

    assert len(CELLS) == 72
    assert plans("B1") == plans("B0")
    assert {cell.name for cell in CELLS} >= DETERMINISM_CELLS


async def pause_activity(treatment: str, activity_id: str, attempt: int) -> None:
    if activity_id not in ("model-1", "tool-5") and attempt == 1:
        return
    if treatment == "blocked":
        threading.Event().wait(0.150)
    else:
        await asyncio.sleep(0.150)


@pytest.mark.parametrize("cell", CELLS, ids=lambda cell: cell.name)
def test_b1_cell(
    run_database: Callable[[str], str], cell: Cell, temporal_host: TemporalHost
) -> None:
    scenario = cell_scenario(cell, load_scenario_file(DEV_001))
    plan = cell_plan(cell)
    spec = RunSpec(scenario, "B1", cell.contract, EXPECTED.seed, plan=plan)
    if os.environ.get("FIVEFOLD_B1_REPEATS") == "20":
        treatments = ["normal"] * 10 + ["async"] * 5 + ["blocked"] * 5
    elif cell.name in DETERMINISM_CELLS:
        treatments = ["normal", "async", "blocked"]
    else:
        treatments = ["normal"]

    async def before_activity(activity_id: str, attempt: int) -> None:
        await pause_activity(treatment, activity_id, attempt)

    first: dict[str, list[str]] | None = None
    first_digests: dict[str, str] | None = None
    for trial, treatment in enumerate(treatments):
        dsn = run_database(f"b1_{cell.name.lower().replace('-', '_')}_{trial}")
        temporal_host.before_activity = None if treatment == "normal" else before_activity
        try:
            result = asyncio.run(InprocRun(spec, dsn, temporal_host=temporal_host).execute())
        finally:
            temporal_host.before_activity = None

        with psycopg.connect(dsn) as conn:
            fired = set(conn.execute("SELECT fault, variant, point FROM harness.fault_log"))
        for entry in plan:
            planned = (entry["fault"], entry.get("variant"), entry["at"]["point"])
            assert planned in fired, f"{cell.name}: {planned} never fired"
        evaluation = result.evaluation
        assert evaluation is not None
        assert tuple(evaluation.evaluated) == EXPECTED.evaluated
        failing = tuple(row.property for row in evaluation.properties if row.verdict == "fail")
        assert (str(evaluation.verdict), failing) == (cell.verdict, cell.failing)
        trace = records(dsn)
        digests = {name: sha256("\n".join(rows)) for name, rows in trace.items()}
        assert set(digests) == {name for name, _ in RECORDS}
        assert trace["message log"] and len(trace["evaluation"]) == 1
        if first is None:
            first, first_digests = trace, digests
        else:
            assert compare(first, trace) == ()
            assert digests == first_digests

    if target := os.environ.get("FIVEFOLD_B1_DIGEST_LOG"):
        with Path(target).open("a", encoding="utf-8") as output:
            row = {"cell": cell.name, "runs": len(treatments), "digests": first_digests}
            output.write(json.dumps(row, sort_keys=True) + "\n")
