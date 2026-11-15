"""The final report's schema (0004, property 5; spec, Gateway)."""

from __future__ import annotations

import pytest

from fivefold.domain.report import ClaimStatus, InvalidReport, parse_report

INTENTS = ("REQ-1", "REQ-2")


def claim(intent_id: str, status: str = "not_done", **more: object) -> dict[str, object]:
    return {"intent_id": intent_id, "status": status, **more}


def errors(data: object) -> list[str]:
    with pytest.raises(InvalidReport) as caught:
        parse_report(data, INTENTS)
    return caught.value.errors


def test_a_report_with_one_claim_per_intent_parses() -> None:
    report = parse_report(
        {
            "claims": [
                claim("REQ-1", "done", effect_ids=["PO-0001"], note="Ordered from S-1."),
                claim("REQ-2", "unknown", note="Reply lost."),
            ]
        },
        INTENTS,
    )
    first, second = report.claims
    assert (first.status, first.effect_ids, first.note) == (
        ClaimStatus.DONE,
        ("PO-0001",),
        "Ordered from S-1.",
    )
    assert (second.status, second.effect_ids) == (ClaimStatus.UNKNOWN, ())


def test_each_intent_needs_exactly_one_claim() -> None:
    assert errors({"claims": [claim("REQ-1")]}) == ["REQ-2 has no claim"]
    assert errors({"claims": [claim("REQ-1"), claim("REQ-1"), claim("REQ-2")]}) == [
        "REQ-1 has more than one claim"
    ]
    assert errors({"claims": [claim("REQ-1"), claim("REQ-2"), claim("REQ-9")]}) == [
        "REQ-9 is not an intent of the task"
    ]


@pytest.mark.parametrize(
    ("bad", "where"),
    [
        (claim("REQ-1", "done"), "claims.0"),
        (claim("REQ-1", "not_done", effect_ids=["PO-0001"]), "claims.0"),
        (claim("REQ-1", "maybe"), "claims.0.status"),
        (claim("REQ-1", extra=1), "claims.0.extra"),
        (claim("REQ-1", note="a\x00b"), "claims.0.note"),
    ],
)
def test_a_broken_claim_is_named_by_its_place(bad: dict[str, object], where: str) -> None:
    found = errors({"claims": [bad, claim("REQ-2")]})
    assert len(found) == 1 and found[0].startswith(where), found


def test_a_report_must_be_an_object_with_claims() -> None:
    assert errors([]) != []
    assert errors({"claims": [claim("REQ-1"), claim("REQ-2")], "extra": True}) == [
        "extra: Extra inputs are not permitted"
    ]
