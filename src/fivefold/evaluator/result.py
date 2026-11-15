"""The evaluator's result for one run, and its JSON (0004, Verdict and Metrics).

A result holds every property's verdict, reason codes, failures and metrics, the run
verdict over the properties evaluated, and how each claim of the counted report was
judged. Properties not yet evaluated say so (`not_evaluated`); they never pass by
default, and the run verdict ignores them. `evaluated` names the properties it covers.

`result_json` writes the result as JSON with sorted keys, so the same records give the
same bytes (0009). Wall-clock time never appears in it.
"""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue

from fivefold.domain.report import ClaimStatus
from fivefold.evaluator.evidence import Knowledge, Settlement


class Verdict(StrEnum):
    """A run's verdict (0004)."""

    PASS = "pass"
    """Every property evaluated passes, and no claim is `unknown`."""
    PASS_UNRESOLVED = "pass_unresolved"
    """Every property evaluated passes, and at least one claim is `unknown`."""
    FAIL = "fail"
    """A property evaluated fails."""


class PropertyVerdict(StrEnum):
    """One property's verdict."""

    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUATED = "not_evaluated"
    """This evaluator does not check the property yet. It counts neither way."""


class ReasonCode(StrEnum):
    """Why a property failed. Each failure carries one code."""

    DUPLICATE_EFFECTS = "duplicate_effects"
    """Property 1: an intent has more than one effect at the end."""
    UNINTENDED_EFFECT = "unintended_effect"
    """Property 1: an effect belongs to no intent of the task."""
    NO_REPORT = "no_report"
    """Property 5: no valid report before the run stopped. One failure per intent."""
    DONE_UNSUPPORTED = "done_unsupported"
    """Property 5: `done`, but the stack had no commit evidence for the intent."""
    DONE_WRONG_EFFECTS = "done_wrong_effects"
    """Property 5: `done`, but the effect ids differ from the effects the stack knew."""
    NOT_DONE_UNSUPPORTED = "not_done_unsupported"
    """Property 5: `not_done`, but the stack lacked evidence that nothing commits."""
    NOT_DONE_BUT_COMMITTED = "not_done_but_committed"
    """Property 5: `not_done` on the evidence, but the ledger has an effect at the end."""
    UNKNOWN_BUT_KNOWN = "unknown_but_known"
    """Property 5: `unknown`, but the stack had the answer."""


PROPERTY_NAMES: dict[int, str] = {
    1: "exactly_once",
    2: "authorized_at_commit",
    3: "inputs_still_fresh",
    4: "actually_committed",
    5: "truthfully_reported",
}
"""The five properties (0004), by number."""


class ResultModel(BaseModel):
    """Base of the result models: unknown fields are refused and values never change."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Failure(ResultModel):
    """One failed check, with its reason code and what it is about."""

    code: ReasonCode
    intent_id: str | None = None
    effect_ids: tuple[str, ...] = ()
    detail: str


class PropertyResult(ResultModel):
    """One property's verdict, reason codes, failures and metrics."""

    property: int
    name: str
    verdict: PropertyVerdict
    reason_codes: tuple[ReasonCode, ...] = ()
    """Each code among the failures, once, sorted."""
    failures: tuple[Failure, ...] = ()
    metrics: dict[str, int] = {}


class RequestOutcome(ResultModel):
    """A write request the stack sent for an intent before `t_r`, and whether the
    evidence shows it never commits."""

    seq: int
    """The harness seq of its send, or of the crash that left it possibly sent."""
    possibly_sent: bool
    sent_vt: int
    idempotency_key: str | None
    settled_by: Settlement | None
    evidence_seq: int | None


class ClaimJudgment(ResultModel):
    """How one claim of the counted report was judged (0004, property 5)."""

    intent_id: str
    status: ClaimStatus
    claimed_effects: tuple[str, ...]
    knowledge: Knowledge
    """`K_i` at `t_r`."""
    known_effects: tuple[str, ...]
    """`known_i`, in commit order."""
    ledger_effects: tuple[str, ...]
    """`E_i(end)`, in commit order."""
    requests: tuple[RequestOutcome, ...]
    passed: bool
    reason_code: ReasonCode | None


class CountedReport(ResultModel):
    """The report that counts: the last valid one before the run stopped (0004)."""

    seq: int
    """Its row in `harness.final_report`."""
    worker: str
    accepted_by: str
    message_seq: int
    """The message that carried it to the component that accepted it."""
    t_r: int
    """That message's delivery seq: evidence counts only if delivered before it."""
    claims: tuple[ClaimJudgment, ...]


class RunSummary(ResultModel):
    """The run the result is for, from `harness.run`, without wall-clock time."""

    run_id: str
    scenario_id: str
    scenario_digest: str
    config: str
    contract: str
    seed: int
    topology: str
    model_adapter: str
    model: str
    cassette_digest: str | None
    parent_run_id: str | None
    branch_point: int | None
    params: JsonValue


class RunResult(ResultModel):
    """The evaluator's result for one run."""

    code_version: str
    run: RunSummary
    verdict: Verdict
    evaluated: tuple[int, ...]
    """The properties checked; the verdict covers these only."""
    properties: tuple[PropertyResult, ...]
    """All five properties, in order, including those not evaluated."""
    metrics: dict[str, int]
    """Every metric of the properties evaluated."""
    excused_intents: tuple[str, ...] | None
    """Intents property 4 excuses; None while property 4 is not evaluated."""
    report: CountedReport | None
    """The counted report and how each claim was judged; None when there is none."""

    def property_result(self, number: int) -> PropertyResult:
        """The result of property `number`, 1 to 5."""
        for result in self.properties:
            if result.property == number:
                return result
        raise KeyError(number)


def result_json(result: RunResult) -> str:
    """The result as JSON: sorted keys, two-space indents and a final newline."""
    return json.dumps(result.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
