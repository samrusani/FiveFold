"""Check B2's approved outcomes and its determinism under host pauses (0020)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

import psycopg
import pytest

from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.baseline_worker import Delivery, Reply, Request
from fivefold.harness.cells import Cell, cell_plan, cell_scenario, load_expected
from fivefold.harness.dbos_worker import DBOSWorkers
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import InprocRun, RunSpec
from fivefold.paths import DEV_MEMORY_PO, EXPECTED_F8
from fivefold.recorder.cassette import sha256
from fivefold.recorder.playback import RECORDS, compare, records

pytestmark = pytest.mark.pg
EXPECTED = load_expected()
CELLS = tuple(cell for cell in EXPECTED.cells if cell.config == "B2")
F8 = load_expected(EXPECTED_F8)
F8_CELLS = tuple(cell for cell in F8.cells if cell.config == "B2")
DETERMINISM_CELLS = {
    "F2-C2-B2",
    "F5-beyond_deadline-C3-B2",
    "F9-same_args-C2-B2",
    "F11-after_tool_call-C2-B2",
}


def test_b2_has_the_same_72_fault_plans_as_b0() -> None:
    def plans(config: str) -> dict[str, list[dict[str, object]]]:
        return {
            cell.name.rsplit("-", 1)[0]: cell_plan(cell)
            for cell in EXPECTED.cells
            if cell.config == config
        }

    assert version("dbos") == "3.6.0"
    assert len(CELLS) == 72
    assert plans("B2") == plans("B0")
    assert {cell.name for cell in CELLS} >= DETERMINISM_CELLS


def cell_spec(cell: Cell) -> RunSpec:
    memory = cell.fault == "F8"
    scenario = cell_scenario(cell, load_scenario_file(DEV_MEMORY_PO if memory else DEV_001))
    expected = F8 if memory else EXPECTED
    return RunSpec(scenario, "B2", cell.contract, expected.seed, plan=cell_plan(cell))


@pytest.mark.parametrize("cell", CELLS + F8_CELLS, ids=lambda cell: cell.name)
def test_b2_cell(
    run_database: Callable[[str], str],
    admin_dsn: str,
    cell: Cell,
    monkeypatch: pytest.MonkeyPatch,
    assert_f8_records: Callable[[str, str | None], None],
    baseline_repeats: Callable[[str, str, str], list[str] | None],
) -> None:
    memory = cell.fault == "F8"
    spec = cell_spec(cell)
    treatments = baseline_repeats("FIVEFOLD_B2_REPEATS", "delay", "stop")
    if treatments is None and cell.name in DETERMINISM_CELLS:
        treatments = ["normal", "delay", "stop"]
    if treatments is None:
        treatments = ["normal"] * (2 if memory else 1)

    first: dict[str, list[str]] | None = None
    first_digests: dict[str, str] | None = None
    original = DBOSWorkers._next

    async def paused(
        workers: DBOSWorkers, worker: str, reply: Reply | None, *, start: bool
    ) -> Delivery:
        response = await original(workers, worker, reply, start=start)
        if isinstance(response, Request) and (
            start or response.attempt > 1 or (reply is not None and reply[2] is not None)
        ):
            if treatment == "stop":
                child = workers.children[worker]
                child.send_signal(signal.SIGSTOP)
                try:
                    await asyncio.sleep(0.150)
                finally:
                    child.send_signal(signal.SIGCONT)
            else:
                await asyncio.sleep(0.150)
        return response

    for trial, treatment in enumerate(treatments):
        dsn = run_database(f"b2_{cell.name.lower().replace('-', '_')}_{trial}")
        with monkeypatch.context() as patch:
            if treatment != "normal":
                patch.setattr(DBOSWorkers, "_next", paused)
            result = asyncio.run(InprocRun(spec, dsn, admin_dsn=admin_dsn).execute())
        with psycopg.connect(dsn) as conn:
            fired = set(conn.execute("SELECT fault, variant, point FROM harness.fault_log"))
        for entry in cell_plan(cell):
            planned = (entry["fault"], entry.get("variant"), entry["at"]["point"])
            assert planned in fired, f"{cell.name}: {planned} never fired"
        if memory:
            assert_f8_records(dsn, cell.variant)
        evaluation = result.evaluation
        assert evaluation is not None
        assert tuple(evaluation.evaluated) == (F8 if memory else EXPECTED).evaluated
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

    if target := os.environ.get("FIVEFOLD_B2_DIGEST_LOG"):
        with Path(target).open("a", encoding="utf-8") as output:
            row = {"cell": cell.name, "runs": len(treatments), "digests": first_digests}
            output.write(json.dumps(row, sort_keys=True) + "\n")
