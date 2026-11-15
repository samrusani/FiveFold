"""Every cell of `faults/expected.yaml` gets its expected verdict (0006, Expected outcomes).

Each cell runs dev-001 from the committed seed with the cell's fault plan, in a fresh run
database, and the evaluator scores it. The test asserts the verdict and the failing
properties, over all five properties. Record 0006 fixes some outcomes before any run, and
the committed cells must agree with them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.cells import Cell, cell_plan, load_expected
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import InprocRun, RunSpec

pytestmark = pytest.mark.pg

EXPECTED = load_expected()

BELOW_C3 = (Contract.C0, Contract.C1, Contract.C2)

G2_LIMITS: dict[tuple[str, str | None, Contract], tuple[str, tuple[int, ...]]] = {
    ("F1", "b", Contract.C0): ("fail", (4,)),
    ("F12", "gw_sim", Contract.C0): ("fail", (4,)),
    **{("F4", "gw_sim", level): ("fail", (1, 2, 3)) for level in (Contract.C0, Contract.C1)},
    **{("F6", "late", level): ("fail", (2,)) for level in BELOW_C3},
    **{("F7", "late_price_up", level): ("fail", (3,)) for level in BELOW_C3},
    ("F2", None, Contract.C0): ("pass_unresolved", ()),
    ("F3", None, Contract.C0): ("pass_unresolved", ()),
}
"""0006's documented G2 limits, by fault, variant and contract level: the cells that fail or
stay unresolved by design. Every other G2 cell is expected to pass."""

B0_FAILS = frozenset({"F2", "F3", "F4", "F5", "F6", "F9", "F11"})
"""0006: at C0 to C2, B0 fails the faults that make it resend, and F6 and F7 `price_up`,
because it checks nothing before writing."""

B0_PASSES = frozenset({"F1", "F10", "F12"})
"""0006: B0 should pass F1, F7 `harmless`, F10 and F12."""

HOP_VARIANTS = frozenset({"F4", "F12"})
"""Faults whose cells name a hop. B0's hop, `agent_sim`, is the B0 equivalent of either
G-mode hop (0006), so their B0 and G2 cells compare by contract level alone."""


def test_the_expected_outcomes_name_the_dev_scenario_and_what_is_evaluated() -> None:
    assert EXPECTED.scenario == load_scenario_file(DEV_001).id
    assert EXPECTED.evaluated == (1, 2, 3, 4, 5)
    names = [cell.name for cell in EXPECTED.cells]
    assert len(names) == len(set(names)), "each cell once"


def test_each_fault_but_f1_f10_and_f12_has_a_cell_where_b0_fails_and_g2_passes() -> None:
    """CLAUDE.md, Tests; 0006: the comparison the suite exists to show."""
    faults = {cell.fault for cell in EXPECTED.cells} - {"F1", "F10", "F12"}

    def where(cell: Cell) -> tuple[str | None, str]:
        return (None if cell.fault in HOP_VARIANTS else cell.variant, str(cell.contract))

    for fault in sorted(faults):
        cells = [cell for cell in EXPECTED.cells if cell.fault == fault]
        b0_fails = {where(c) for c in cells if c.config == "B0" and c.verdict == "fail"}
        g2_passes = {where(c) for c in cells if c.config == "G2" and c.verdict == "pass"}
        assert b0_fails & g2_passes, fault


def test_the_cells_agree_with_the_outcomes_record_0006_fixes() -> None:
    """0006 fixes these outcomes before any run. A run that contradicts one is revisited by
    decision record; nobody edits the expectation to match."""
    for cell in EXPECTED.cells:
        outcome = (cell.verdict, cell.failing)
        if cell.config == "G2":
            expected = G2_LIMITS.get((cell.fault, cell.variant, cell.contract), ("pass", ()))
            assert outcome == expected, cell.name
        if cell.config == "B0" and cell.contract is not Contract.C3:
            price_up = cell.fault == "F7" and (cell.variant or "").endswith("price_up")
            if cell.fault in B0_FAILS or price_up:
                assert cell.verdict == "fail", cell.name
            elif cell.fault in B0_PASSES or cell.fault == "F7":
                assert outcome == ("pass", ()), cell.name


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
