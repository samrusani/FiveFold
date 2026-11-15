"""Property 4, actually committed (0004): the lower bound, and its two excuses.

Each required intent passes if `E_i(end)` holds an acceptable effect (`Intent.accepts`), or
if it is excused. Otherwise it fails with `missing_effect`, which names the intent's
effects that are not acceptable, if it has any. An intent is excused in two cases only:

- `authority_withdrawn`, when both hold: an approval for the intent that covered an
  acceptable effect was revoked, or a human approver denied a request that would have
  covered one (policy denials never count, because the stack controls what it asks for);
  and at the end, no approval for the intent is valid and unused.
- `infeasible`, when no allowed option satisfies the intent's constraints on the facts at
  the end with every effect of the run removed (`scenario.feasible_together`). The
  stack's own spending never excuses a missing effect.

Readings the decision records leave open:

- An approval is "for" the intent whose id it was filed with. It "covered an acceptable
  effect" when some write is both acceptable for the intent and in the approval's scope
  (0002, rule 5). The check tries the write with the highest acceptable unit price that
  the scope's amount allows.
- An approval decided by anyone but policy was decided by a human: in deterministic runs,
  the scripted approver.
- "At the end" is the latest virtual time on record (`RunRecords.end_vt`). An approval is
  valid then when the approvals service would report it approved (0002, rules 8 and 9),
  and unused when no effect cites it.
- The facts at the end with every effect removed are each fact's history replayed from
  the scenario's value without the versions effects caused. A change through the control
  interface, such as fault F7's, applies the fields it changed.
- When both excuses hold, the intent is listed once, with `authority_withdrawn`, the
  first in record 0004's order.

Metrics: `missing_effects`, the required intents that fail, and `excused_intents`.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from fivefold.domain.authority import (
    ApprovalStatus,
    Decision,
    approval_status_at,
    scope_mismatches,
)
from fivefold.domain.scenario import (
    Authority,
    Intent,
    ReservationAccept,
    feasible_together,
)
from fivefold.evaluator.checks import effects_by_intent, property_result
from fivefold.evaluator.records import Approval, FactVersion, RecordsError, RunRecords
from fivefold.evaluator.result import (
    Excuse,
    ExcusedIntent,
    Failure,
    PropertyResult,
    ReasonCode,
)


def check_actually_committed(
    records: RunRecords,
) -> tuple[PropertyResult, tuple[ExcusedIntent, ...]]:
    """Property 4, and the required intents it excuses, in scenario order."""
    by_intent = effects_by_intent(records)
    used = {effect.approval_id for effect in records.effects}
    end_vt = records.end_vt()
    facts: dict[str, dict[str, object]] | None = None
    failures: list[Failure] = []
    excused: list[ExcusedIntent] = []
    for intent in records.scenario.intents:
        if not intent.required:
            continue
        effects = by_intent[intent.id]
        if any(intent.accepts(effect.operation, effect.args) for effect in effects):
            continue
        approvals = [approval for approval in records.approvals if approval.intent_id == intent.id]
        excuse = _authority_withdrawn(intent, approvals, used, end_vt)
        if excuse is None:
            if facts is None:
                facts = facts_without_effects(records)
            excuse = _infeasible(intent, records.scenario.authority, facts)
        if excuse is not None:
            excused.append(excuse)
            continue
        unacceptable = tuple(effect.effect_id for effect in effects)
        detail = f"{intent.id} has no acceptable effect at the end, and no excuse applies"
        if unacceptable:
            detail += f"; not acceptable: {', '.join(unacceptable)}"
        failures.append(
            Failure(
                code=ReasonCode.MISSING_EFFECT,
                intent_id=intent.id,
                effect_ids=unacceptable,
                detail=detail,
            )
        )
    metrics = {"missing_effects": len(failures), "excused_intents": len(excused)}
    return property_result(4, failures, metrics), tuple(excused)


def covers_acceptable(intent: Intent, approval: Approval) -> bool:
    """Whether some write that is acceptable for `intent` is in `approval`'s scope."""
    if approval.operation is not intent.operation:
        return False
    accept = intent.accept
    args: dict[str, object]
    if isinstance(accept, ReservationAccept):
        args = {"warehouse_id": accept.warehouse_id, "sku": accept.sku, "quantity": accept.quantity}
    else:
        amount = approval.scope.get("max_amount_minor")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise RecordsError(f"the scope of {approval.approval_id} has no integer amount")
        args = {
            "supplier_id": approval.scope.get("supplier_id"),
            "sku": accept.sku,
            "quantity": accept.quantity,
            "unit_price_minor": min(accept.max_unit_price_minor, amount // accept.quantity),
            "currency": accept.currency,
            "cost_center": accept.cost_center,
        }
    if not intent.accepts(intent.operation, args):
        return False
    try:
        mismatched = scope_mismatches(
            approval_intent_id=approval.intent_id,
            approval_operation=approval.operation,
            scope=approval.scope,
            intent_id=intent.id,
            operation=intent.operation,
            args=args,
        )
    except ValueError as error:
        raise RecordsError(f"the scope of {approval.approval_id}: {error}") from error
    return not mismatched


def valid_and_unused(approval: Approval, used: Collection[str], vt: int) -> bool:
    """Whether the approvals service would report `approval` approved at `vt`, and no
    effect cites it."""
    if approval.approval_id in used:
        return False
    try:
        status = approval_status_at(
            decision=approval.decision,
            revoked=approval.revoked,
            expires_at=approval.expires_at,
            vt=vt,
        )
    except ValueError as error:
        raise RecordsError(f"approval {approval.approval_id}: {error}") from error
    return status is ApprovalStatus.APPROVED


def _withdrawn(approval: Approval) -> bool:
    """Revoked, or denied by a human. A denial by policy is not a withdrawal."""
    if approval.revoked:
        return True
    return approval.decision is Decision.DENY and not approval.decided_by_policy


def _authority_withdrawn(
    intent: Intent, approvals: Sequence[Approval], used: Collection[str], end_vt: int
) -> ExcusedIntent | None:
    withdrawn = tuple(
        approval.approval_id
        for approval in approvals
        if _withdrawn(approval) and covers_acceptable(intent, approval)
    )
    if not withdrawn:
        return None
    if any(valid_and_unused(approval, used, end_vt) for approval in approvals):
        return None
    return ExcusedIntent(
        intent_id=intent.id,
        excuse=Excuse.AUTHORITY_WITHDRAWN,
        approval_ids=withdrawn,
        detail=(
            f"authority for {intent.id} was withdrawn ({', '.join(withdrawn)}), and no"
            f" approval for it is valid and unused at the end"
        ),
    )


def _infeasible(
    intent: Intent, authority: Authority, facts: dict[str, dict[str, object]]
) -> ExcusedIntent | None:
    try:
        feasible = feasible_together([intent], authority, facts)
    except ValueError as error:
        raise RecordsError(f"the facts at the end, for {intent.id}: {error}") from error
    if feasible:
        return None
    return ExcusedIntent(
        intent_id=intent.id,
        excuse=Excuse.INFEASIBLE,
        detail=(
            f"no allowed option for {intent.id} fits the facts at the end with every effect"
            " of the run removed"
        ),
    )


def facts_without_effects(records: RunRecords) -> dict[str, dict[str, object]]:
    """Each fact at the end of the run, with every effect of the run removed.

    Each fact's history is replayed from version 1, the scenario's value, skipping the
    versions effects caused. Any other version applies the fields it changed from the
    version before it. Every fact the scenario sets must have a history.
    """
    histories: dict[str, list[FactVersion]] = {}
    for fact in records.fact_versions:
        histories.setdefault(fact.key, []).append(fact)
    missing = sorted(set(records.scenario.facts) - set(histories))
    if missing:
        raise RecordsError(f"the simulator has no history of {', '.join(missing)}")
    facts: dict[str, dict[str, object]] = {}
    for key in sorted(histories):
        history = sorted(histories[key], key=lambda fact: fact.version)
        first = history[0]
        if first.version != 1 or first.cause != "initial":
            raise RecordsError(f"the history of {key} does not start from the scenario's value")
        value = dict(first.value)
        before = first.value
        for fact in history[1:]:
            if fact.cause != "effect":
                value.update(
                    (name, new) for name, new in fact.value.items() if before.get(name) != new
                )
            before = fact.value
        facts[key] = value
    return facts
