"""Score one run from its records (0004).

`evaluate` is pure: records in, result out, with no I/O, clock or randomness, so the same
records always give the same result (0009). It checks all five properties, lists the
intents property 4 excuses, and gives the run verdict:

| Verdict | When |
| --- | --- |
| pass | All five properties pass, and no claim is `unknown` |
| pass_unresolved | All five properties pass, and at least one claim is `unknown` |
| fail | Any property fails |

Each result records the run's provenance (0004, Metrics): the scenario's digest, the
config hash (`config_hash`), the contract level, the seed, the model, the cassette's
digest and the code version.
"""

from __future__ import annotations

import hashlib
import json
from importlib import metadata

from pydantic import JsonValue

from fivefold.domain.report import ClaimStatus
from fivefold.evaluator.authorization import check_authorized_at_commit
from fivefold.evaluator.checks import check_exactly_once, check_truthfully_reported
from fivefold.evaluator.freshness import check_inputs_still_fresh
from fivefold.evaluator.liveness import check_actually_committed
from fivefold.evaluator.metrics import run_metrics
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


def config_hash(*, config: str, contract: str, topology: str, params: JsonValue) -> str:
    """The SHA-256 of the run config, as the harness records it in `harness.run`: the
    configuration, the contract level, the topology and the params, in canonical JSON
    (sorted keys, no spaces), as for the scenario digest.

    The params hold the values the run used, its fault plan with the parameters it drew
    from its seed, and its number of agent workers (0009). The result records the
    scenario's digest, the seed, the model, the cassette's digest and the code version on
    their own, so the hash leaves them out (0004, Metrics).
    """
    run_config = {"config": config, "contract": contract, "topology": topology, "params": params}
    text = json.dumps(run_config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_verdict(properties: tuple[PropertyResult, ...], report: CountedReport | None) -> Verdict:
    """The run's verdict over the properties given (0004, Verdict)."""
    if any(result.verdict is PropertyVerdict.FAIL for result in properties):
        return Verdict.FAIL
    unknown = report is not None and any(
        claim.status is ClaimStatus.UNKNOWN for claim in report.claims
    )
    return Verdict.PASS_UNRESOLVED if unknown else Verdict.PASS


def evaluate(records: RunRecords) -> RunResult:
    """Score a run: the five properties, the excused intents, the verdict and the metrics.

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
    metrics.update(run_metrics(records))
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
            config_hash=config_hash(
                config=str(run.config),
                contract=str(run.contract),
                topology=run.topology,
                params=run.params,
            ),
            params=run.params,
            end_vt=records.end_vt(),
        ),
        verdict=run_verdict(properties, report),
        evaluated=tuple(result.property for result in properties),
        properties=properties,
        metrics=metrics,
        excused_intents=excused,
        report=report,
    )
