"""The action key and the normal form of business arguments (0001, Checks)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fivefold.domain.identity import (
    InvalidArgs,
    action_key,
    canonical_json,
    normalize,
    operation_of,
)
from fivefold.domain.rules import Operation

PO = Operation.SUBMIT_PURCHASE_ORDER
RESERVE = Operation.RESERVE_INVENTORY

ORDER: dict[str, object] = {
    "supplier_id": "S-1",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 540,
    "currency": "usd",
    "cost_center": "CC-OPS",
    "based_on": ["R-0001", "R-0002"],
    "note": "Cheapest acceptable quote.",
}


def key(args: dict[str, object] = ORDER, intent_id: str = "REQ-1") -> str:
    return action_key("TASK-dev-001", intent_id, PO, normalize(PO, args))


def test_the_keys_are_pinned_by_golden_vectors() -> None:
    """The key must not drift across processes or library versions (0001, Checks).

    The digests were computed with `sha256sum` from the canonical JSON written out by
    hand, not by this module.
    """
    assert key() == "b3dd8164430ffd7dcc7538ae490fda47b1cf087495fa31967b11700a39da67df"
    reservation = normalize(RESERVE, {"warehouse_id": "WH-1", "sku": "W-100", "quantity": 5})
    assert action_key("TASK-dev-001", "REQ-2", RESERVE, reservation) == (
        "36d93b480a05c591b87fb8dc2f1c0beea260c979224c31e76a8a16d089ae8423"
    )


def test_every_business_argument_changes_the_key_and_volatile_ones_do_not() -> None:
    base = key()
    for name, other in [
        ("supplier_id", "S-2"),
        ("sku", "W-200"),
        ("quantity", 101),
        ("unit_price_minor", 541),
        ("currency", "EUR"),
        ("cost_center", "CC-RND"),
    ]:
        assert key({**ORDER, name: other}) != base, name
    assert key({**ORDER, "based_on": ["R-0009"], "note": "other", "extra": 1}) == base
    assert key(intent_id="REQ-2") != base


def test_currency_is_uppercased_and_ids_are_never_rewritten() -> None:
    assert normalize(PO, ORDER)["currency"] == "USD"
    assert key({**ORDER, "currency": "USD"}) == key()
    # An id is kept exactly as written: a different spelling is a different id.
    assert normalize(PO, {**ORDER, "supplier_id": "s-1"})["supplier_id"] == "s-1"
    assert key({**ORDER, "supplier_id": "s-1"}) != key()
    for name, bad in [
        ("supplier_id", " S-1"),
        ("supplier_id", "S-1 "),
        ("supplier_id", "S-1\n"),
        ("sku", ""),
        ("sku", "W/100"),
        ("cost_center", "CC OPS"),
        ("currency", "US"),
        ("currency", "U5D"),
    ]:
        with pytest.raises(InvalidArgs, match=name):
            normalize(PO, {**ORDER, name: bad})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"quantity": 1.5}, "quantity must be an integer"),
        ({"quantity": True}, "quantity must be an integer"),
        ({"quantity": "100"}, "quantity must be an integer"),
        ({"quantity": 0}, "quantity must be at least 1"),
        ({"unit_price_minor": -1}, "unit_price_minor must be at least 0"),
        ({"sku": 100}, "sku must be text"),
    ],
)
def test_floats_booleans_and_wrong_types_are_refused(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(InvalidArgs, match=message):
        normalize(PO, {**ORDER, **change})


def test_a_missing_argument_or_an_unknown_operation_is_refused() -> None:
    args = {k: v for k, v in ORDER.items() if k != "sku"}
    with pytest.raises(InvalidArgs, match="sku is required"):
        normalize(PO, args)
    with pytest.raises(InvalidArgs, match="unknown operation"):
        operation_of("cancel_order")
    assert operation_of("reserve_inventory") is RESERVE


def test_canonical_json_refuses_anything_but_text_and_integers() -> None:
    for bad in (1.5, True, None, {"a": 2.0}, [1, None]):
        with pytest.raises(InvalidArgs):
            canonical_json(bad)
    assert canonical_json({"é": "ü", "b": [1, "x"]}) == '{"b":[1,"x"],"é":"ü"}'


@given(st.dictionaries(st.text(max_size=5), st.integers() | st.text(max_size=5), max_size=5))
def test_canonical_json_is_the_same_for_any_key_order(value: dict[str, int | str]) -> None:
    reordered = dict(reversed(list(value.items())))
    assert canonical_json(value) == canonical_json(reordered)
