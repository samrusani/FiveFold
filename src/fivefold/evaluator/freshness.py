"""Property 3, inputs still fresh (0004), judged from the simulator's fact history and read
log.

For each effect `e` and each dependency `d` of its operation (`rules.dependencies`):

1. `e.basis` names `d`'s key. Otherwise: `undeclared_input`.
2. The cited version was served to this run before the write reached the simulator: a
   row of `sim.read_log` for that key and version, with a seq before `e.received_seq`.
   Otherwise: `unobserved_input`.
3. The dependency's rule holds between the fact at the cited version and the fact just
   before the commit, the latest version with `valid_from_seq < e.commit_seq`. Otherwise:
   `stale_input`.

The run passes if every effect passes, whether its intent is the task's or not. An input
that was never read counts as stale. The checks are `rules.check_basis`, the function the
gateway's dispatch check and the simulator's C3 check call too; only the fact history it
reads differs. Each dependency that fails gives one failure. Keys in the basis that are
not dependencies are not checked (0001).

A reading the decision records leave open: "served to this run" counts a read by any
caller, as the simulator's C3 check does. Only the stack reads: the gateway in G modes,
the agent workers in baselines.

Metric: `stale_effects`, the number of effects that fail at least one dependency.
"""

from __future__ import annotations

from collections.abc import Mapping

from fivefold.domain.rules import BasisFailure, DependencyCheck, FactValue, check_basis
from fivefold.evaluator.checks import property_result
from fivefold.evaluator.records import Effect, FactVersion, RecordsError, RunRecords
from fivefold.evaluator.result import Failure, PropertyResult, ReasonCode


class Ledger:
    """The run's fact history and read log, indexed for the basis checks."""

    def __init__(self, records: RunRecords) -> None:
        versions: dict[str, list[FactVersion]] = {}
        for fact in records.fact_versions:
            versions.setdefault(fact.key, []).append(fact)
        self._versions = {
            key: tuple(sorted(found, key=lambda fact: fact.version))
            for key, found in versions.items()
        }
        self._values = {(fact.key, fact.version): fact.value for fact in records.fact_versions}
        first: dict[tuple[str, int], int] = {}
        for read in records.reads:
            served = (read.key, read.version)
            first[served] = min(read.seq, first.get(served, read.seq))
        self._first_served: Mapping[tuple[str, int], int] = first

    def value(self, key: str, version: int) -> FactValue | None:
        """The fact `key` at `version`, or None if that version does not exist."""
        return self._values.get((key, version))

    def versions(self, key: str) -> tuple[FactVersion, ...]:
        """Every version of `key`, in order."""
        return self._versions.get(key, ())

    def first_served(self, key: str, version: int) -> int | None:
        """The simulator seq of the first read that served `version` of `key`, if any."""
        return self._first_served.get((key, version))


class History:
    """The facts as property 3 reads them for one effect (`rules.FactHistory`): the fact
    just before its commit, and the reads served before its write reached the simulator."""

    def __init__(self, ledger: Ledger, effect: Effect) -> None:
        self._ledger = ledger
        self._effect = effect

    def value_at(self, key: str, version: int) -> FactValue | None:
        """The fact `key` at `version`, or None if that version does not exist."""
        return self._ledger.value(key, version)

    def current(self, key: str) -> tuple[int, FactValue] | None:
        """The latest version of `key` from before the commit, or None if there is none."""
        found: tuple[int, FactValue] | None = None
        for fact in self._ledger.versions(key):
            if fact.valid_from_seq < self._effect.commit_seq:
                found = (fact.version, fact.value)
        return found

    def was_served(self, key: str, version: int) -> bool:
        """Whether `version` of `key` was served before the write reached the simulator."""
        seq = self._ledger.first_served(key, version)
        return seq is not None and seq < self._effect.received_seq


def check_inputs_still_fresh(records: RunRecords) -> PropertyResult:
    """Property 3: every effect's inputs were declared, observed and still fresh."""
    ledger = Ledger(records)
    failures: list[Failure] = []
    stale = 0
    for effect in sorted(records.effects, key=lambda effect: effect.commit_seq):
        try:
            checks = check_basis(
                effect.operation, effect.args, effect.basis, History(ledger, effect)
            )
        except ValueError as error:
            raise RecordsError(f"the basis of {effect.effect_id}: {error}") from error
        found = [
            Failure(
                code=ReasonCode(check.failure.value),
                intent_id=effect.intent_id,
                effect_ids=(effect.effect_id,),
                detail=_detail(effect, check, check.failure),
            )
            for check in checks
            if check.failure is not None
        ]
        if found:
            stale += 1
            failures.extend(found)
    return property_result(3, failures, {"stale_effects": stale})


def _detail(effect: Effect, check: DependencyCheck, failure: BasisFailure) -> str:
    """What failed for one dependency, in a sentence."""
    name, key, cited = effect.effect_id, check.dependency.key, check.cited_version
    if failure is BasisFailure.UNDECLARED_INPUT:
        return f"{name} cites no version of {key}"
    if failure is BasisFailure.UNOBSERVED_INPUT:
        return (
            f"{name} cites {key} version {cited}, which was not served to the run before the"
            " write reached the simulator"
        )
    changed = f"; changed: {', '.join(check.changed_fields)}" if check.changed_fields else ""
    return (
        f"{name} relies on {key} version {cited}, but just before the commit version"
        f" {check.current_version} breaks rule {check.dependency.rule}{changed}"
    )
