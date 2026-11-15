"""Authority rules of record 0002 (`fivefold.domain.authority`)."""

from __future__ import annotations

import pytest

from fivefold.domain.authority import (
    ApprovalStatus,
    Decision,
    PolicyDecision,
    approval_status_at,
    policy_decision,
    scope_mismatches,
)
from fivefold.domain.rules import Operation

PO = Operation.SUBMIT_PURCHASE_ORDER
RESERVE = Operation.RESERVE_INVENTORY
POLICY = {"allowlist": ["S-1", "S-2"], "per_order_ceiling_minor": 100_000}
SCOPE: dict[str, object] = {
    "supplier_id": "S-1",
    "sku": "W-100",
    "max_quantity": 100,
    "max_amount_minor": 54_000,
    "currency": "USD",
    "cost_center": "CC-OPS",
}
ORDER: dict[str, object] = {
    "supplier_id": "S-1",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 540,
    "currency": "USD",
    "cost_center": "CC-OPS",
}


def _decide(operation: Operation, **scope: object) -> PolicyDecision:
    return policy_decision(
        operation,
        {**SCOPE, **scope},
        allowlist=["S-1", "S-2"],
        per_order_ceiling_minor=100_000,
        auto_approve_up_to_minor=60_000,
    )


@pytest.mark.parametrize(
    ("scope", "decision"),
    [
        ({}, PolicyDecision.APPROVE),
        ({"max_amount_minor": 60_000}, PolicyDecision.APPROVE),
        ({"max_amount_minor": 60_001}, PolicyDecision.QUEUE),
        ({"max_amount_minor": 100_000}, PolicyDecision.QUEUE),
        ({"max_amount_minor": 100_001}, PolicyDecision.DENY),
        ({"supplier_id": "S-3"}, PolicyDecision.DENY),
        ({"supplier_id": "S-3", "max_amount_minor": 70_000}, PolicyDecision.DENY),
    ],
)
def test_policy_decides_a_purchase_order_on_arrival(
    scope: dict[str, object], decision: PolicyDecision
) -> None:
    assert _decide(PO, **scope) is decision


def test_policy_approves_every_reservation() -> None:
    scope = {"warehouse_id": "WH-1", "sku": "W-100", "max_quantity": 10_000}
    decision = policy_decision(
        RESERVE, scope, allowlist=[], per_order_ceiling_minor=0, auto_approve_up_to_minor=0
    )
    assert decision is PolicyDecision.APPROVE


def _mismatches(**changes: object) -> tuple[str, ...]:
    args = {**ORDER, **changes}
    return scope_mismatches(
        approval_intent_id="REQ-1",
        approval_operation=PO,
        scope=SCOPE,
        intent_id=str(args.pop("intent_id", "REQ-1")),
        operation=PO,
        args=args,
    )


def test_a_matching_order_is_in_scope() -> None:
    assert _mismatches() == ()
    assert _mismatches(quantity=50) == ()


@pytest.mark.parametrize(
    ("changes", "broken"),
    [
        ({"intent_id": "REQ-2"}, ("intent_id",)),
        ({"supplier_id": "S-2"}, ("supplier_id",)),
        ({"sku": "W-200"}, ("sku",)),
        ({"quantity": 101}, ("max_quantity", "max_amount_minor")),
        ({"unit_price_minor": 541}, ("max_amount_minor",)),
        ({"currency": "EUR"}, ("currency",)),
        ({"cost_center": "CC-RND"}, ("cost_center",)),
    ],
)
def test_each_scope_field_is_checked(changes: dict[str, object], broken: tuple[str, ...]) -> None:
    assert _mismatches(**changes) == broken


def test_a_reservation_scope_checks_warehouse_sku_and_quantity() -> None:
    scope = {"warehouse_id": "WH-1", "sku": "W-100", "max_quantity": 30}
    args = {"warehouse_id": "WH-2", "sku": "W-100", "quantity": 31}
    broken = scope_mismatches(
        approval_intent_id="REQ-1",
        approval_operation=RESERVE,
        scope=scope,
        intent_id="REQ-1",
        operation=RESERVE,
        args=args,
    )
    assert broken == ("warehouse_id", "max_quantity")


def test_another_operation_is_out_of_scope_without_comparing_fields() -> None:
    broken = scope_mismatches(
        approval_intent_id="REQ-1",
        approval_operation=PO,
        scope=SCOPE,
        intent_id="REQ-9",
        operation=RESERVE,
        args={"warehouse_id": "WH-1", "sku": "W-100", "quantity": 1},
    )
    assert broken == ("intent_id", "operation")


@pytest.mark.parametrize(
    ("decision", "revoked", "vt", "status"),
    [
        (None, False, 0, ApprovalStatus.PENDING),
        (Decision.DENY, False, 0, ApprovalStatus.DENIED),
        (Decision.APPROVE, False, 4_999, ApprovalStatus.APPROVED),
        (Decision.APPROVE, False, 5_000, ApprovalStatus.EXPIRED),
        (Decision.APPROVE, True, 0, ApprovalStatus.REVOKED),
        (Decision.APPROVE, True, 9_000, ApprovalStatus.REVOKED),
    ],
)
def test_an_approvals_status_at_a_time(
    decision: Decision | None, revoked: bool, vt: int, status: ApprovalStatus
) -> None:
    expires_at = 5_000 if decision is Decision.APPROVE else None
    found = approval_status_at(decision=decision, revoked=revoked, expires_at=expires_at, vt=vt)
    assert found is status


@pytest.mark.parametrize("decision", [None, Decision.DENY])
def test_only_an_approved_approval_can_be_revoked(decision: Decision | None) -> None:
    with pytest.raises(ValueError, match="only an approved approval"):
        approval_status_at(decision=decision, revoked=True, expires_at=None, vt=0)


def test_an_approved_approval_has_an_expiry() -> None:
    with pytest.raises(ValueError, match="has an expiry"):
        approval_status_at(decision=Decision.APPROVE, revoked=False, expires_at=None, vt=0)
