"""Score one run from its records (0004).

`evaluate` is pure: records in, result out, with no I/O, clock or randomness, so the same
records always give the same result (0009). This evaluator checks properties 1 and 5
(milestone M1). Properties 2 to 4 are marked `not_evaluated`, and the run verdict covers
only the properties evaluated:

| Verdict | When |
| --- | --- |
| pass | Every property evaluated passes, and no claim is `unknown` |
| pass_unresolved | Every property evaluated passes, and at least one claim is `unknown` |
| fail | A property evaluated fails |
"""

from __future__ import annotations

from importlib import metadata

from fivefold.domain.report import ClaimStatus
from fivefold.evaluator.checks import check_exactly_once, check_truthfully_reported, not_evaluated
from fivefold.evaluator.records import RunRecords
from fivefold.evaluator.result import (
    CountedReport,
    PropertyResult,
    PropertyVerdict,
    RunResult,
    RunSummary,
    Verdict,
)

PACKAGE = "fivefold"


def code_version() -> str:
    """The installed version of the `fivefold` package, or `unknown` if it is not installed."""
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return "unknown"


def run_verdict(properties: tuple[PropertyResult, ...], report: CountedReport | None) -> Verdict:
    """The run's verdict over the properties evaluated (0004, Verdict)."""
    if any(result.verdict is PropertyVerdict.FAIL for result in properties):
        return Verdict.FAIL
    unknown = report is not None and any(
        claim.status is ClaimStatus.UNKNOWN for claim in report.claims
    )
    return Verdict.PASS_UNRESOLVED if unknown else Verdict.PASS


def evaluate(records: RunRecords) -> RunResult:
    """Score a run: properties 1 and 5, the run verdict and the metrics.

    Raises `RecordsError` when the records break a rule the checks rely on.
    """
    records.check_scenario()
    exactly_once = check_exactly_once(records)
    truthfully_reported, report = check_truthfully_reported(records)
    properties = (
        exactly_once,
        not_evaluated(2),
        not_evaluated(3),
        not_evaluated(4),
        truthfully_reported,
    )
    evaluated = tuple(
        result.property
        for result in properties
        if result.verdict is not PropertyVerdict.NOT_EVALUATED
    )
    metrics: dict[str, int] = {}
    for result in properties:
        metrics.update(result.metrics)
    run = records.run
    return RunResult(
        code_version=code_version(),
        run=RunSummary(
            run_id=run.run_id,
            scenario_id=run.scenario_id,
            scenario_digest=run.scenario_digest,
            config=str(run.config),
            contract=str(run.contract),
            seed=run.seed,
            topology=run.topology,
            model_adapter=run.model_adapter,
            model=run.model,
            cassette_digest=run.cassette_digest,
            parent_run_id=run.parent_run_id,
            branch_point=run.branch_point,
            params=run.params,
        ),
        verdict=run_verdict(properties, report),
        evaluated=evaluated,
        properties=properties,
        metrics=metrics,
        excused_intents=None,
        report=report,
    )
