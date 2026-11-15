"""Every cell of `faults/expected.yaml` gets its expected verdict (0006, Expected outcomes).

Each cell runs dev-001 from the committed seed with the cell's fault plan, in a fresh run
database, and the evaluator scores it. The test asserts the verdict and the failing
properties, over the properties the evaluator checks so far.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import psycopg
import pytest

from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.cells import Cell, cell_plan, load_expected
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import InprocRun, RunSpec

pytestmark = pytest.mark.pg

EXPECTED = load_expected()


def test_the_expected_outcomes_name_the_dev_scenario_and_what_is_evaluated() -> None:
    assert EXPECTED.scenario == load_scenario_file(DEV_001).id
    assert EXPECTED.evaluated == (1, 5)
    names = [cell.name for cell in EXPECTED.cells]
    assert len(names) == len(set(names)), "each cell once"


def test_each_fault_but_f1_f10_and_f12_has_a_cell_where_b0_fails_and_g2_passes() -> None:
    """CLAUDE.md, Tests; 0006: the comparison the suite exists to show."""
    faults = {cell.fault for cell in EXPECTED.cells} - {"F1", "F10", "F12"}
    for fault in sorted(faults):
        outcomes = {
            (cell.variant, str(cell.contract), cell.config): cell.verdict
            for cell in EXPECTED.cells
            if cell.fault == fault
        }
        assert any(
            outcomes.get((variant, contract, "B0")) == "fail" and verdict == "pass"
            for (variant, contract, config), verdict in outcomes.items()
            if config == "G2"
        ), fault


@pytest.mark.parametrize("cell", EXPECTED.cells, ids=lambda cell: cell.name)
def test_the_cell_gets_its_expected_verdict(run_database: Callable[[str], str], cell: Cell) -> None:
    dsn = run_database(cell.name.lower().replace("-", "_"))
    plan = cell_plan(cell)
    spec = RunSpec(
        load_scenario_file(DEV_001),
        cell.config,  # type: ignore[arg-type]
        cell.contract,
        EXPECTED.seed,
        plan=plan,
    )
    run = InprocRun(spec, dsn)
    asyncio.run(run.execute())
    with psycopg.connect(dsn) as conn:
        fired = conn.execute("SELECT fault, variant, point FROM harness.fault_log").fetchall()
    for entry in plan:
        planned = (entry["fault"], entry.get("variant"), entry["at"]["point"])
        assert planned in fired, f"{cell.name}: {planned} never fired, so the cell tests nothing"
    evaluation = run.result.evaluation
    assert evaluation is not None
    assert tuple(evaluation.evaluated) == EXPECTED.evaluated
    failing = tuple(
        result.property for result in evaluation.properties if str(result.verdict) == "fail"
    )
    assert (str(evaluation.verdict), failing) == (cell.verdict, cell.failing)
