"""The evaluator (0004): property checks, metrics and the result of each run.

It decides pass or fail only from simulator and harness records and the scenario, and
never calls a model or reads a transcript. It imports only `fivefold.domain`: it reads
other components' tables, never their code. It checks all five properties: 1, exactly
once (`checks`); 2, authorized at commit (`authorization`); 3, inputs still fresh
(`freshness`); 4, actually committed (`liveness`); and 5, truthfully reported (`checks`,
with `evidence`). It also measures recovery latency for each intent a fault touched
(`recovery`, 0014), from the same records.

Typical use, after the run has stopped:

    result = evaluate_run(conninfo_as_ff_eval, load_scenario_file(path))
    text = result_json(result)

`load_run` and `evaluate` split that into reading and pure scoring; `write_result`
stores a result in the `eval` schema. Across runs, `load_results` reads saved result
files, and `runs_csv` and `markdown_report` write a CSV with one row per run and a
Markdown report, with each run's cost when given a price file (`report`; `python -m
fivefold.evaluator report`).
"""

from fivefold.evaluator.checks import CLAIM_METRICS
from fivefold.evaluator.evaluate import evaluate, run_verdict
from fivefold.evaluator.evidence import (
    CommitEvidence,
    CommitSource,
    IntentKnowledge,
    Knowledge,
    RequestFinding,
    SentWrite,
    Settlement,
    commit_evidence,
    stack_knowledge,
)
from fivefold.evaluator.liveness import facts_without_effects
from fivefold.evaluator.load import load_run
from fivefold.evaluator.metrics import RECOVERY_METRICS, RUN_METRICS, TOKEN_METRICS
from fivefold.evaluator.records import (
    ActorAction,
    Approval,
    Config,
    Destination,
    Effect,
    FactVersion,
    Fault,
    Message,
    ModelCall,
    Read,
    RecordsError,
    RunInfo,
    RunRecords,
    SubmittedReport,
)
from fivefold.evaluator.recovery import recovery_entries
from fivefold.evaluator.report import (
    COST_COLUMN,
    CSV_COLUMNS,
    METRICS,
    ReportError,
    load_prices,
    load_results,
    markdown_report,
    run_cost,
    runs_csv,
)
from fivefold.evaluator.result import (
    WALL_CLOCK_SUFFIXES,
    ClaimJudgment,
    CountedReport,
    Excuse,
    ExcusedIntent,
    Failure,
    Outcome,
    PropertyResult,
    PropertyVerdict,
    ReasonCode,
    RecoveryEntry,
    RequestOutcome,
    RunResult,
    RunSummary,
    SettledBy,
    Verdict,
    comparable_json,
    result_json,
    without_wall_clock,
)
from fivefold.evaluator.store import EVALUATOR_ROLE, WrongRole, evaluate_run, write_result

__all__ = [
    "CLAIM_METRICS",
    "COST_COLUMN",
    "CSV_COLUMNS",
    "EVALUATOR_ROLE",
    "METRICS",
    "RECOVERY_METRICS",
    "RUN_METRICS",
    "TOKEN_METRICS",
    "WALL_CLOCK_SUFFIXES",
    "ActorAction",
    "Approval",
    "ClaimJudgment",
    "CommitEvidence",
    "CommitSource",
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
    "ModelCall",
    "Outcome",
    "PropertyResult",
    "PropertyVerdict",
    "Read",
    "ReasonCode",
    "RecordsError",
    "RecoveryEntry",
    "ReportError",
    "RequestFinding",
    "RequestOutcome",
    "RunInfo",
    "RunRecords",
    "RunResult",
    "RunSummary",
    "SentWrite",
    "SettledBy",
    "Settlement",
    "SubmittedReport",
    "Verdict",
    "WrongRole",
    "commit_evidence",
    "comparable_json",
    "evaluate",
    "evaluate_run",
    "facts_without_effects",
    "load_prices",
    "load_results",
    "load_run",
    "markdown_report",
    "recovery_entries",
    "result_json",
    "run_cost",
    "run_verdict",
    "runs_csv",
    "stack_knowledge",
    "without_wall_clock",
    "write_result",
]
