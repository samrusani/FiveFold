"""The evaluator (0004): property checks, metrics and the result of each run.

It decides pass or fail only from simulator and harness records and the scenario, and
never calls a model or reads a transcript. It imports only `fivefold.domain`: it reads
other components' tables, never their code. It checks all five properties: 1, exactly
once (`checks`); 2, authorized at commit (`authorization`); 3, inputs still fresh
(`freshness`); 4, actually committed (`liveness`); and 5, truthfully reported (`checks`,
with `evidence`).

Typical use, after the run has stopped:

    result = evaluate_run(conninfo_as_ff_eval, load_scenario_file(path))
    text = result_json(result)

`load_run` and `evaluate` split that into reading and pure scoring; `write_result`
stores a result in the `eval` schema. Across runs, `load_results` reads saved result
files, and `runs_csv` and `markdown_report` write a CSV with one row per run and a
Markdown report (`report`; `python -m fivefold.evaluator report`).
"""

from fivefold.evaluator.checks import CLAIM_METRICS
from fivefold.evaluator.evaluate import config_hash, evaluate, run_verdict
from fivefold.evaluator.evidence import (
    IntentKnowledge,
    Knowledge,
    RequestFinding,
    SentWrite,
    Settlement,
    stack_knowledge,
)
from fivefold.evaluator.liveness import facts_without_effects
from fivefold.evaluator.load import load_run
from fivefold.evaluator.metrics import RUN_METRICS
from fivefold.evaluator.records import (
    ActorAction,
    Approval,
    Config,
    Destination,
    Effect,
    FactVersion,
    Fault,
    Message,
    Read,
    RecordsError,
    RunInfo,
    RunRecords,
    SubmittedReport,
)
from fivefold.evaluator.report import (
    CSV_COLUMNS,
    METRICS,
    ReportError,
    load_results,
    markdown_report,
    runs_csv,
)
from fivefold.evaluator.result import (
    ClaimJudgment,
    CountedReport,
    Excuse,
    ExcusedIntent,
    Failure,
    PropertyResult,
    PropertyVerdict,
    ReasonCode,
    RequestOutcome,
    RunResult,
    RunSummary,
    Verdict,
    result_json,
)
from fivefold.evaluator.store import EVALUATOR_ROLE, WrongRole, evaluate_run, write_result

__all__ = [
    "CLAIM_METRICS",
    "CSV_COLUMNS",
    "EVALUATOR_ROLE",
    "METRICS",
    "RUN_METRICS",
    "ActorAction",
    "Approval",
    "ClaimJudgment",
    "Config",
    "CountedReport",
    "Destination",
    "Effect",
    "Excuse",
    "ExcusedIntent",
    "FactVersion",
    "Failure",
    "Fault",
    "IntentKnowledge",
    "Knowledge",
    "Message",
    "PropertyResult",
    "PropertyVerdict",
    "Read",
    "ReasonCode",
    "RecordsError",
    "ReportError",
    "RequestFinding",
    "RequestOutcome",
    "RunInfo",
    "RunRecords",
    "RunResult",
    "RunSummary",
    "SentWrite",
    "Settlement",
    "SubmittedReport",
    "Verdict",
    "WrongRole",
    "config_hash",
    "evaluate",
    "evaluate_run",
    "facts_without_effects",
    "load_results",
    "load_run",
    "markdown_report",
    "result_json",
    "run_verdict",
    "runs_csv",
    "stack_knowledge",
    "write_result",
]
