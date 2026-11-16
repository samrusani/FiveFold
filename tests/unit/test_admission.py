"""Admission: reads, citations, the order of the checks and the record of every write call.

Decision record 0001, items 13 to 17. Every test runs in memory and, with PostgreSQL, on
both stores side by side (`make_store`).
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.records import (
    ActionResult,
    Admission,
    Refusal,
    RefusalReason,
    WriteCall,
)
from fivefold.gateway.state_machine import GatewaySettings
from fivefold.gateway.twin import TwinStore

SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
)
INTENTS = {"REQ-1": "submit_purchase_order", "REQ-2": "reserve_inventory"}
QUOTE, BUDGET = "quote/S-1/W-100", "budget/CC-OPS"
DEPENDENCIES = (QUOTE, BUDGET)

Store = MemoryStore | TwinStore


@pytest.fixture
def store(make_store: Callable[..., Store]) -> Store:
    return make_store(task_id="TASK-1", intents=INTENTS, contract=Contract.C2, settings=SETTINGS)


def call(
    based_on: tuple[str, ...],
    *,
    intent: str = "REQ-1",
    operation: str = "submit_purchase_order",
    key: str = "k1",
) -> WriteCall:
    return WriteCall(
        intent, operation, "supplier/S-1", {"quantity": 5}, key, based_on, DEPENDENCIES
    )


def refusal(result: ActionResult | Refusal) -> RefusalReason:
    assert isinstance(result, Refusal)
    return result.reason


def test_reads_are_numbered_from_one(store: Store) -> None:
    first = store.record_read(QUOTE, 3, {"unit_price_minor": 540, "tags": ["a"]}, 10)
    second = store.record_read(BUDGET, 1, {"limit_minor": 100_000}, 10)
    assert (first.read_id, second.read_id) == ("R-0001", "R-0002")
    assert store.snapshot().reads["R-0001"] == first
    assert (first.key, first.version, first.vt) == (QUOTE, 3, 10)
    assert dict(first.data) == {"unit_price_minor": 540, "tags": ("a",)}
    assert store.read_of(QUOTE, 3) == first and store.read_of(QUOTE, 4) is None


def test_the_basis_is_each_dependency_at_its_newest_cited_version(store: Store) -> None:
    old_quote = store.record_read(QUOTE, 1, {}, 0).read_id
    new_quote = store.record_read(QUOTE, 2, {}, 0).read_id
    budget = store.record_read(BUDGET, 4, {}, 0).read_id
    extra = store.record_read("quote/S-2/W-100", 9, {}, 0).read_id
    result = store.propose(call((new_quote, budget, old_quote, extra)), 1)
    assert isinstance(result, ActionResult)
    action = store.snapshot().actions[result.action_id]
    assert dict(action.basis) == {QUOTE: 2, BUDGET: 4}, "extra citations stay out of the basis"
    (record,) = store.snapshot().proposals
    assert record.based_on == (new_quote, budget, old_quote, extra), "but are recorded"
    assert (record.admission, record.reason, record.action_id) == (
        Admission.ADMITTED,
        None,
        result.action_id,
    )


def test_unknown_reads_and_missing_dependencies_are_named(store: Store) -> None:
    quote = store.record_read(QUOTE, 1, {}, 0).read_id
    unknown = store.propose(call((quote, "R-0042", "R-0042", "R-0043")), 1)
    assert isinstance(unknown, Refusal) and unknown.reason is RefusalReason.UNKNOWN_READ
    assert unknown.unknown_reads == ("R-0042", "R-0043")
    missing = store.propose(call((quote,)), 2)
    assert isinstance(missing, Refusal) and missing.reason is RefusalReason.MISSING_BASIS
    assert missing.missing_keys == (BUDGET,)
    assert not store.snapshot().actions


def test_the_checks_run_in_the_order_of_0001_item_17(store: Store) -> None:
    """Each call fails two checks; the earlier one decides the refusal."""
    quote = store.record_read(QUOTE, 1, {}, 0).read_id
    budget = store.record_read(BUDGET, 1, {}, 0).read_id
    assert refusal(store.propose(call(("R-0099",), intent="REQ-9"), 1)) is (
        RefusalReason.UNKNOWN_INTENT
    )
    assert refusal(store.propose(call(("R-0099",), intent="REQ-2"), 2)) is (
        RefusalReason.WRONG_OPERATION
    )
    assert refusal(store.propose(call(("R-0099",)), 3)) is RefusalReason.UNKNOWN_READ
    admitted = store.propose(call((quote, budget)), 4)
    assert isinstance(admitted, ActionResult)
    # A different key would be refused as intent_busy, but the basis check comes first.
    assert refusal(store.propose(call((quote,), key="k2"), 5)) is RefusalReason.MISSING_BASIS
    assert refusal(store.propose(call((quote, budget), key="k2"), 6)) is RefusalReason.INTENT_BUSY
    store.close_run(7)
    assert refusal(store.propose(call(("R-0099",)), 8)) is RefusalReason.RUN_CLOSED
    reasons = [(str(p.admission), p.reason) for p in store.snapshot().proposals]
    assert reasons == [
        ("refused", RefusalReason.UNKNOWN_INTENT),
        ("refused", RefusalReason.WRONG_OPERATION),
        ("refused", RefusalReason.UNKNOWN_READ),
        ("admitted", None),
        ("refused", RefusalReason.MISSING_BASIS),
        ("refused", RefusalReason.INTENT_BUSY),
        ("refused", RefusalReason.RUN_CLOSED),
    ]


def test_every_refusal_is_logged_as_the_tools_response(store: Store) -> None:
    store.propose(call(("R-0099",), intent="REQ-9"), 1)
    (logged,) = store.snapshot().tool_calls
    assert logged.tool == "submit_purchase_order"
    assert isinstance(logged.result, Refusal)
    assert logged.result.reason is RefusalReason.UNKNOWN_INTENT


def test_an_attaching_call_is_recorded_as_attached(store: Store) -> None:
    quote = store.record_read(QUOTE, 1, {}, 0).read_id
    budget = store.record_read(BUDGET, 1, {}, 0).read_id
    first = store.propose(call((quote, budget)), 1)
    again = store.propose(call((budget, quote)), 2)
    assert isinstance(first, ActionResult) and isinstance(again, ActionResult)
    assert again.action_id == first.action_id
    admissions = [p.admission for p in store.snapshot().proposals]
    assert admissions == [Admission.ADMITTED, Admission.ATTACHED]


def test_a_call_that_breaks_the_schema_is_recorded_as_sent(store: Store) -> None:
    args: dict[str, object] = {
        "intent_id": "REQ-1",
        "quantity": 1.5,
        "sku": "W\x00100",
        "price": math.nan,
        "based_on": ["R-0001", "R-0002"],
        "nested": {"a": [1, 2]},
    }
    result = store.refuse_invalid("submit_purchase_order", args, "quantity must be an integer", 3)
    assert result == Refusal(RefusalReason.INVALID_ARGS, message="quantity must be an integer")
    (record,) = store.snapshot().proposals
    assert (record.intent_id, record.admission, record.reason) == (
        "REQ-1",
        Admission.REFUSED,
        RefusalReason.INVALID_ARGS,
    )
    assert record.based_on == ("R-0001", "R-0002")
    assert record.args["sku"] == "W�100" and record.args["price"] == "nan"
    assert record.args["quantity"] == 1.5
    (logged,) = store.snapshot().tool_calls
    assert logged.result == result


def test_a_schema_refusal_without_a_textual_intent_records_none(store: Store) -> None:
    store.refuse_invalid("reserve_inventory", {"intent_id": 7, "based_on": "R-1"}, "bad", 0)
    (record,) = store.snapshot().proposals
    assert (record.intent_id, record.based_on) == (None, ())


def test_large_exponent_numbers_match_jsonb(store: Store) -> None:
    args: dict[str, object] = {"intent_id": "REQ-1", "nested": [1e23, -1e23]}
    store.refuse_invalid("submit_purchase_order", args, "bad quantity", 3)
    (proposal,) = store.snapshot().proposals
    assert proposal.args["nested"] == (10**23, -(10**23))


def test_large_exponent_read_matches_jsonb_snapshot(store: Store) -> None:
    read = store.record_read(QUOTE, 1, {"value": 1e23}, 0)
    assert read.data["value"] == 1e23
    assert store.snapshot().reads[read.read_id].data["value"] == 10**23
