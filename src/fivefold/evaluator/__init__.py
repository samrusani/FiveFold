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
stores a result in the `eval` schema.
"""

from fivefold.evaluator.checks import CLAIM_METRICS
from fivefold.evaluator.evaluate import evaluate, run_verdict
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
    "EVALUATOR_ROLE",
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
    "evaluate",
    "evaluate_run",
    "facts_without_effects",
    "load_run",
    "result_json",
    "run_verdict",
    "stack_knowledge",
    "write_result",
]
