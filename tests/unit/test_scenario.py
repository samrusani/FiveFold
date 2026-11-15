"""The scenario format (0009), its loader, and the feasibility check (0004, property 4)."""

from __future__ import annotations

import copy
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from fivefold.domain.authority import Decision, PolicyDecision
from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.rules import Operation
from fivefold.domain.scenario import (
    BudgetFact,
    Intent,
    Resolver,
    Scenario,
    ScenarioError,
    feasible_together,
    json_schema_text,
    load_scenario,
    load_scenario_file,
    options,
    parse_yaml,
    scenario_from_data,
)

ROOT = Path(__file__).resolve().parents[2]
DEV_FILE = ROOT / "scenarios" / "dev" / "dev-001-single-po.yaml"
SCHEMA_FILE = ROOT / "scenarios" / "scenario.schema.json"

Data = dict[str, Any]


def test_the_dev_scenario_validates(dev_data: Data) -> None:
    scenario = scenario_from_data(dev_data)
    assert scenario.id == "dev-001-single-po"
    assert scenario.intent("REQ-1").operation is Operation.SUBMIT_PURCHASE_ORDER
    assert scenario.contracts == [Contract.C0, Contract.C1, Contract.C2, Contract.C3]
    assert scenario.authority.resolver == "none"
    assert scenario.params.contract_params() == ContractParams(50, 10_000, 0)
    assert scenario.params.max_attempts == 5
    assert scenario.fact_values()["budget/CC-OPS"] == {
        "limit_minor": 100_000,
        "committed_minor": 20_000,
    }
    assert list(scenario.fact_values()) == sorted(dev_data["facts"])
    with pytest.raises(KeyError):
        scenario.intent("REQ-9")


def test_the_dev_scenario_file_holds_the_dev_data(dev_data: Data) -> None:
    """Runs once PyYAML is installed; the loader's YAML use is `parse_yaml` alone."""
    pytest.importorskip("yaml")
    assert parse_yaml(DEV_FILE.read_text(encoding="utf-8")) == dev_data
    assert load_scenario_file(DEV_FILE) == scenario_from_data(dev_data)


def test_the_loader_parses_yaml_with_safe_load(
    dev_data: Data, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def safe_load(text: str) -> Data:
        seen.append(text)
        return copy.deepcopy(dev_data)

    stub = types.ModuleType("yaml")
    stub.safe_load = safe_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yaml", stub)
    assert load_scenario("the text").id == "dev-001-single-po"
    assert seen == ["the text"]


def test_the_loader_says_when_pyyaml_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(ScenarioError, match="needs PyYAML"):
        parse_yaml("id: x")


def test_the_committed_json_schema_matches_the_model() -> None:
    expected = json_schema_text()
    assert SCHEMA_FILE.read_text(encoding="utf-8") == expected, (
        "regenerate it: python -m fivefold.domain.scenario > scenarios/scenario.schema.json"
    )


def test_the_json_schema_describes_the_format() -> None:
    schema = Scenario.model_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "id",
        "task_id",
        "title",
        "task",
        "intents",
        "facts",
        "authority",
        "contracts",
        "budget",
    }


def _set(path: str, value: object) -> Callable[[Data], None]:
    """A change that sets one value at a dotted path, with list indexes as numbers."""

    def change(data: Data) -> None:
        *parents, last = path.split(".")
        node: Any = data
        for part in parents:
            node = node[int(part)] if isinstance(node, list) else node[part]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value

    return change


def _drop(path: str) -> Callable[[Data], None]:
    def change(data: Data) -> None:
        *parents, last = path.split(".")
        node: Any = data
        for part in parents:
            node = node[part]
        del node[last]

    return change


def _second_intent(data: Data) -> None:
    data["intents"].append(copy.deepcopy(data["intents"][0]))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_set("surprise", 1), "Extra inputs are not permitted"),
        (_set("intents", []), "at least 1 item"),
        (_second_intent, "intent ids must be unique"),
        (_set("intents.0.id", "REQ/1"), "String should match pattern"),
        (_set("intents.0.operation", "reserve_inventory"), "needs ReservationAccept"),
        (_set("intents.0.required", "yes"), "valid boolean"),
        (_set("intents.0.accept.quantity", 100.0), "valid integer"),
        (_set("intents.0.accept.quantity", True), "valid integer"),
        (_set("intents.0.accept.quantity", 0), "greater than or equal to 1"),
        (_set("intents.0.accept.currency", "usd"), "String should match pattern"),
        (_set("intents.0.accept.suppliers", []), "at least 1 item"),
        (_set("facts.suppliers", {"suppliers": []}), "String should match pattern"),
        (_set("facts.price/S-1", {"limit_minor": 1, "committed_minor": 0}), "should match"),
        (
            _set("facts.quote/S-1/W-100", {"limit_minor": 1, "committed_minor": 0}),
            "needs the fields of QuoteFact",
        ),
        (_set("facts.budget/CC-OPS.committed_minor", -1), "greater than or equal to 0"),
        (_set("facts.budget/CC-OPS.extra", 1), "Extra inputs are not permitted"),
        (_drop("facts.quote/S-1/W-100.status"), "Field required"),
        (_set("contracts", ["C2", "C2"]), "contracts must be unique"),
        (_set("contracts", ["C4"]), "Input should be 'C0', 'C1', 'C2' or 'C3'"),
        (_set("contracts", []), "at least 1 item"),
        (_set("params.dispatch_timeout_ms", 100), "must exceed 2 x max_hop_latency_ms"),
        (_set("params.commit_deadline_ms", 0), "greater than or equal to 1"),
        (_set("params.unknown_ms", 1), "Extra inputs are not permitted"),
        (_set("authority.approver.0.match", {"price": 1}), "Input should be 'intent_id'"),
        (_set("authority.approver.0.decision", "maybe"), "Input should be 'approve' or 'deny'"),
        (_set("authority.resolver", "always"), "Input should be 'none'"),
        (_set("authority.resolver", {"after_ms": 1, "attests": "lies"}), "Input should be"),
        (_set("authority.approval_ttl_ms", 0), "greater than or equal to 1"),
        (_drop("budget"), "Field required"),
    ],
)
def test_a_scenario_that_breaks_the_format_is_refused(
    dev_data: Data, change: Callable[[Data], None], message: str
) -> None:
    change(dev_data)
    with pytest.raises(ScenarioError, match=message):
        scenario_from_data(dev_data)


def test_optional_sections_have_defaults(dev_data: Data) -> None:
    for name in ("faults", "params"):
        del dev_data[name]
    del dev_data["authority"]["approver"]
    del dev_data["authority"]["resolver"]
    del dev_data["intents"][0]["required"]
    scenario = scenario_from_data(dev_data)
    assert scenario.faults == []
    assert scenario.params.contract_params() == ContractParams()
    assert scenario.params.dispatch_timeout_ms == 2_000
    assert scenario.authority.approver == []
    assert scenario.authority.resolver == "none"
    assert scenario.intents[0].required is True


def test_a_resolver_can_attest_the_truth(dev_data: Data) -> None:
    dev_data["authority"]["resolver"] = {"after_ms": 30_000, "attests": "truth"}
    resolver = scenario_from_data(dev_data).authority.resolver
    assert resolver == Resolver(after_ms=30_000, attests="truth")


def test_a_random_fault_plan_is_kept_as_data(dev_data: Data) -> None:
    dev_data["faults"] = {"random": {"from": ["F2", "F4"], "max": 3}}
    assert scenario_from_data(dev_data).faults == {"random": {"from": ["F2", "F4"], "max": 3}}


# Feasibility ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        _set("intents.0.accept.max_unit_price_minor", 539),
        _set("authority.allowlist", ["S-3"]),
        _set("authority.per_order_ceiling_minor", 53_999),
        _set("intents.0.accept.quantity", 5),
        _set("intents.0.accept.currency", "EUR"),
        _set("intents.0.accept.suppliers", ["S-3"]),
        _set("intents.0.accept.cost_center", "CC-RND"),
        _set("facts.budget/CC-OPS.committed_minor", 46_001),
        _set("facts.budget/CC-OPS.limit_minor", 73_999),
    ],
)
def test_a_required_intent_with_no_allowed_option_is_infeasible(
    dev_data: Data, change: Callable[[Data], None]
) -> None:
    change(dev_data)
    with pytest.raises(ScenarioError, match="not feasible together"):
        scenario_from_data(dev_data)


def test_a_budget_that_covers_the_order_exactly_is_enough(dev_data: Data) -> None:
    dev_data["facts"]["budget/CC-OPS"]["limit_minor"] = 74_000
    scenario_from_data(dev_data)


def test_withdrawn_quotes_are_not_options(dev_data: Data) -> None:
    dev_data["facts"]["quote/S-1/W-100"]["status"] = "withdrawn"
    scenario = scenario_from_data(dev_data)
    found = options(scenario.intents[0], scenario.authority, scenario.fact_values())
    assert [option.args["supplier_id"] for option in found] == ["S-2"]
    dev_data["facts"]["quote/S-2/W-100"]["status"] = "withdrawn"
    with pytest.raises(ScenarioError, match="not feasible together"):
        scenario_from_data(dev_data)


def test_an_option_names_its_write_and_what_it_uses(dev_data: Data) -> None:
    scenario = scenario_from_data(dev_data)
    first = options(scenario.intents[0], scenario.authority, scenario.fact_values())[0]
    assert dict(first.args) == {
        "supplier_id": "S-1",
        "sku": "W-100",
        "quantity": 100,
        "unit_price_minor": 540,
        "currency": "USD",
        "cost_center": "CC-OPS",
    }
    assert (first.resource, first.need) == ("budget/CC-OPS", 54_000)


def test_a_queued_order_is_still_an_option(dev_data: Data) -> None:
    dev_data["authority"]["auto_approve_up_to_minor"] = 0
    scenario = scenario_from_data(dev_data)
    assert len(options(scenario.intents[0], scenario.authority, scenario.fact_values())) == 2


def test_an_intent_that_is_not_required_may_be_infeasible(dev_data: Data) -> None:
    dev_data["intents"][0]["required"] = False
    dev_data["intents"][0]["accept"]["max_unit_price_minor"] = 1
    assert scenario_from_data(dev_data).intents[0].required is False


def test_required_intents_share_the_budget(dev_data: Data) -> None:
    _second_intent(dev_data)
    dev_data["intents"][1]["id"] = "REQ-2"
    with pytest.raises(ScenarioError, match="not feasible together"):
        scenario_from_data(dev_data)
    dev_data["facts"]["budget/CC-OPS"]["limit_minor"] = 128_000
    scenario_from_data(dev_data)


def test_feasibility_tries_every_combination_of_options(dev_data: Data) -> None:
    """The cheapest-first choice for REQ-1 leaves too little for REQ-2; another fits."""
    facts = dev_data["facts"]
    facts["quote/S-1/W-100"]["unit_price_minor"] = 600
    facts["quote/S-2/W-100"]["unit_price_minor"] = 500
    facts["quote/S-3/W-200"] = {**facts["quote/S-1/W-100"], "unit_price_minor": 300}
    facts["budget/CC-OPS"] = {"limit_minor": 80_000, "committed_minor": 0}
    dev_data["authority"]["allowlist"] = ["S-1", "S-2", "S-3"]
    dev_data["intents"].append(
        {
            "id": "REQ-2",
            "operation": "submit_purchase_order",
            "accept": {
                "sku": "W-200",
                "quantity": 100,
                "suppliers": ["S-3"],
                "max_unit_price_minor": 300,
                "currency": "USD",
                "cost_center": "CC-OPS",
            },
        }
    )
    scenario = scenario_from_data(dev_data)
    first = options(scenario.intents[0], scenario.authority, scenario.fact_values())
    assert [option.need for option in first] == [60_000, 50_000]
    facts["budget/CC-OPS"]["limit_minor"] = 79_999
    with pytest.raises(ScenarioError, match="not feasible together"):
        scenario_from_data(dev_data)


def _reservations(dev_data: Data, *quantities: int) -> Data:
    dev_data["facts"]["inventory/WH-1/W-100"] = {"available": 50}
    for number, quantity in enumerate(quantities, start=2):
        dev_data["intents"].append(
            {
                "id": f"REQ-{number}",
                "operation": "reserve_inventory",
                "accept": {"warehouse_id": "WH-1", "sku": "W-100", "quantity": quantity},
            }
        )
    return dev_data


def test_required_reservations_share_the_stock(dev_data: Data) -> None:
    scenario_from_data(_reservations(copy.deepcopy(dev_data), 30, 20))
    with pytest.raises(ScenarioError, match="not feasible together"):
        scenario_from_data(_reservations(dev_data, 30, 21))


def test_a_reservation_needs_its_stock(dev_data: Data) -> None:
    data = _reservations(dev_data, 10)
    del data["facts"]["inventory/WH-1/W-100"]
    with pytest.raises(ScenarioError, match="not feasible together"):
        scenario_from_data(data)


def test_feasibility_on_other_facts_serves_the_infeasible_excuse(dev_data: Data) -> None:
    scenario = scenario_from_data(dev_data)
    facts = scenario.fact_values()
    assert feasible_together([scenario.intents[0]], scenario.authority, facts)
    facts["budget/CC-OPS"] = BudgetFact(limit_minor=50_000, committed_minor=0).model_dump()
    assert not feasible_together([scenario.intents[0]], scenario.authority, facts)
    assert feasible_together([], scenario.authority, facts)


# Intents and authority -------------------------------------------------------------------


ORDER: dict[str, object] = {
    "supplier_id": "S-2",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 600,
    "currency": "USD",
    "cost_center": "CC-OPS",
}


def test_an_acceptable_order_meets_every_constraint(dev_data: Data) -> None:
    intent = scenario_from_data(dev_data).intents[0]
    assert intent.accepts(Operation.SUBMIT_PURCHASE_ORDER, ORDER)
    assert not intent.accepts(Operation.RESERVE_INVENTORY, ORDER)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("supplier_id", "S-3"),
        ("sku", "W-200"),
        ("quantity", 99),
        ("unit_price_minor", 601),
        ("unit_price_minor", True),
        ("currency", "EUR"),
        ("cost_center", "CC-RND"),
    ],
)
def test_an_order_breaking_one_constraint_is_not_acceptable(
    dev_data: Data, field: str, value: object
) -> None:
    intent = scenario_from_data(dev_data).intents[0]
    assert not intent.accepts(Operation.SUBMIT_PURCHASE_ORDER, {**ORDER, field: value})


def test_an_acceptable_reservation_names_its_warehouse() -> None:
    intent = Intent.model_validate(
        {
            "id": "REQ-2",
            "operation": "reserve_inventory",
            "accept": {"warehouse_id": "WH-1", "sku": "W-100", "quantity": 30},
        }
    )
    args = {"warehouse_id": "WH-1", "sku": "W-100", "quantity": 30}
    assert intent.accepts(Operation.RESERVE_INVENTORY, args)
    assert not intent.accepts(Operation.RESERVE_INVENTORY, {**args, "warehouse_id": "WH-2"})
    assert not intent.accepts(Operation.RESERVE_INVENTORY, {**args, "quantity": 31})


def test_the_first_matching_approver_rule_decides(dev_data: Data) -> None:
    dev_data["authority"]["approver"] = [
        {"match": {"supplier_id": "S-2", "intent_id": "REQ-1"}, "decision": "deny", "after_ms": 5},
        {"match": {}, "decision": "approve", "after_ms": 7},
    ]
    authority = scenario_from_data(dev_data).authority
    s2 = authority.approver_rule({"intent_id": "REQ-1", "supplier_id": "S-2"})
    s1 = authority.approver_rule({"intent_id": "REQ-1", "supplier_id": "S-1"})
    assert s2 is not None and s2.decision is Decision.DENY
    assert s1 is not None and (s1.decision, s1.after_ms) == (Decision.APPROVE, 7)
    dev_data["authority"]["approver"] = dev_data["authority"]["approver"][:1]
    authority = scenario_from_data(dev_data).authority
    assert authority.approver_rule({"intent_id": "REQ-1", "supplier_id": "S-1"}) is None


def test_the_authority_applies_its_policy(dev_data: Data) -> None:
    authority = scenario_from_data(dev_data).authority
    scope = {"supplier_id": "S-2", "max_amount_minor": 57_500}
    assert authority.policy_decision(Operation.SUBMIT_PURCHASE_ORDER, scope) is (
        PolicyDecision.APPROVE
    )
    scope["max_amount_minor"] = 60_001
    assert authority.policy_decision(Operation.SUBMIT_PURCHASE_ORDER, scope) is (
        PolicyDecision.QUEUE
    )
