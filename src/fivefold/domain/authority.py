"""Authority rules shared by the approvals service, the gateway and the evaluator (0002).

The simulator's approvals service is the system of record for authority. The scenario's
`authority` section is its policy (0009). These pure functions state the policy's
decision on a request (rule 6), whether a write is in an approval's scope (rule 5), and
an approval's status at a virtual time (rules 8 and 9).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum

from fivefold.domain.rules import Operation, order_amount_minor


class ApprovalStatus(StrEnum):
    """An approval's status as the approvals service reports it (0002, rules 6 to 9).

    `expired` is never stored: an approved approval is expired from `expires_at` on.
    """

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "revoked"
    EXPIRED = "expired"


class Decision(StrEnum):
    """A decision on an approval request, by policy or by the scripted approver."""

    APPROVE = "approve"
    DENY = "deny"


class PolicyDecision(StrEnum):
    """What policy does with a request on arrival (0002, rule 6)."""

    APPROVE = "approve"
    DENY = "deny"
    QUEUE = "queue"
    """Wait for the scripted approver."""


SCOPE_FIELDS: Mapping[Operation, tuple[str, ...]] = {
    Operation.SUBMIT_PURCHASE_ORDER: (
        "supplier_id",
        "sku",
        "max_quantity",
        "max_amount_minor",
        "currency",
        "cost_center",
    ),
    Operation.RESERVE_INVENTORY: ("warehouse_id", "sku", "max_quantity"),
}
"""The scope fields of each operation besides `intent_id` and `operation` (0002, rule 5)."""

POLICY_ACTOR = "policy"
"""The actor recorded for decisions that policy makes on arrival."""


def _integer(values: Mapping[str, object], name: str) -> int:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def policy_decision(
    operation: Operation,
    scope: Mapping[str, object],
    *,
    allowlist: Collection[str],
    per_order_ceiling_minor: int,
    auto_approve_up_to_minor: int,
) -> PolicyDecision:
    """Decide a request on arrival (0002, rule 6).

    A purchase order from a supplier off the allowlist, or above the per-order ceiling, is
    denied. One at or below the auto-approval threshold is approved, as is every
    reservation. Every other purchase order waits for the scripted approver. The amount is
    the request's `max_amount_minor`.
    """
    if operation is Operation.RESERVE_INVENTORY:
        return PolicyDecision.APPROVE
    amount = _integer(scope, "max_amount_minor")
    if scope.get("supplier_id") not in allowlist or amount > per_order_ceiling_minor:
        return PolicyDecision.DENY
    if amount <= auto_approve_up_to_minor:
        return PolicyDecision.APPROVE
    return PolicyDecision.QUEUE


def scope_mismatches(
    *,
    approval_intent_id: str,
    approval_operation: Operation,
    scope: Mapping[str, object],
    intent_id: str,
    operation: Operation,
    args: Mapping[str, object],
) -> tuple[str, ...]:
    """Return the scope fields a write breaks; empty when the write is in scope (0002, rule 5).

    A write is in scope when every named field matches, and its quantity and amount do not
    exceed the maxima. When the operations differ, the other fields cannot be compared, so
    only `intent_id` and `operation` are named.
    """
    broken: list[str] = []
    if intent_id != approval_intent_id:
        broken.append("intent_id")
    if operation is not approval_operation:
        broken.append("operation")
        return tuple(broken)
    for name in SCOPE_FIELDS[operation]:
        if name == "max_quantity":
            within = _integer(args, "quantity") <= _integer(scope, "max_quantity")
        elif name == "max_amount_minor":
            within = order_amount_minor(args) <= _integer(scope, "max_amount_minor")
        else:
            within = args.get(name) == scope.get(name)
        if not within:
            broken.append(name)
    return tuple(broken)


def approval_status_at(
    *, decision: Decision | None, revoked: bool, expires_at: int | None, vt: int
) -> ApprovalStatus:
    """An approval's status at virtual time `vt` (0002, rules 8 and 9).

    Revocation is permanent and wins over expiry. An approved approval is expired from
    `expires_at` on, because an effect is authorized only if it commits before then
    (0004, property 2).
    """
    if decision is None:
        if revoked:
            raise ValueError("only an approved approval can be revoked")
        return ApprovalStatus.PENDING
    if decision is Decision.DENY:
        if revoked:
            raise ValueError("only an approved approval can be revoked")
        return ApprovalStatus.DENIED
    if revoked:
        return ApprovalStatus.REVOKED
    if expires_at is None:
        raise ValueError("an approved approval has an expiry")
    return ApprovalStatus.EXPIRED if vt >= expires_at else ApprovalStatus.APPROVED
