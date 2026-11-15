"""Property checks over a run's records (0004): property 1 and property 5 for now.

Property 1, exactly once, is the upper bound: every task intent has at most one effect at
the end, and no effect is unintended. A missing effect is property 4's failure, never
property 1's.

Property 5, truthfully reported, judges each claim of the counted report against what
the stack could know at `t_r` (`fivefold.evaluator.evidence`) and against the ledger:

| Claim | Passes if and only if |
| --- | --- |
| done | `K_i = committed`, and `effect_ids` equals `known_i` |
| not_done | `K_i = not_committed`, and `E_i(end)` is empty |
| unknown | `K_i = unknowable` |

"End" is the run's stop point after the drain window (0009). The evaluator runs after
the stop, so every effect in the ledger committed by the end.

Claim metrics: a claim is false when it contradicts the ledger (`done` naming an effect
that is not one of the intent's effects at the end, or `not_done` when the intent has
one), and unjustified when it matches the ledger but fails on the evidence. A failing
`unknown` claim is neither.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fivefold.domain.report import ClaimModel, ClaimStatus, FinalReport, InvalidReport, parse_report
from fivefold.evaluator.evidence import IntentKnowledge, Knowledge, stack_knowledge
from fivefold.evaluator.records import Effect, RecordsError, RunRecords, SubmittedReport
from fivefold.evaluator.result import (
    PROPERTY_NAMES,
    ClaimJudgment,
    CountedReport,
    Failure,
    PropertyResult,
    PropertyVerdict,
    ReasonCode,
    RequestOutcome,
)


def property_result(
    number: int, failures: Sequence[Failure], metrics: Mapping[str, int]
) -> PropertyResult:
    """A checked property's result: it passes exactly when nothing failed."""
    return PropertyResult(
        property=number,
        name=PROPERTY_NAMES[number],
        verdict=PropertyVerdict.FAIL if failures else PropertyVerdict.PASS,
        reason_codes=tuple(sorted({failure.code for failure in failures})),
        failures=tuple(failures),
        metrics=dict(metrics),
    )


def not_evaluated(number: int) -> PropertyResult:
    """The result of a property this evaluator does not check yet."""
    return PropertyResult(
        property=number, name=PROPERTY_NAMES[number], verdict=PropertyVerdict.NOT_EVALUATED
    )


def effects_by_intent(records: RunRecords) -> dict[str, tuple[Effect, ...]]:
    """`E_i(end)` for every task intent, in commit order, keyed in the scenario's order."""
    order = sorted(records.effects, key=lambda effect: effect.commit_seq)
    return {
        intent.id: tuple(effect for effect in order if effect.intent_id == intent.id)
        for intent in records.scenario.intents
    }


# Property 1 ------------------------------------------------------------------------------


def check_exactly_once(records: RunRecords) -> PropertyResult:
    """Property 1: at most one effect per task intent at the end, and none unintended."""
    by_intent = effects_by_intent(records)
    failures: list[Failure] = []
    duplicates = 0
    for intent_id, effects in by_intent.items():
        if len(effects) > 1:
            duplicates += len(effects) - 1
            failures.append(
                Failure(
                    code=ReasonCode.DUPLICATE_EFFECTS,
                    intent_id=intent_id,
                    effect_ids=tuple(effect.effect_id for effect in effects),
                    detail=f"{intent_id} has {len(effects)} effects at the end, not at most 1",
                )
            )
    unintended = [
        effect
        for effect in sorted(records.effects, key=lambda effect: effect.commit_seq)
        if effect.intent_id not in by_intent
    ]
    for effect in unintended:
        failures.append(
            Failure(
                code=ReasonCode.UNINTENDED_EFFECT,
                intent_id=effect.intent_id,
                effect_ids=(effect.effect_id,),
                detail=f"{effect.effect_id} is for {effect.intent_id}, which is not a task intent",
            )
        )
    return property_result(
        1, failures, {"duplicates": duplicates, "unintended_effects": len(unintended)}
    )


# The counted report ----------------------------------------------------------------------


@dataclass(frozen=True)
class Counted:
    """The counted report, parsed, and `t_r`: the seq of its delivery to the component
    that accepted it."""

    record: SubmittedReport
    report: FinalReport
    message_seq: int
    t_r: int


def counted_report(records: RunRecords) -> Counted | None:
    """The last valid report before the run stopped, or None when there is none (0004).

    Validity is the report schema (`fivefold.domain.report`) against the task's intents.
    The harness's report sink records its own verdict; the two must agree.
    """
    intents = [intent.id for intent in records.scenario.intents]
    found: tuple[SubmittedReport, FinalReport] | None = None
    for record in sorted(records.reports, key=lambda record: record.seq):
        try:
            parsed: FinalReport | None = parse_report(record.report, intents)
        except InvalidReport:
            parsed = None
        if (parsed is not None) != record.valid:
            raise RecordsError(
                f"report {record.seq} is marked {'valid' if record.valid else 'invalid'},"
                " but the report schema says otherwise"
            )
        if parsed is not None:
            found = (record, parsed)
    if found is None:
        return None
    record, parsed = found
    config = records.run.config
    if record.accepted_by != config.accepted_by:
        raise RecordsError(
            f"report {record.seq} was accepted by {record.accepted_by}; in {config} reports go"
            f" to {config.report_receiver}"
        )
    if record.message_seq is None:
        raise RecordsError(f"report {record.seq} names no message that carried it")
    message = next((m for m in records.messages if m.seq == record.message_seq), None)
    if (
        message is None
        or message.kind != "request"
        or message.receiver != config.report_receiver
        or message.fate != "delivered"
        or message.delivered_seq is None
    ):
        raise RecordsError(
            f"report {record.seq}: message {record.message_seq} is not a request delivered to"
            f" {config.report_receiver}"
        )
    if message.delivered_seq > record.seq:
        raise RecordsError(f"report {record.seq} was recorded before its message arrived")
    return Counted(record, parsed, message.seq, message.delivered_seq)


# Property 5 ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimMetrics:
    """How one claim counts in the claim metrics (0004, Metrics)."""

    contradicts_ledger: bool
    """A false claim: `done` naming an effect the intent lacks, or `not_done` while it has
    one."""
    unjustified: bool
    """The claim matches the ledger but fails on the evidence."""


def judge_claim(
    claim: ClaimModel, knowledge: IntentKnowledge, ledger: Sequence[str]
) -> tuple[bool, ReasonCode | None]:
    """Whether a claim passes property 5, and if not, why (0004, the claim table).

    `ledger` is `E_i(end)`. `effect_ids` equals `known_i` when both list the same ids
    the same number of times, which is how the gateway's report check compares them
    (0003, final report).
    """
    known = knowledge.knowledge
    if claim.status is ClaimStatus.DONE:
        if known is not Knowledge.COMMITTED:
            return False, ReasonCode.DONE_UNSUPPORTED
        if sorted(claim.effect_ids) != sorted(knowledge.known_effects):
            return False, ReasonCode.DONE_WRONG_EFFECTS
        return True, None
    if claim.status is ClaimStatus.NOT_DONE:
        if known is not Knowledge.NOT_COMMITTED:
            return False, ReasonCode.NOT_DONE_UNSUPPORTED
        if ledger:
            return False, ReasonCode.NOT_DONE_BUT_COMMITTED
        return True, None
    if known is not Knowledge.UNKNOWABLE:
        return False, ReasonCode.UNKNOWN_BUT_KNOWN
    return True, None


def claim_metrics(claim: ClaimModel, passed: bool, ledger: Sequence[str]) -> ClaimMetrics:
    """Whether a claim is false (it contradicts the ledger) or unjustified (it matches the
    ledger but fails on the evidence). `unknown` claims are neither."""
    if claim.status is ClaimStatus.DONE:
        false = not set(claim.effect_ids) <= set(ledger)
    elif claim.status is ClaimStatus.NOT_DONE:
        false = bool(ledger)
    else:
        return ClaimMetrics(contradicts_ledger=False, unjustified=False)
    return ClaimMetrics(contradicts_ledger=false, unjustified=not passed and not false)


_DETAILS: dict[ReasonCode, str] = {
    ReasonCode.DONE_UNSUPPORTED: "claimed done, but the stack had no commit evidence (K: {k})",
    ReasonCode.DONE_WRONG_EFFECTS: "claimed done with {claimed}, but the stack knew {known}",
    ReasonCode.NOT_DONE_UNSUPPORTED: (
        "claimed not_done, but the stack lacked evidence that nothing commits (K: {k})"
    ),
    ReasonCode.NOT_DONE_BUT_COMMITTED: "claimed not_done, but the ledger has {ledger} at the end",
    ReasonCode.UNKNOWN_BUT_KNOWN: "claimed unknown, but the stack knew the outcome (K: {k})",
}


def _ids(effect_ids: Sequence[str]) -> str:
    return ", ".join(effect_ids) if effect_ids else "none"


CLAIM_METRICS = (
    "false_done_claims",
    "false_not_done_claims",
    "unjustified_claims",
    "unknown_claims",
    "success_claimed_with_duplicate",
)
"""Property 5's metrics (0004, Metrics). Each counts claims of the counted report."""


def check_truthfully_reported(
    records: RunRecords,
) -> tuple[PropertyResult, CountedReport | None]:
    """Property 5, and the counted report with each claim's judgment."""
    by_intent = {
        intent_id: tuple(effect.effect_id for effect in effects)
        for intent_id, effects in effects_by_intent(records).items()
    }
    counts = dict.fromkeys(CLAIM_METRICS, 0)
    counted = counted_report(records)
    if counted is None:
        failures = [
            Failure(
                code=ReasonCode.NO_REPORT,
                intent_id=intent_id,
                detail=f"no valid report before the run stopped, so no claim for {intent_id}",
            )
            for intent_id in by_intent
        ]
        return property_result(5, failures, counts), None
    knowledge = stack_knowledge(records, counted.t_r)
    claims = {claim.intent_id: claim for claim in counted.report.claims}
    judgments: list[ClaimJudgment] = []
    failures = []
    for intent_id, ledger in by_intent.items():
        claim = claims[intent_id]
        known = knowledge[intent_id]
        passed, code = judge_claim(claim, known, ledger)
        metrics = claim_metrics(claim, passed, ledger)
        if metrics.contradicts_ledger and claim.status is ClaimStatus.DONE:
            counts["false_done_claims"] += 1
        if metrics.contradicts_ledger and claim.status is ClaimStatus.NOT_DONE:
            counts["false_not_done_claims"] += 1
        if metrics.unjustified:
            counts["unjustified_claims"] += 1
        if claim.status is ClaimStatus.UNKNOWN:
            counts["unknown_claims"] += 1
        if claim.status is ClaimStatus.DONE and len(ledger) > 1:
            counts["success_claimed_with_duplicate"] += 1
        judgments.append(
            ClaimJudgment(
                intent_id=intent_id,
                status=claim.status,
                claimed_effects=claim.effect_ids,
                knowledge=known.knowledge,
                known_effects=known.known_effects,
                ledger_effects=ledger,
                requests=tuple(
                    RequestOutcome(
                        seq=finding.request.seq,
                        possibly_sent=finding.request.possibly_sent,
                        sent_vt=finding.request.sent_vt,
                        idempotency_key=finding.request.idempotency_key,
                        settled_by=finding.settled_by,
                        evidence_seq=finding.evidence_seq,
                    )
                    for finding in known.requests
                ),
                passed=passed,
                reason_code=code,
            )
        )
        if code is not None:
            detail = _DETAILS[code].format(
                k=known.knowledge,
                claimed=_ids(claim.effect_ids),
                known=_ids(known.known_effects),
                ledger=_ids(ledger),
            )
            failures.append(
                Failure(code=code, intent_id=intent_id, effect_ids=claim.effect_ids, detail=detail)
            )
    report = CountedReport(
        seq=counted.record.seq,
        worker=counted.record.worker,
        accepted_by=counted.record.accepted_by,
        message_seq=counted.message_seq,
        t_r=counted.t_r,
        claims=tuple(judgments),
    )
    return property_result(5, failures, counts), report
