"""The evaluator's result for one run, and its JSON (0004, Verdict and Metrics).

A result holds every property's verdict, reason codes, failures and metrics, the run
verdict over all five properties, the intents property 4 excuses with their excuse, how
each claim of the counted report was judged, and the recovery latency of each intent a
fault touched (0014). `evaluated` names the properties the result covers: all five.

`result_json` writes the result as JSON with sorted keys, so the same records give the
same bytes (0009). Its one kind of wall-clock value is recovery latency by the wall clock,
in fields whose names end in `wall_ms`. They are for metrics only, so comparisons that
need equal bytes compare `comparable_json`, which leaves them out (0009, as amended by
0014).
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, JsonValue, StrictInt, model_validator

from fivefold.domain.report import ClaimStatus
from fivefold.evaluator.evidence import Knowledge, Settlement


class Verdict(StrEnum):
    """A run's verdict (0004)."""

    PASS = "pass"
    """All five properties pass, and no claim is `unknown`."""
    PASS_UNRESOLVED = "pass_unresolved"
    """All five properties pass, and at least one claim is `unknown`."""
    FAIL = "fail"
    """A property fails."""


class PropertyVerdict(StrEnum):
    """One property's verdict."""

    PASS = "pass"
    FAIL = "fail"


class ReasonCode(StrEnum):
    """Why a property failed. Each failure carries one code.

    Property 2's codes are the names the simulator's C3 check gives the same failures
    (`fivefold.domain.api.CheckName`). Property 3's are record 0004's, which the shared
    basis check returns (`fivefold.domain.rules.BasisFailure`).
    """

    DUPLICATE_EFFECTS = "duplicate_effects"
    """Property 1: an intent has more than one effect at the end."""
    UNINTENDED_EFFECT = "unintended_effect"
    """Property 1: an effect belongs to no intent of the task."""
    APPROVAL_UNKNOWN = "approval_unknown"
    """Property 2, clause 1: the cited approval does not exist in this run."""
    APPROVAL_PENDING = "approval_pending"
    """Property 2, clause 2: the approval was not decided before the effect committed."""
    APPROVAL_DENIED = "approval_denied"
    """Property 2, clause 2: the approval was denied before the effect committed."""
    APPROVAL_REVOKED = "approval_revoked"
    """Property 2, clause 3: the approval was revoked before the effect committed."""
    APPROVAL_EXPIRED = "approval_expired"
    """Property 2, clause 4: the effect committed at or after the approval's expiry."""
    APPROVAL_OUT_OF_SCOPE = "approval_out_of_scope"
    """Property 2, clause 5: the approval's scope does not cover the effect."""
    APPROVAL_USED = "approval_used"
    """Property 2, clause 6: an effect that committed earlier cites the same approval."""
    UNDECLARED_INPUT = "undeclared_input"
    """Property 3: the basis names no version of a dependency."""
    UNOBSERVED_INPUT = "unobserved_input"
    """Property 3: the cited version was not served to the run before the write reached
    the simulator."""
    STALE_INPUT = "stale_input"
    """Property 3: the dependency's rule fails between the cited version and the fact
    just before the commit."""
    MISSING_EFFECT = "missing_effect"
    """Property 4: a required intent has no acceptable effect at the end, and no excuse."""
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


class Excuse(StrEnum):
    """Why property 4 excuses a required intent that has no acceptable effect (0004)."""

    AUTHORITY_WITHDRAWN = "authority_withdrawn"
    """An approval for the intent that covered an acceptable effect was revoked, or a
    human approver denied one, and no approval for the intent is valid and unused at the
    end."""
    INFEASIBLE = "infeasible"
    """No allowed option fits the facts at the end with every effect of the run removed."""


PROPERTY_NAMES: dict[int, str] = {
    1: "exactly_once",
    2: "authorized_at_commit",
    3: "inputs_still_fresh",
    4: "actually_committed",
    5: "truthfully_reported",
}
"""The five properties (0004), by number."""


class Outcome(StrEnum):
    """An affected intent's outcome at the end, from the ledger (0014)."""

    COMMITTED = "committed"
    """The intent has an effect at the end: `E_i(end)` is not empty."""
    NOT_COMMITTED = "not_committed"
    """The intent has no effect at the end."""


class SettledBy(StrEnum):
    """What settled an affected intent's outcome for the stack, ending its recovery (0014).

    For a committed outcome, the first commit evidence for one of the intent's effects
    (`fivefold.evaluator.evidence.CommitSource`). For a not_committed one, how the last of
    its write requests was first shown never to commit
    (`fivefold.evaluator.evidence.Settlement`), or that it sent none.
    """

    WRITE_ANSWER = "write_answer"
    STATUS_ANSWER = "status_answer"
    ATTESTATION = "attestation"
    REJECTED = "rejected"
    STATUS_EMPTY = "status_empty"
    SAME_KEY_REJECTED = "same_key_rejected"
    ATTESTED_NOT_COMMITTED = "attested_not_committed"
    NO_WRITE = "no_write"
    """The stack sent no write request for the intent, nor possibly sent one."""


class ResultModel(BaseModel):
    """Frozen result models with strict integer fields and no unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Failure(ResultModel):
    """One failed check, with its reason code and what it is about."""

    code: ReasonCode
    intent_id: str | None = None
    effect_ids: tuple[str, ...] = ()
    detail: str


class ExcusedIntent(ResultModel):
    """A required intent with no acceptable effect that property 4 excuses, and why."""

    intent_id: str
    excuse: Excuse
    approval_ids: tuple[str, ...] = ()
    """For `authority_withdrawn`: the approvals that were revoked or denied by a human."""
    detail: str


class PropertyResult(ResultModel):
    """One property's verdict, reason codes, failures and metrics."""

    property: StrictInt
    name: str
    verdict: PropertyVerdict
    reason_codes: tuple[ReasonCode, ...] = ()
    """Each code among the failures, once, sorted."""
    failures: tuple[Failure, ...] = ()
    metrics: dict[str, StrictInt] = {}


class RequestOutcome(ResultModel):
    """A write request the stack sent for an intent before `t_r`, and whether the
    evidence shows it never commits."""

    seq: StrictInt
    """The harness seq of its send, or of the crash that left it possibly sent."""
    possibly_sent: bool
    sent_vt: StrictInt
    idempotency_key: str | None
    settled_by: Settlement | None
    evidence_seq: StrictInt | None


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

    seq: StrictInt
    """Its row in `harness.final_report`."""
    worker: str
    accepted_by: str
    message_seq: StrictInt
    """The message that carried it to the component that accepted it."""
    t_r: StrictInt
    """That message's delivery seq: evidence counts only if delivered before it."""
    claims: tuple[ClaimJudgment, ...]


class RecoveryEntry(ResultModel):
    """Recovery latency for one task intent a fault touched (0014).

    The start is the first fault, by seq, that touched the intent. The end is the first
    harness seq at or after the start from which the stack's knowledge of the intent
    (`K_i`, 0004) equals its outcome until the run stops: the start itself when the stack
    held that evidence already. An intent with no end is unresolved: its end, latencies
    and `settled_by` are None, and `knowledge_at_stop` says what the stack knew when the
    run stopped.
    """

    intent_id: str
    fault_seq: StrictInt
    """The harness seq of the first fault that touched the intent."""
    fault: str
    """That fault's id, such as `F2`."""
    start_vt: StrictInt
    """When that fault fired."""
    outcome: Outcome
    settled_by: SettledBy | None
    end_seq: StrictInt | None
    end_vt: StrictInt | None
    """When the evidence that settled the outcome reached the stack: a delivery's
    `delivered_vt`, or an attestation's `vt`."""
    latency_ms: StrictInt | None
    """`end_vt - start_vt`, in virtual ms."""
    latency_wall_ms: StrictInt | None
    """The same span by the wall clock, rounded to the nearest ms: from the start fault's
    `wall_at` to the delivery's `delivered_wall_at` or the attestation's `wall_at`, and 0
    when the end is the start. None when a wall-clock time it needs is missing. For
    metrics only."""
    knowledge_at_stop: Knowledge | None
    """For an unresolved intent, `K_i` when the run stopped; None for a resolved one."""

    @property
    def resolved(self) -> bool:
        """Whether the stack's evidence settled the intent's outcome."""
        return self.end_seq is not None


class RunSummary(ResultModel):
    """The run the result is for, from `harness.run`, without wall-clock time."""

    run_id: str
    scenario_id: str
    scenario_digest: str
    config: str
    contract: str
    seed: StrictInt
    topology: str
    model_adapter: str
    model: str
    cassette_digest: str | None
    parent_run_id: str | None
    branch_point: StrictInt | None
    params: JsonValue
    end_vt: StrictInt
    """When the run ended, as property 4 judges "at the end": the harness's stop time."""

    @model_validator(mode="after")
    def check_branch_fields(self) -> Self:
        if (self.parent_run_id is None) != (self.branch_point is None):
            raise ValueError("parent_run_id and branch_point must be set together")
        return self


class RunResult(ResultModel):
    """The evaluator's result for one run."""

    code_version: str
    run: RunSummary
    verdict: Verdict
    evaluated: tuple[StrictInt, ...]
    """The properties checked, which the verdict covers: all five."""
    properties: tuple[PropertyResult, ...]
    """All five properties, in order."""
    metrics: dict[str, StrictInt]
    """Every metric: each property's, the run's own counts of human actions and
    approvals, the intents a fault touched and those left unresolved, and the tokens its
    model calls used (`fivefold.evaluator.metrics`)."""
    excused_intents: tuple[ExcusedIntent, ...]
    """The required intents property 4 excuses, with their excuse, in scenario order."""
    report: CountedReport | None
    """The counted report and how each claim was judged; None when there is none."""
    recovery: tuple[RecoveryEntry, ...]
    """Recovery latency of each task intent a fault touched, in scenario order (0014)."""

    def property_result(self, number: int) -> PropertyResult:
        """The result of property `number`, 1 to 5."""
        for result in self.properties:
            if result.property == number:
                return result
        raise KeyError(number)


WALL_CLOCK_SUFFIXES = ("wall_at", "wall_ms")
"""Wall-clock columns and fields end in one of these. They are for metrics only (0009)."""


def without_wall_clock(value: object) -> object:
    """JSON data without the fields whose names end in a wall-clock suffix, at any depth."""
    if isinstance(value, dict):
        return {
            key: without_wall_clock(item)
            for key, item in value.items()
            if not str(key).endswith(WALL_CLOCK_SUFFIXES)
        }
    if isinstance(value, list):
        return [without_wall_clock(item) for item in value]
    return value


def result_json(result: RunResult) -> str:
    """The result as JSON: sorted keys, two-space indents and a final newline."""
    return _json(result.model_dump(mode="json"))


def comparable_json(result: RunResult) -> str:
    """The result as `result_json` writes it, without its wall-clock fields: what a
    comparison that needs equal bytes compares (0009, as amended by 0014). Two runs with
    the same records but for wall-clock times give the same text."""
    return _json(without_wall_clock(result.model_dump(mode="json")))


def _json(data: object) -> str:
    return json.dumps(data, sort_keys=True, indent=2) + "\n"
