"""Records to JSON and back, as the Postgres store keeps them in jsonb columns."""

from __future__ import annotations

import json
from types import MappingProxyType

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fivefold.domain.contracts import Contract
from fivefold.gateway.codec import (
    CodecError,
    args_from_json,
    basis_to_json,
    check_from_columns,
    from_json,
    mapping_from_json,
    result_from_json,
    result_to_json,
    to_json,
    versions_from_json,
)
from fivefold.gateway.records import (
    ActionResult,
    ApprovalStatus,
    BasisCheck,
    CheckRecord,
    Claim,
    ClaimConflict,
    ClaimStatus,
    Reconcile,
    RecordedAction,
    Refusal,
    RefusalReason,
    ReportAccepted,
    StaleFact,
    frozen,
)
from fivefold.gateway.state_machine import (
    ActionState,
    CheckKind,
    EscalationTrigger,
    Reason,
    ReconcileHow,
)

STALE = StaleFact("quote/S-1/W-100", 1, 2, ("unit_price_minor", "status"))


def through_json(value: object) -> object:
    """What a jsonb column gives back: JSON text parsed again."""
    return json.loads(json.dumps(to_json(value)))


def test_event_details_round_trip() -> None:
    details = frozen(
        {
            "attempt_no": 2,
            "effect_id": None,
            "listed": ("PO-0001", "PO-0002"),
            "stale": (STALE,),
            "trigger": EscalationTrigger.RETRY_CHECK_FAILED,
            "approval": ApprovalStatus.REVOKED,
            "answered": False,
            "from": frozen({"quote": 1}),
        }
    )
    decoded = mapping_from_json(through_json(details))
    assert decoded == details
    # A StrEnum member equals its text, so equality alone would not show the enum is back.
    assert type(decoded["trigger"]) is EscalationTrigger
    assert type(decoded["approval"]) is ApprovalStatus
    assert isinstance(decoded["listed"], tuple)
    assert isinstance(decoded["stale"], tuple) and decoded["stale"][0] == STALE
    assert isinstance(decoded, MappingProxyType)


@pytest.mark.parametrize(
    "result",
    [
        ActionResult(
            "A-0001", "REQ-1", ActionState.CONFIRMED, "PO-0001", Reason.COMMITTED, (), None
        ),
        ActionResult(
            "A-0002",
            "REQ-2",
            ActionState.OUTCOME_UNKNOWN,
            None,
            Reason.TIMEOUT,
            (),
            Reconcile(Contract.C0, ReconcileHow.ESCALATED_TO_HUMAN, "ESC-0002", "may exist"),
        ),
        ActionResult(
            "A-0003", "REQ-1", ActionState.REJECTED, None, Reason.STALE_INPUT, (STALE,), None
        ),
        Refusal(RefusalReason.RUN_CLOSED),
        Refusal(
            RefusalReason.INTENT_BUSY,
            "A-0001",
            ActionState.DISPATCHED,
            args=frozen({"quantity": 100, "sku": "W-100"}),
        ),
        Refusal(RefusalReason.INTENT_DONE, "A-0001", ActionState.CONFIRMED, "PO-0001"),
        Refusal(
            RefusalReason.REPORT_INCONSISTENT,
            conflicts=(
                ClaimConflict(
                    "REQ-1",
                    ClaimStatus.DONE,
                    ("PO-0009",),
                    (RecordedAction("A-0001", ActionState.CONFIRMED, ("PO-0001", "PO-0002")),),
                ),
            ),
        ),
        ReportAccepted(
            (
                Claim("REQ-1", ClaimStatus.DONE, ("PO-0001",), "Ordered."),
                Claim("REQ-2", ClaimStatus.UNKNOWN),
            )
        ),
    ],
)
def test_responses_round_trip(result: ActionResult | Refusal | ReportAccepted) -> None:
    assert result_from_json(through_json(result_to_json(result))) == result


@pytest.mark.parametrize(
    "check",
    [
        CheckRecord(
            CheckKind.FULL,
            ApprovalStatus.APPROVED,
            BasisCheck(True, True, frozen({"quote": 1}), ()),
            7,
            30,
            approval_expires_at=3_600_030,
        ),
        CheckRecord(
            CheckKind.EXACT_ONLY,
            ApprovalStatus.REVOKED,
            BasisCheck(False, None, stale=(STALE,)),
            9,
            40,
        ),
        CheckRecord(CheckKind.EXEMPT, None, None, 11, 50),
        CheckRecord(
            CheckKind.AUTHORITY_ONLY, ApprovalStatus.APPROVED, None, 13, 60, approval_expires_at=90
        ),
        CheckRecord(CheckKind.AUTHORITY_ONLY, ApprovalStatus.REVOKED, None, 15, 70),
    ],
)
def test_check_records_round_trip(check: CheckRecord) -> None:
    stored_basis = json.loads(json.dumps(basis_to_json(check.basis)))
    approval = None if check.approval is None else str(check.approval)
    rebuilt = check_from_columns(
        str(check.kind),
        approval,
        stored_basis,
        check.seen_seq,
        check.check_vt,
        check.approval_expires_at,
    )
    assert rebuilt == check


def test_an_authority_only_check_records_the_approval_and_no_basis() -> None:
    """G1's check (0008) reads no fact, so it records no basis. It records the approval it
    found, with the expiry exactly when approved, and passes only for an approval that
    outlives the send by the settle time (0010)."""
    kind, approved = CheckKind.AUTHORITY_ONLY, ApprovalStatus.APPROVED
    with pytest.raises(ValueError, match="no basis"):
        CheckRecord(kind, approved, BasisCheck(True, True), 1, 1, approval_expires_at=100)
    with pytest.raises(ValueError, match="no basis"):
        CheckRecord(kind, None, None, 1, 1)
    with pytest.raises(ValueError, match="expiry"):
        CheckRecord(kind, approved, None, 1, 1)
    check = CheckRecord(kind, approved, None, 1, 1, approval_expires_at=100)
    assert check.passes(10, 50) and not check.passes(50, 50)
    assert not CheckRecord(kind, ApprovalStatus.REVOKED, None, 1, 1).passes(10, 50)


def test_arguments_and_bases_keep_their_types() -> None:
    assert args_from_json({"sku": "W-100", "quantity": 100}) == {"sku": "W-100", "quantity": 100}
    assert versions_from_json({"quote": 3}) == {"quote": 3}
    for bad in ({"quantity": 1.5}, {"flag": True}, ["not", "an", "object"]):
        with pytest.raises(CodecError):
            args_from_json(bad)
    with pytest.raises(CodecError):
        versions_from_json({"quote": "3"})


def test_malformed_data_is_refused() -> None:
    with pytest.raises(CodecError):
        to_json(object())
    with pytest.raises(CodecError):
        result_from_json({"result": "something else"})
    with pytest.raises(CodecError):
        result_from_json({"result": "action", "action_id": 1})
    with pytest.raises(CodecError):
        from_json(1.5)


json_scalars = st.none() | st.booleans() | st.integers(-(2**53), 2**53) | st.text(max_size=8)
stale_facts = st.builds(
    StaleFact,
    st.text(max_size=8),
    st.integers(0, 99),
    st.integers(0, 99),
    st.lists(st.text(max_size=5), max_size=3).map(tuple),
)
values = st.recursive(
    json_scalars | stale_facts,
    lambda inner: (
        st.lists(inner, max_size=3).map(tuple)
        | st.dictionaries(st.text(max_size=5), inner, max_size=3).map(frozen)
    ),
    max_leaves=10,
)


# Event details hold enum members under "trigger" and "approval", never other values.
detail_keys = st.text(max_size=8).filter(lambda key: key not in ("trigger", "approval"))


@given(st.dictionaries(detail_keys, values, max_size=4).map(frozen))
def test_any_details_round_trip(details: object) -> None:
    assert mapping_from_json(through_json(details)) == details
