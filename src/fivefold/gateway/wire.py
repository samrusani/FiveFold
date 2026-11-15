"""What the agent is told: the JSON the gateway's tools return (0003, 0001).

Write tools and `get_action_status` return one of two shapes, an action result or a
refusal (0003, "What the agent is told"). Read tools return `{read_id, key, version,
data}`. `submit_final_report` returns `{"result": "accepted"}` or a refusal.

- `effect_id` is set only when the state is `confirmed`; `stale` lists the facts that
  made a stale input reject the action; `reconcile` appears only with `outcome_unknown`.
- A refusal's `details` depend on its reason: `intent_busy` names the holding action,
  its state and its arguments, and `intent_done` the confirmed action and its effect
  (0001, item 10); `unknown_read` lists the read ids the gateway never issued,
  `missing_basis` the dependencies no citation covers, and `invalid_args` what broke the
  schema; `report_inconsistent` names each conflicting claim and the gateway's record of
  each action for its intent (0003, final report). Other reasons have empty details.
"""

from __future__ import annotations

from collections.abc import Mapping

from fivefold.gateway.records import (
    ActionResult,
    ClaimConflict,
    ReadRecord,
    Refusal,
    RefusalReason,
    ReportAccepted,
    StaleFact,
)

Json = dict[str, object]
"""A JSON object as the agent receives it."""


def action(result: ActionResult) -> Json:
    """The `{"result": "action", ...}` shape."""
    shaped: Json = {
        "result": "action",
        "action_id": result.action_id,
        "intent_id": result.intent_id,
        "state": str(result.state),
        "effect_id": result.effect_id,
        "reason": str(result.reason),
        "stale": [_stale(fact) for fact in result.stale],
    }
    if result.reconcile is not None:
        shaped["reconcile"] = {
            "contract": str(result.reconcile.contract),
            "how": str(result.reconcile.how),
            "escalation_id": result.reconcile.escalation_id,
            "what": result.reconcile.what,
        }
    return shaped


def refusal(refused: Refusal) -> Json:
    """The `{"result": "refused", "reason": ..., "details": {...}}` shape."""
    return {"result": "refused", "reason": str(refused.reason), "details": _details(refused)}


def outcome(result: ActionResult | Refusal) -> Json:
    """A write tool's or `get_action_status`'s result."""
    return action(result) if isinstance(result, ActionResult) else refusal(result)


def report(result: ReportAccepted | Refusal) -> Json:
    """`submit_final_report`'s result."""
    return {"result": "accepted"} if isinstance(result, ReportAccepted) else refusal(result)


def read(record: ReadRecord) -> Json:
    """A read tool's result: the recorded read and the fact's value."""
    return {
        "read_id": record.read_id,
        "key": record.key,
        "version": record.version,
        "data": _plain(record.data),
    }


def _plain(value: object) -> object:
    """Read-only mappings and tuples as JSON objects and arrays."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _details(refused: Refusal) -> Json:
    reason = refused.reason
    if reason is RefusalReason.INTENT_BUSY:
        return {
            "action_id": refused.action_id,
            "state": None if refused.state is None else str(refused.state),
            "args": None if refused.args is None else dict(refused.args),
        }
    if reason is RefusalReason.INTENT_DONE:
        return {"action_id": refused.action_id, "effect_id": refused.effect_id}
    if reason is RefusalReason.UNKNOWN_READ:
        return {"read_ids": list(refused.unknown_reads)}
    if reason is RefusalReason.MISSING_BASIS:
        return {"missing": list(refused.missing_keys)}
    if reason is RefusalReason.INVALID_ARGS:
        return {"message": refused.message}
    if reason is RefusalReason.REPORT_INCONSISTENT:
        return {"conflicts": [_conflict(conflict) for conflict in refused.conflicts]}
    return {}


def _conflict(conflict: ClaimConflict) -> Json:
    return {
        "intent_id": conflict.intent_id,
        "claimed": str(conflict.claimed),
        "claimed_effect_ids": list(conflict.claimed_effect_ids),
        "recorded": [
            {
                "action_id": recorded.action_id,
                "state": str(recorded.state),
                "effect_ids": list(recorded.effect_ids),
            }
            for recorded in conflict.recorded
        ],
    }


def _stale(fact: StaleFact) -> Json:
    return {
        "key": fact.key,
        "cited_version": fact.cited_version,
        "current_version": fact.current_version,
        "changed_fields": list(fact.changed_fields),
    }
