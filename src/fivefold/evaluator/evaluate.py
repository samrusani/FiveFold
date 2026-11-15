"""Score one run from its records (0004).

`evaluate` is pure: records in, result out, with no I/O, clock or randomness, so the same
records always give the same result (0009). It checks all five properties, lists the
intents property 4 excuses, works out the recovery latency of each intent a fault
touched (0014), and gives the run verdict:

| Verdict | When |
| --- | --- |
| pass | All five properties pass, and no claim is `unknown` |
| pass_unresolved | All five properties pass, and at least one claim is `unknown` |
| fail | Any property fails |
"""

from __future__ import annotations

from importlib import metadata

from fivefold.domain.report import ClaimStatus
from fivefold.evaluator.authorization import check_authorized_at_commit
from fivefold.evaluator.checks import check_exactly_once, check_truthfully_reported
from fivefold.evaluator.freshness import check_inputs_still_fresh
from fivefold.evaluator.liveness import check_actually_committed
from fivefold.evaluator.metrics import recovery_metrics, run_metrics, token_metrics
from fivefold.evaluator.records import RunRecords
from fivefold.evaluator.recovery import recovery_entries
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
    """The run's verdict over the properties given (0004, Verdict)."""
    if any(result.verdict is PropertyVerdict.FAIL for result in properties):
        return Verdict.FAIL
    unknown = report is not None and any(
        claim.status is ClaimStatus.UNKNOWN for claim in report.claims
    )
    return Verdict.PASS_UNRESOLVED if unknown else Verdict.PASS


def evaluate(records: RunRecords) -> RunResult:
    """Score a run: the five properties, the excused intents, the verdict, the recovery
    entries and the metrics.

    Raises `RecordsError` when the records break a rule the checks rely on.
    """
    records.check_scenario()
    exactly_once = check_exactly_once(records)
    authorized_at_commit = check_authorized_at_commit(records)
    inputs_still_fresh = check_inputs_still_fresh(records)
    actually_committed, excused = check_actually_committed(records)
    truthfully_reported, report = check_truthfully_reported(records)
    properties = (
        exactly_once,
        authorized_at_commit,
        inputs_still_fresh,
        actually_committed,
        truthfully_reported,
    )
    metrics: dict[str, int] = {}
    for result in properties:
        metrics.update(result.metrics)
    recovery = recovery_entries(records)
    metrics.update(run_metrics(records))
    metrics.update(recovery_metrics(recovery))
    metrics.update(token_metrics(records))
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
            end_vt=records.end_vt(),
        ),
        verdict=run_verdict(properties, report),
        evaluated=tuple(result.property for result in properties),
        properties=properties,
        metrics=metrics,
        excused_intents=excused,
        report=report,
        recovery=recovery,
    )
