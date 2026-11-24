"""F8's in-process matrix and its repeatability (0023)."""

import asyncio
from collections.abc import Callable
from itertools import product
from typing import cast

import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.cells import Cell, cell_plan, load_expected
from fivefold.harness.run import CONFIGS, Config, InprocRun, RunSpec
from fivefold.paths import DEV_MEMORY_PO, EXPECTED_F8
from fivefold.recorder.cassette import sha256
from fivefold.recorder.playback import RECORDS, compare, records

pytestmark = pytest.mark.pg
EXPECTED = load_expected(EXPECTED_F8)
CELLS = tuple(cell for cell in EXPECTED.cells if cell.config not in ("B1", "B2"))
VARIANTS = ("early_relevant", "early_harmless", "late_relevant", "late_harmless")


def test_f8_outcomes_match_record_0023() -> None:
    assert EXPECTED.scenario == load_scenario_file(DEV_MEMORY_PO).id
    assert len(EXPECTED.cells) == 80
    expected: dict[tuple[str, Contract, str | None], tuple[str, tuple[int, ...]]] = {}
    for config, contract, variant in product(CONFIGS, Contract, VARIANTS):
        stale = (
            contract is not Contract.C3
            and variant.endswith("_relevant")
            and (config != "G2" or variant.startswith("late_"))
        )
        expected[config, contract, variant] = ("fail", (3,)) if stale else ("pass", ())
    assert {
        (cell.config, cell.contract, cell.variant): (cell.verdict, cell.failing)
        for cell in EXPECTED.cells
    } == expected


@pytest.mark.parametrize("cell", CELLS, ids=lambda cell: cell.name)
def test_f8_cell_twice(
    run_database: Callable[[str], str],
    cell: Cell,
    assert_f8_records: Callable[[str, str | None], None],
) -> None:
    scenario = load_scenario_file(DEV_MEMORY_PO)
    spec = RunSpec(
        scenario, cast(Config, cell.config), cell.contract, EXPECTED.seed, plan=cell_plan(cell)
    )
    first = None
    for trial in range(2):
        dsn = run_database(f"{cell.name.lower().replace('-', '_')}_{trial}")
        result = asyncio.run(InprocRun(spec, dsn).execute()).evaluation
        assert result is not None
        assert tuple(result.evaluated) == EXPECTED.evaluated
        failing = tuple(p.property for p in result.properties if p.verdict == "fail")
        assert (result.verdict, failing) == (cell.verdict, cell.failing)
        assert {str(failure.code) for p in result.properties for failure in p.failures} == (
            {"stale_input"} if failing else set()
        )
        assert_f8_records(dsn, cell.variant)
        trace = records(dsn)
        digests = {name: sha256("\n".join(rows)) for name, rows in trace.items()}
        assert set(digests) == {name for name, _ in RECORDS}
        if first is None:
            first = trace, digests
        else:
            assert compare(first[0], trace) == ()
            assert digests == first[1]
