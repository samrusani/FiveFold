"""The simulator API's models (`fivefold.domain.api`) and run configuration (`SimConfig`)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from fivefold.domain.api import (
    REQUESTS,
    ApiName,
    ApprovalAnswer,
    ApprovalReply,
    CheckName,
    FactRead,
    FailedCheck,
    PurchaseOrderScope,
    ReadReply,
    Refusal,
    RefusalReason,
    RequestApproval,
    ReservationScope,
    ReserveInventory,
    StatusReply,
    SubmitPurchaseOrder,
    WriteCommitted,
    WriteInProgress,
    WriteRejected,
    WriteRejectReason,
    WriteReply,
)
from fivefold.domain.authority import ApprovalStatus
from fivefold.domain.contracts import Capability, Contract, ContractParams
from fivefold.domain.rules import Operation
from fivefold.sim import SimConfig

ORDER: dict[str, Any] = {
    "intent_id": "REQ-1",
    "approval_id": "APR-0001",
    "basis": {"quote/S-1/W-100": 1, "budget/CC-OPS": 1},
    "supplier_id": "S-1",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 540,
    "currency": "USD",
    "cost_center": "CC-OPS",
}
SCOPE: dict[str, Any] = {
    "supplier_id": "S-1",
    "sku": "W-100",
    "max_quantity": 100,
    "max_amount_minor": 54_000,
    "currency": "USD",
    "cost_center": "CC-OPS",
}


def test_every_api_call_has_a_request_model() -> None:
    assert set(REQUESTS) == set(ApiName)
    for name, model in REQUESTS.items():
        assert vars(model)["api"] is name


def test_a_purchase_order_carries_its_business_args_in_record_0001s_order() -> None:
    order = SubmitPurchaseOrder.model_validate(ORDER)
    assert order.operation is Operation.SUBMIT_PURCHASE_ORDER
    assert list(order.business_args().items()) == [
        ("supplier_id", "S-1"),
        ("sku", "W-100"),
        ("quantity", 100),
        ("unit_price_minor", 540),
        ("currency", "USD"),
        ("cost_center", "CC-OPS"),
    ]
    assert (order.idempotency_key, order.client_ref) == (None, None)


def test_a_reservation_carries_its_business_args() -> None:
    reservation = ReserveInventory.model_validate(
        {
            "intent_id": "REQ-2",
            "approval_id": "APR-0002",
            "basis": {},
            "warehouse_id": "WH-1",
            "sku": "W-100",
            "quantity": 3,
            "idempotency_key": "A-0002",
        }
    )
    assert reservation.operation is Operation.RESERVE_INVENTORY
    assert reservation.business_args() == {"warehouse_id": "WH-1", "sku": "W-100", "quantity": 3}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", 100.0),
        ("quantity", True),
        ("quantity", "100"),
        ("quantity", 0),
        ("unit_price_minor", -1),
        ("currency", "usd"),
        ("supplier_id", "S 1"),
        ("supplier_id", "S/1"),
        ("supplier_id", ""),
        ("intent_id", "REQ-1/2"),
        ("basis", {"price/S-1": 1}),
        ("basis", {"quote/S-1/W-100": 0}),
        ("basis", {"quote/S-1/W-100": 1.0}),
        ("idempotency_key", "A 1"),
        ("client_ref", "bad\x00ref"),
        ("client_ref", ""),
        ("surprise", 1),
    ],
)
def test_a_malformed_write_is_refused(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        SubmitPurchaseOrder.model_validate({**ORDER, field: value})


def test_a_write_carries_its_intent_approval_and_basis() -> None:
    for field in ("intent_id", "approval_id", "basis"):
        data = dict(ORDER)
        del data[field]
        with pytest.raises(ValidationError, match="Field required"):
            SubmitPurchaseOrder.model_validate(data)


def test_a_client_ref_may_hold_an_attempt_id() -> None:
    assert SubmitPurchaseOrder.model_validate({**ORDER, "client_ref": "A-0001/2"}).client_ref


def test_an_approval_request_needs_the_scope_of_its_operation() -> None:
    request = RequestApproval.model_validate(
        {"intent_id": "REQ-1", "operation": "submit_purchase_order", "scope": SCOPE}
    )
    assert isinstance(request.scope, PurchaseOrderScope)
    with pytest.raises(ValidationError, match="needs a ReservationScope"):
        RequestApproval.model_validate(
            {"intent_id": "REQ-1", "operation": "reserve_inventory", "scope": SCOPE}
        )
    with pytest.raises(ValidationError):
        RequestApproval.model_validate(
            {
                "intent_id": "REQ-1",
                "operation": "submit_purchase_order",
                "scope": {**SCOPE, "warehouse_id": "WH-1"},
            }
        )


def test_an_approval_answer_lists_its_request_fields() -> None:
    answer = ApprovalAnswer(
        approval_id="APR-0001",
        intent_id="REQ-2",
        operation=Operation.RESERVE_INVENTORY,
        scope=ReservationScope(warehouse_id="WH-1", sku="W-100", max_quantity=30),
        status=ApprovalStatus.PENDING,
        requested_vt=0,
    )
    assert answer.request_fields() == {
        "intent_id": "REQ-2",
        "operation": "reserve_inventory",
        "warehouse_id": "WH-1",
        "sku": "W-100",
        "max_quantity": 30,
    }
    with pytest.raises(ValidationError, match="needs a PurchaseOrderScope"):
        ApprovalAnswer(
            approval_id="APR-0001",
            intent_id="REQ-2",
            operation=Operation.SUBMIT_PURCHASE_ORDER,
            scope=answer.scope,
            status=ApprovalStatus.PENDING,
            requested_vt=0,
        )


def test_failed_checks_come_with_precondition_failed_only() -> None:
    check = FailedCheck(check=CheckName.APPROVAL_REVOKED)
    WriteRejected(reason=WriteRejectReason.PRECONDITION_FAILED, failed_checks=[check])
    with pytest.raises(ValidationError, match="failed checks come with precondition_failed"):
        WriteRejected(reason=WriteRejectReason.PRECONDITION_FAILED)
    with pytest.raises(ValidationError, match="failed checks come with precondition_failed"):
        WriteRejected(reason=WriteRejectReason.FORBIDDEN, failed_checks=[check])


def test_input_checks_name_their_fact_and_only_they_do() -> None:
    FailedCheck(check=CheckName.STALE_INPUT, key="quote/S-1/W-100", cited_version=1)
    with pytest.raises(ValidationError, match="input checks name a fact key"):
        FailedCheck(check=CheckName.UNDECLARED_INPUT)
    with pytest.raises(ValidationError, match="input checks name a fact key"):
        FailedCheck(check=CheckName.APPROVAL_USED, key="quote/S-1/W-100")


COMMITTED = WriteCommitted(
    effect_id="PO-0001",
    replay=True,
    intent_id="REQ-1",
    operation=Operation.SUBMIT_PURCHASE_ORDER,
    args={"supplier_id": "S-1", "quantity": 100},
    committed_vt=40,
)
REJECTED = WriteRejected(
    reason=WriteRejectReason.PRECONDITION_FAILED,
    failed_checks=[
        FailedCheck(
            check=CheckName.STALE_INPUT,
            key="quote/S-1/W-100",
            cited_version=1,
            current_version=2,
            changed_fields=["unit_price_minor"],
        )
    ],
)


@pytest.mark.parametrize("answer", [COMMITTED, REJECTED, WriteInProgress(idempotency_key="A-1")])
def test_a_write_answer_parses_back_by_its_tag(answer: object) -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(WriteReply)
    assert adapter.validate_json(adapter.dump_json(answer)) == answer


def test_read_approval_and_status_replies_parse_by_their_tag() -> None:
    refusal = Refusal(reason=RefusalReason.NOT_FOUND, detail="no fact quote/S-9/W-100")
    fact = FactRead(key="suppliers", version=1, data={"suppliers": [{"supplier_id": "S-1"}]})
    reads: TypeAdapter[Any] = TypeAdapter(ReadReply)
    assert reads.validate_python(fact.model_dump()) == fact
    assert reads.validate_python(refusal.model_dump()) == refusal
    approvals: TypeAdapter[Any] = TypeAdapter(ApprovalReply)
    assert approvals.validate_python(refusal.model_dump()) == refusal
    statuses: TypeAdapter[Any] = TypeAdapter(StatusReply)
    with pytest.raises(ValidationError):
        statuses.validate_python(fact.model_dump())


def test_a_preset_sets_the_capabilities_of_its_level() -> None:
    config = SimConfig.preset(Contract.C3, credentials=["gateway"])
    assert config.capabilities == {Capability.IDEMPOTENCY_KEY, Capability.CONDITIONAL_WRITE}
    assert config.has(Capability.CONDITIONAL_WRITE)
    assert not config.has(Capability.STATUS)
    assert config.params == ContractParams()
    assert SimConfig.preset(Contract.C0, credentials=["agent"]).capabilities == frozenset()


def test_capabilities_combine_freely() -> None:
    config = SimConfig(
        capabilities=frozenset({Capability.STATUS, Capability.CONDITIONAL_WRITE}),
        credentials=frozenset({"gateway"}),
    )
    assert config.has(Capability.STATUS) and not config.has(Capability.IDEMPOTENCY_KEY)


@pytest.mark.parametrize("credentials", [[], [""], ["a\x00b"]])
def test_a_run_gives_named_callers_a_credential(credentials: list[str]) -> None:
    with pytest.raises(ValueError, match=r"credential|named"):
        SimConfig.preset(Contract.C2, credentials=credentials)
