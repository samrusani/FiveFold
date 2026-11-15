"""The JSON the agent receives, checked against the examples in record 0003."""

from __future__ import annotations

import json

from fivefold.domain.contracts import Contract
from fivefold.gateway import wire
from fivefold.gateway.records import (
    ActionResult,
    Claim,
    ClaimConflict,
    ClaimStatus,
    ReadRecord,
    Reconcile,
    RecordedAction,
    Refusal,
    RefusalReason,
    ReportAccepted,
    StaleFact,
)
from fivefold.gateway.state_machine import ActionState, Reason, ReconcileHow

S = ActionState


def test_an_unknown_outcome_matches_the_record_0003_example() -> None:
    result = ActionResult(
        "A-0003",
        "REQ-1",
        S.OUTCOME_UNKNOWN,
        None,
        Reason.TIMEOUT,
        (),
        Reconcile(
            Contract.C0,
            ReconcileHow.ESCALATED_TO_HUMAN,
            "ESC-0001",
            "A purchase order for REQ-1 at S-1 may or may not exist.",
        ),
    )
    example = """
    {"result": "action", "action_id": "A-0003", "intent_id": "REQ-1", "state": "outcome_unknown",
     "effect_id": null, "reason": "timeout", "stale": [],
     "reconcile": {"contract": "C0", "how": "escalated_to_human", "escalation_id": "ESC-0001",
                   "what": "A purchase order for REQ-1 at S-1 may or may not exist."}}
    """
    assert wire.outcome(result) == json.loads(example)


def test_a_busy_intent_matches_the_record_0003_example_plus_the_args_of_0001() -> None:
    refused = Refusal(
        RefusalReason.INTENT_BUSY, "A-0001", S.DISPATCHED, args={"sku": "W-100", "quantity": 5}
    )
    assert wire.outcome(refused) == {
        "result": "refused",
        "reason": "intent_busy",
        "details": {
            "action_id": "A-0001",
            "state": "dispatched",
            "args": {"sku": "W-100", "quantity": 5},
        },
    }


def test_reconcile_appears_only_with_an_unknown_outcome() -> None:
    confirmed = ActionResult("A-0001", "REQ-1", S.CONFIRMED, "PO-0001", Reason.COMMITTED, (), None)
    shaped = wire.action(confirmed)
    assert "reconcile" not in shaped
    assert (shaped["state"], shaped["effect_id"]) == ("confirmed", "PO-0001")


def test_a_stale_rejection_names_each_fact() -> None:
    stale = StaleFact("quote/S-1/W-100", 1, 2, ("unit_price_minor",))
    rejected = ActionResult("A-0002", "REQ-1", S.REJECTED, None, Reason.STALE_INPUT, (stale,), None)
    assert wire.action(rejected)["stale"] == [
        {
            "key": "quote/S-1/W-100",
            "cited_version": 1,
            "current_version": 2,
            "changed_fields": ["unit_price_minor"],
        }
    ]


def test_each_refusal_reason_carries_its_details() -> None:
    cases = [
        (Refusal(RefusalReason.INTENT_DONE, "A-0001", S.CONFIRMED, "PO-0001"),
         {"action_id": "A-0001", "effect_id": "PO-0001"}),
        (Refusal(RefusalReason.UNKNOWN_READ, unknown_reads=("R-0009",)), {"read_ids": ["R-0009"]}),
        (Refusal(RefusalReason.MISSING_BASIS, missing_keys=("budget/CC-OPS",)),
         {"missing": ["budget/CC-OPS"]}),
        (Refusal(RefusalReason.INVALID_ARGS, message="quantity must be an integer"),
         {"message": "quantity must be an integer"}),
        (Refusal(RefusalReason.RUN_CLOSED), {}),
        (Refusal(RefusalReason.UNKNOWN_INTENT), {}),
        (Refusal(RefusalReason.WRONG_OPERATION), {}),
    ]  # fmt: skip
    for refused, details in cases:
        assert wire.refusal(refused) == {
            "result": "refused",
            "reason": str(refused.reason),
            "details": details,
        }


def test_a_report_is_accepted_or_refused_with_each_conflict() -> None:
    accepted = ReportAccepted((Claim("REQ-1", ClaimStatus.NOT_DONE),))
    assert wire.report(accepted) == {"result": "accepted"}
    conflict = ClaimConflict(
        "REQ-1",
        ClaimStatus.NOT_DONE,
        (),
        (RecordedAction("A-0001", S.CONFIRMED, ("PO-0001",)),),
    )
    refused = Refusal(RefusalReason.REPORT_INCONSISTENT, conflicts=(conflict,))
    assert wire.report(refused)["details"] == {
        "conflicts": [
            {
                "intent_id": "REQ-1",
                "claimed": "not_done",
                "claimed_effect_ids": [],
                "recorded": [
                    {"action_id": "A-0001", "state": "confirmed", "effect_ids": ["PO-0001"]}
                ],
            }
        ]
    }


def test_a_read_returns_its_id_key_version_and_data() -> None:
    data = {"unit_price_minor": 540, "currency": "USD"}
    record = ReadRecord("R-0001", "quote/S-1/W-100", 3, data, 120)
    assert wire.read(record) == {
        "read_id": "R-0001",
        "key": "quote/S-1/W-100",
        "version": 3,
        "data": data,
    }


def test_every_shape_is_plain_json() -> None:
    shapes = [
        wire.read(ReadRecord("R-0001", "k", 1, {"a": (1, "b"), "c": {"d": ()}}, 0)),
        wire.refusal(Refusal(RefusalReason.INTENT_BUSY, "A-0001", S.PROPOSED, args={"q": 1})),
    ]
    for shape in shapes:
        assert json.loads(json.dumps(shape)) == shape
