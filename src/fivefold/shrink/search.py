"""Search an ordered, materialized fault plan without changing the run's inputs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from fivefold.evaluator.result import PropertyVerdict, RunResult, Verdict
from fivefold.faults.catalog import F5_BEYOND_DEADLINE, FaultId
from fivefold.faults.plan import PlannedFault, check_run, parse_plan
from fivefold.harness.run import RunSpec
from fivefold.recorder.cassette import canonical_json

Plan = tuple[PlannedFault, ...]


def plan_data(plan: Plan) -> list[dict[str, Any]]:
    return [entry.model_dump(mode="json", exclude_none=True) for entry in plan]


@dataclass(frozen=True)
class Signature:
    verdict: Verdict
    properties: tuple[int, ...]
    failures: tuple[tuple[int, str, str | None], ...]

    @classmethod
    def of(cls, result: RunResult) -> Signature:
        failing = [prop for prop in result.properties if prop.verdict is PropertyVerdict.FAIL]
        failures = {
            (prop.property, str(failure.code), failure.intent_id)
            for prop in failing
            for failure in prop.failures
        }
        return cls(
            result.verdict,
            tuple(sorted(prop.property for prop in failing)),
            tuple(
                sorted(
                    failures,
                    key=lambda item: (item[0], item[1], item[2] is not None, item[2] or ""),
                )
            ),
        )


@dataclass(frozen=True)
class Observation:
    signature: Signature
    digests: dict[str, str]


@dataclass(frozen=True)
class Candidate:
    plan: Plan
    observation: Observation
    reproduces: bool


@dataclass(frozen=True)
class SearchResult:
    plan: Plan
    observation: Observation
    candidates: tuple[Candidate, ...]
    next_candidate: Plan | None

    @property
    def budget_exhausted(self) -> bool:
        return self.next_candidate is not None


class _BudgetExhausted(Exception):
    """The next distinct candidate would exceed a search budget."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan


def minimize(
    spec: RunSpec,
    original: Plan,
    baseline: Observation,
    trial: Callable[[Plan], Observation],
    *,
    max_candidates: int,
    expired: Callable[[], bool],
) -> SearchResult:
    """Both limits stop at the same boundary, just before a cache miss (0022)."""
    seen = {canonical_json(plan_data(original)): baseline}
    history: list[Candidate] = []

    def reproduces(plan: Plan) -> bool:
        key = canonical_json(plan_data(plan))
        if key not in seen:
            if len(history) >= max_candidates or expired():
                raise _BudgetExhausted(plan)
            observed = trial(plan)
            seen[key] = observed
            history.append(Candidate(plan, observed, observed.signature == baseline.signature))
        return seen[key].signature == baseline.signature

    best, granularity = original, 2
    next_candidate = None
    try:
        while best:
            width = (len(best) + granularity - 1) // granularity
            for start in range(0, len(best), width):
                candidate = best[:start] + best[start + width :]
                if reproduces(candidate):
                    best, granularity = candidate, max(2, granularity - 1)
                    break
            else:
                if granularity < len(best):
                    granularity = min(len(best), 2 * granularity)
                    continue
                for candidate in reductions(best, spec):
                    if reproduces(candidate):
                        best, granularity = candidate, 2
                        break
                else:
                    break
    except _BudgetExhausted as stop:
        next_candidate = stop.plan
    return SearchResult(best, seen[canonical_json(plan_data(best))], tuple(history), next_candidate)


def _lower_values(value: int, minimum: int) -> Iterator[int]:
    """Try the minimum, then halve the remaining distance toward the old value."""
    while minimum < value:
        yield minimum
        minimum += max(1, (value - minimum) // 2)


def _entries(entry: PlannedFault, spec: RunSpec) -> Iterator[PlannedFault]:
    for value in _lower_values(entry.at.occurrence, 1):
        yield entry.model_copy(update={"at": entry.at.model_copy(update={"occurrence": value})})
    for name, minimum in sorted(entry.kind.minimums.items()):
        if entry.fault is FaultId.F5 and name == "delay_ms":
            params = spec.scenario.params
            deadline = entry.variant == F5_BEYOND_DEADLINE
            minimum = max(
                minimum, 1 + (params.commit_deadline_ms if deadline else params.dispatch_timeout_ms)
            )
        for value in _lower_values(entry.param(name), minimum):
            yield entry.model_copy(update={"params": {**entry.params, name: value}})


def reductions(plan: Plan, spec: RunSpec) -> Iterator[Plan]:
    """Check run-specific bounds and repeated crash triggers with the original validators."""
    for index, entry in enumerate(plan):
        for lower in _entries(entry, spec):
            candidate = (*plan[:index], lower, *plan[index + 1 :])
            try:
                checked = parse_plan(plan_data(candidate))
                check_run(checked, params=spec.scenario.params, contract=spec.contract)
            except ValueError:
                continue
            yield checked
