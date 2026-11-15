"""The dependency rules of record 0004, as pure functions (`fivefold.domain.rules`)."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from fivefold.domain.rules import (
    QUOTE_EXACT_FIELDS,
    BasisFailure,
    Dependency,
    FactKind,
    FactValue,
    Operation,
    Rule,
    RuleKind,
    check_basis,
    check_rule,
    consequence,
    consequence_key,
    dependencies,
    fact_kind,
    order_amount_minor,
)

PO = Operation.SUBMIT_PURCHASE_ORDER
RESERVE = Operation.RESERVE_INVENTORY
ORDER: dict[str, str | int] = {
    "supplier_id": "S-1",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 540,
    "currency": "USD",
    "cost_center": "CC-OPS",
}
RESERVATION: dict[str, str | int] = {"warehouse_id": "WH-1", "sku": "W-100", "quantity": 30}
QUOTE: dict[str, object] = {
    "unit_price_minor": 540,
    "currency": "USD",
    "min_quantity": 10,
    "lead_time_days": 5,
    "status": "active",
}
QUOTE_DEP = Dependency("quote/S-1/W-100", Rule.QUOTE_UNCHANGED)
BUDGET_DEP = Dependency("budget/CC-OPS", Rule.BUDGET_COVERS)
STOCK_DEP = Dependency("inventory/WH-1/W-100", Rule.STOCK_COVERS)


class FakeHistory:
    """Fact versions in memory, with the versions served in time."""

    def __init__(
        self, versions: Mapping[str, list[FactValue]], served: set[tuple[str, int]]
    ) -> None:
        self.versions = versions
        self.served = served

    def value_at(self, key: str, version: int) -> FactValue | None:
        values = self.versions.get(key, [])
        return values[version - 1] if 1 <= version <= len(values) else None

    def current(self, key: str) -> tuple[int, FactValue] | None:
        values = self.versions.get(key, [])
        return (len(values), values[-1]) if values else None

    def was_served(self, key: str, version: int) -> bool:
        return (key, version) in self.served


def test_a_purchase_order_depends_on_its_quote_exactly_and_its_budget_sufficiently() -> None:
    assert dependencies(PO, ORDER) == (QUOTE_DEP, BUDGET_DEP)
    assert [dependency.kind for dependency in dependencies(PO, ORDER)] == [
        RuleKind.EXACT,
        RuleKind.SUFFICIENT,
    ]


def test_a_reservation_depends_on_its_stock_sufficiently() -> None:
    assert dependencies(RESERVE, RESERVATION) == (STOCK_DEP,)
    assert STOCK_DEP.kind is RuleKind.SUFFICIENT


def test_the_consequence_is_the_budget_or_the_stock() -> None:
    assert consequence_key(PO, ORDER) == "budget/CC-OPS"
    assert consequence_key(RESERVE, RESERVATION) == "inventory/WH-1/W-100"


@pytest.mark.parametrize(
    ("key", "kind"),
    [
        ("quote/S-1/W-100", FactKind.QUOTE),
        ("budget/CC-OPS", FactKind.BUDGET),
        ("inventory/WH-1/W-100", FactKind.INVENTORY),
        ("suppliers", FactKind.SUPPLIERS),
    ],
)
def test_a_fact_key_names_its_kind(key: str, kind: FactKind) -> None:
    assert fact_kind(key) is kind


@pytest.mark.parametrize(
    "key",
    ["quote/S-1", "budget/CC OPS", "budget/", "inventory/WH-1/W-100/x", "price/S-1/W-100", ""],
)
def test_a_malformed_fact_key_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="not a fact key"):
        fact_kind(key)


def test_an_order_amount_is_quantity_times_unit_price() -> None:
    assert order_amount_minor(ORDER) == 54_000


def test_a_quote_rule_compares_exactly_the_four_fields() -> None:
    assert QUOTE_EXACT_FIELDS == ("unit_price_minor", "currency", "min_quantity", "status")
    assert Rule.QUOTE_UNCHANGED.fields == QUOTE_EXACT_FIELDS


def test_an_unchanged_quote_holds() -> None:
    assert check_rule(QUOTE_DEP, ORDER, cited=QUOTE, current=dict(QUOTE)).holds


def test_a_lead_time_change_is_harmless() -> None:
    later = {**QUOTE, "lead_time_days": 9}
    check = check_rule(QUOTE_DEP, ORDER, cited=QUOTE, current=later)
    assert check.holds
    assert check.changed_fields == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unit_price_minor", 567),
        ("unit_price_minor", 500),
        ("currency", "EUR"),
        ("min_quantity", 200),
        ("status", "withdrawn"),
    ],
)
def test_any_change_to_an_exact_field_breaks_the_quote_rule(field: str, value: object) -> None:
    later = {**QUOTE, field: value, "lead_time_days": 1}
    check = check_rule(QUOTE_DEP, ORDER, cited=QUOTE, current=later)
    assert not check.holds
    assert check.changed_fields == (field,)


def test_changed_fields_follow_the_rules_order() -> None:
    later = {**QUOTE, "status": "withdrawn", "unit_price_minor": 600}
    check = check_rule(QUOTE_DEP, ORDER, cited=QUOTE, current=later)
    assert check.changed_fields == ("unit_price_minor", "status")


@pytest.mark.parametrize(("committed", "holds"), [(46_000, True), (46_001, False)])
def test_a_budget_covers_an_order_exactly_up_to_its_limit(committed: int, holds: bool) -> None:
    cited = {"limit_minor": 100_000, "committed_minor": 20_000}
    current = {"limit_minor": 100_000, "committed_minor": committed}
    check = check_rule(BUDGET_DEP, ORDER, cited=cited, current=current)
    assert check.holds is holds
    assert check.changed_fields == ("committed_minor",)


def test_a_budget_rule_reads_only_the_current_version() -> None:
    cited = {"limit_minor": 0, "committed_minor": 0}
    current = {"limit_minor": 54_000, "committed_minor": 0}
    check = check_rule(BUDGET_DEP, ORDER, cited=cited, current=current)
    assert check.holds
    assert check.changed_fields == ("limit_minor",)


@pytest.mark.parametrize(("available", "holds"), [(30, True), (29, False)])
def test_stock_covers_a_reservation_exactly_up_to_what_is_available(
    available: int, holds: bool
) -> None:
    check = check_rule(
        STOCK_DEP, RESERVATION, cited={"available": 50}, current={"available": available}
    )
    assert check.holds is holds


def test_a_rule_refuses_a_fact_without_integers() -> None:
    with pytest.raises(ValueError, match="committed_minor must be an integer"):
        check_rule(
            BUDGET_DEP,
            ORDER,
            cited={},
            current={"limit_minor": 1, "committed_minor": True},
        )


def test_an_order_adds_its_amount_to_the_budget_even_past_the_limit() -> None:
    budget = {"limit_minor": 100_000, "committed_minor": 74_000}
    assert consequence(PO, ORDER, budget) == {"limit_minor": 100_000, "committed_minor": 128_000}


def test_a_reservation_takes_its_quantity_from_stock_even_below_zero() -> None:
    assert consequence(RESERVE, RESERVATION, {"available": 10}) == {"available": -20}


def _history(extra: Mapping[str, list[FactValue]] | None = None) -> dict[str, list[FactValue]]:
    budget_v1: FactValue = {"limit_minor": 100_000, "committed_minor": 20_000}
    versions: dict[str, list[FactValue]] = {
        "quote/S-1/W-100": [QUOTE],
        "budget/CC-OPS": [budget_v1],
    }
    versions.update(extra or {})
    return versions


ALL_SERVED = {("quote/S-1/W-100", 1), ("budget/CC-OPS", 1)}
BASIS = {"quote/S-1/W-100": 1, "budget/CC-OPS": 1}


def test_a_fresh_basis_passes_every_dependency() -> None:
    checks = check_basis(PO, ORDER, BASIS, FakeHistory(_history(), ALL_SERVED))
    assert [check.dependency for check in checks] == [QUOTE_DEP, BUDGET_DEP]
    assert all(check.passed for check in checks)
    assert [(check.cited_version, check.current_version) for check in checks] == [(1, 1), (1, 1)]


def test_a_dependency_missing_from_the_basis_is_undeclared() -> None:
    checks = check_basis(PO, ORDER, {"budget/CC-OPS": 1}, FakeHistory(_history(), ALL_SERVED))
    assert checks[0].failure is BasisFailure.UNDECLARED_INPUT
    assert (checks[0].cited_version, checks[0].current_version) == (None, 1)
    assert checks[1].passed


def test_a_version_never_served_in_time_is_unobserved() -> None:
    served = {("budget/CC-OPS", 1)}
    checks = check_basis(PO, ORDER, BASIS, FakeHistory(_history(), served))
    assert checks[0].failure is BasisFailure.UNOBSERVED_INPUT
    assert checks[1].passed


def test_a_version_that_does_not_exist_is_unobserved() -> None:
    basis = {"quote/S-1/W-100": 7, "budget/CC-OPS": 1}
    checks = check_basis(PO, ORDER, basis, FakeHistory(_history(), ALL_SERVED))
    assert checks[0].failure is BasisFailure.UNOBSERVED_INPUT


def test_a_missing_fact_is_unobserved_and_has_no_current_version() -> None:
    versions = _history()
    del versions["budget/CC-OPS"]
    served = {("quote/S-1/W-100", 1)}
    checks = check_basis(PO, ORDER, BASIS, FakeHistory(versions, served))
    assert checks[1].failure is BasisFailure.UNOBSERVED_INPUT
    assert checks[1].current_version is None


def test_a_quote_that_changed_since_the_basis_is_stale() -> None:
    versions = _history({"quote/S-1/W-100": [QUOTE, {**QUOTE, "unit_price_minor": 567}]})
    checks = check_basis(PO, ORDER, BASIS, FakeHistory(versions, ALL_SERVED))
    assert checks[0].failure is BasisFailure.STALE_INPUT
    assert (checks[0].cited_version, checks[0].current_version) == (1, 2)
    assert checks[0].changed_fields == ("unit_price_minor",)


def test_an_insufficient_budget_is_stale_even_at_the_cited_version() -> None:
    versions = _history({"budget/CC-OPS": [{"limit_minor": 50_000, "committed_minor": 0}]})
    checks = check_basis(PO, ORDER, BASIS, FakeHistory(versions, ALL_SERVED))
    assert checks[1].failure is BasisFailure.STALE_INPUT
    assert checks[1].changed_fields == ()


def test_exact_only_skips_the_sufficient_rules() -> None:
    versions = _history({"budget/CC-OPS": [{"limit_minor": 0, "committed_minor": 0}]})
    checks = check_basis(PO, ORDER, BASIS, FakeHistory(versions, ALL_SERVED), exact_only=True)
    assert [check.dependency for check in checks] == [QUOTE_DEP]
    assert checks[0].passed


def test_extra_citations_are_ignored() -> None:
    basis = {**BASIS, "quote/S-2/W-100": 99, "suppliers": 1}
    checks = check_basis(PO, ORDER, basis, FakeHistory(_history(), ALL_SERVED))
    assert all(check.passed for check in checks)


def test_a_served_version_missing_from_history_is_an_error() -> None:
    served = {*ALL_SERVED, ("quote/S-1/W-100", 2)}
    basis = {"quote/S-1/W-100": 2, "budget/CC-OPS": 1}
    with pytest.raises(ValueError, match="was served but is not on record"):
        check_basis(PO, ORDER, basis, FakeHistory(_history(), served))
