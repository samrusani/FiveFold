"""The shrinker tries legal reductions in a fixed order (0022)."""

import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.evaluator.result import Verdict
from fivefold.faults.plan import parse_plan
from fivefold.harness.cells import cell_plan, load_expected
from fivefold.harness.run import RunSpec
from fivefold.paths import DEV_001
from fivefold.shrink.search import Observation, Plan, Signature, minimize, reductions

SCENARIO = load_scenario_file(DEV_001)
SPEC = RunSpec(SCENARIO, "B0", Contract.C2, 7)


@pytest.mark.parametrize(
    ("variant", "bound"), [("in_contract", "timeout"), ("beyond_deadline", "deadline")]
)
def test_f5_reductions_stay_in_the_original_timing_band(variant: str, bound: str) -> None:
    cell = next(c for c in load_expected().cells if c.name == f"F5-{variant}-C2-B0")
    original = parse_plan(cell_plan(cell))
    lower = [plan[0].param("delay_ms") for plan in reductions(original, SPEC)]
    params = SCENARIO.params
    minimum = 1 + (params.dispatch_timeout_ms if bound == "timeout" else params.commit_deadline_ms)
    assert lower[0] == minimum
    assert all(minimum <= ms < original[0].param("delay_ms") for ms in lower)
    if variant == "in_contract":
        assert max(lower) < params.commit_deadline_ms


def test_occurrence_reduction_uses_the_catalog_and_never_duplicates_a_crash_trigger() -> None:
    cell = next(c for c in load_expected().cells if c.name == "F11-after_tool_call-C2-B0")
    (first,) = cell_plan(cell)
    first["params"] = {"restart_delay_ms": 0}
    second = {**first, "at": {**first["at"], "occurrence": 3}}
    choices = tuple(reductions(parse_plan([first, second]), SPEC))
    assert choices[0][1].at.occurrence == 2
    assert all(plan[0].at.occurrence != plan[1].at.occurrence for plan in choices)


def test_lower_parameter_restarts_deletion_in_fixed_order() -> None:
    cells = {cell.name: cell for cell in load_expected().cells}
    raw = cell_plan(cells["F5-beyond_deadline-C2-B0"]) + cell_plan(cells["F7-late_price_up-C2-B0"])
    source = parse_plan(raw)
    same = Observation(Signature(Verdict.FAIL, (1,), ((1, "duplicate_effects", "REQ-1"),)), {})
    other = Observation(Signature(Verdict.PASS, (), ()), {})
    called: list[Plan] = []

    def trial(plan: Plan) -> Observation:
        called.append(plan)
        if (
            plan
            and plan[0].fault == "F5"
            and plan[0].param("delay_ms") == SCENARIO.params.commit_deadline_ms + 1
        ):
            return same
        return other

    result = minimize(SPEC, source, same, trial, max_candidates=100, expired=lambda: False)
    assert [entry.fault for entry in result.plan] == ["F5"]
    assert len(called) == len(result.candidates) == 5
    assert not result.budget_exhausted
