"""Operations, fact keys and the dependency rules (0001, 0004).

Record 0004's dependency rules live here and nowhere else. The simulator's C3 check, the
gateway's dispatch check and the evaluator's property 3 all call these functions, so they
cannot disagree about what "still fresh" means. The rules change only by decision record.

- submit_purchase_order depends on `quote/{supplier_id}/{sku}` (exact): unit_price_minor,
  currency, min_quantity and status are unchanged since the basis version.
- submit_purchase_order depends on `budget/{cost_center}` (sufficient): limit_minor minus
  committed_minor, just before this effect, is at least quantity x unit_price_minor.
- reserve_inventory depends on `inventory/{warehouse_id}/{sku}` (sufficient): available,
  just before this effect, is at least quantity.

`check_basis` applies property 3's three checks to one write, in order, for each
dependency: the basis declares it (`undeclared_input`), the cited version was served
before the write reached the simulator (`unobserved_input`), and the rule holds between
the cited version and the fact just before commit (`stale_input`). The caller supplies the
fact history through `FactHistory`, so the same function serves a check inside a commit
transaction, a check before a send, and a check over a finished run's records.

Everything here is pure: no I/O, no clock, no randomness.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

ID_BODY = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
"""One id: a letter or digit, then up to 63 letters, digits, dots, underscores or hyphens.

Record 0001 asks for ids in their documented format, such as `S-1`, `W-100` or `CC-OPS`.
No format is written down yet, so this pattern only excludes what would break a fact key:
an empty id, whitespace and the `/` separator.
"""

ID_PATTERN = rf"^{ID_BODY}$"
"""An id, anchored."""

CURRENCY_PATTERN = r"^[A-Z]{3}$"
"""An ISO 4217 currency code, as three capital letters (0001, item 6)."""


class Operation(StrEnum):
    """The two write operations of v0.1 (0001, item 2)."""

    SUBMIT_PURCHASE_ORDER = "submit_purchase_order"
    RESERVE_INVENTORY = "reserve_inventory"


BUSINESS_ARGS: Mapping[Operation, tuple[str, ...]] = {
    Operation.SUBMIT_PURCHASE_ORDER: (
        "supplier_id",
        "sku",
        "quantity",
        "unit_price_minor",
        "currency",
        "cost_center",
    ),
    Operation.RESERVE_INVENTORY: ("warehouse_id", "sku", "quantity"),
}
"""The business arguments of each operation, as in record 0001's table."""


class FactKind(StrEnum):
    """The kinds of facts the simulator keeps. The key's first segment names the kind."""

    QUOTE = "quote"
    MEMORY = "memory"
    BUDGET = "budget"
    INVENTORY = "inventory"
    SUPPLIERS = "suppliers"
    """The supplier directory that `list_suppliers` serves. No rule reads it."""


SUPPLIERS_KEY = "suppliers"
"""The key of the supplier directory."""

FACT_KEY_PATTERN = (
    rf"^(quote/{ID_BODY}/{ID_BODY}|memory/{ID_BODY}/{ID_BODY}"
    rf"|budget/{ID_BODY}|inventory/{ID_BODY}/{ID_BODY}|suppliers)$"
)
"""Every fact key: `quote/{supplier_id}/{sku}`, `budget/{cost_center}`,
`inventory/{warehouse_id}/{sku}`, `memory/{supplier_id}/{sku}` or `suppliers`."""

_FACT_KEY = re.compile(FACT_KEY_PATTERN)

FACT_FIELDS: Mapping[FactKind, tuple[str, ...]] = {
    FactKind.MEMORY: ("order_channel", "notes"),
    FactKind.QUOTE: ("unit_price_minor", "currency", "min_quantity", "lead_time_days", "status"),
    FactKind.BUDGET: ("limit_minor", "committed_minor"),
    FactKind.INVENTORY: ("available",),
}
"""The fields of each kind of fact that the rules and the scenario format know."""

ORDERABLE_STATUS = "active"
"""The quote status that allows an order. Any other status means the quote is not on offer."""

FactValue = Mapping[str, object]
"""One version of a fact: field name to value."""


def fact_kind(key: str) -> FactKind:
    """Return the kind of the fact `key`, or raise `ValueError` for a malformed key."""
    if _FACT_KEY.fullmatch(key) is None:
        raise ValueError(f"not a fact key: {key!r}")
    return FactKind(key.split("/", 1)[0])


def quote_key(supplier_id: str, sku: str) -> str:
    """The key of a supplier's quote for a sku."""
    return f"quote/{supplier_id}/{sku}"


def memory_key(supplier_id: str, sku: str) -> str:
    """The key of a supplier's remembered ordering channel for a sku."""
    return f"memory/{supplier_id}/{sku}"


def budget_key(cost_center: str) -> str:
    """The key of a cost center's budget."""
    return f"budget/{cost_center}"


def inventory_key(warehouse_id: str, sku: str) -> str:
    """The key of a sku's stock in a warehouse."""
    return f"inventory/{warehouse_id}/{sku}"


class RuleKind(StrEnum):
    """Exact rules need the cited fields unchanged; sufficient rules need enough left."""

    EXACT = "exact"
    SUFFICIENT = "sufficient"


QUOTE_EXACT_FIELDS: tuple[str, ...] = ("unit_price_minor", "currency", "min_quantity", "status")
"""The quote fields the exact rule compares. A change to any other field is harmless."""


class Rule(StrEnum):
    """The dependency rules of records 0004 and 0023."""

    QUOTE_UNCHANGED = "quote_unchanged"
    MEMORY_CHANNEL_UNCHANGED = "memory_channel_unchanged"
    BUDGET_COVERS = "budget_covers"
    STOCK_COVERS = "stock_covers"

    @property
    def kind(self) -> RuleKind:
        """Whether the rule is exact or sufficient."""
        return (
            RuleKind.EXACT
            if self in (Rule.QUOTE_UNCHANGED, Rule.MEMORY_CHANNEL_UNCHANGED)
            else RuleKind.SUFFICIENT
        )

    @property
    def fields(self) -> tuple[str, ...]:
        """The fact fields the rule reads."""
        if self is Rule.QUOTE_UNCHANGED:
            return QUOTE_EXACT_FIELDS
        if self is Rule.MEMORY_CHANNEL_UNCHANGED:
            return ("order_channel",)
        if self is Rule.BUDGET_COVERS:
            return ("limit_minor", "committed_minor")
        return ("available",)


@dataclass(frozen=True)
class Dependency:
    """One input an operation depends on: a fact key and the rule it must satisfy."""

    key: str
    rule: Rule

    @property
    def kind(self) -> RuleKind:
        """Whether the dependency's rule is exact or sufficient."""
        return self.rule.kind


def _text(args: Mapping[str, object], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise ValueError(f"argument {name} must be text")
    return value


def _integer(values: Mapping[str, object], name: str) -> int:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def dependencies(
    operation: Operation, args: Mapping[str, object], *, memory_enabled: bool = False
) -> tuple[Dependency, ...]:
    """Return the dependencies from the business arguments and the scenario's rules."""
    if operation is Operation.SUBMIT_PURCHASE_ORDER:
        sku = _text(args, "sku")
        supplier_id = _text(args, "supplier_id")
        order: tuple[Dependency, ...] = (
            Dependency(quote_key(supplier_id, sku), Rule.QUOTE_UNCHANGED),
            Dependency(budget_key(_text(args, "cost_center")), Rule.BUDGET_COVERS),
        )
        if memory_enabled:
            order += (Dependency(memory_key(supplier_id, sku), Rule.MEMORY_CHANNEL_UNCHANGED),)
        return order
    return (
        Dependency(
            inventory_key(_text(args, "warehouse_id"), _text(args, "sku")), Rule.STOCK_COVERS
        ),
    )


def consequence_key(operation: Operation, args: Mapping[str, object]) -> str:
    """The fact a committed write changes: the budget it charges or the stock it reserves."""
    if operation is Operation.SUBMIT_PURCHASE_ORDER:
        return budget_key(_text(args, "cost_center"))
    return inventory_key(_text(args, "warehouse_id"), _text(args, "sku"))


def order_amount_minor(args: Mapping[str, object]) -> int:
    """A purchase order's amount in minor units: quantity x unit_price_minor."""
    return _integer(args, "quantity") * _integer(args, "unit_price_minor")


def consequence(
    operation: Operation, args: Mapping[str, object], current: FactValue
) -> dict[str, object]:
    """The new value of the fact a committed write changes (0004, effects).

    A purchase order adds its amount to the budget's `committed_minor`. A reservation takes
    its quantity from `available`. No check happens here: at C0 to C2 a destination commits
    whatever it receives, so a budget can be overdrawn and stock can go below zero.
    """
    value = dict(current)
    if operation is Operation.SUBMIT_PURCHASE_ORDER:
        value["committed_minor"] = _integer(current, "committed_minor") + order_amount_minor(args)
    else:
        value["available"] = _integer(current, "available") - _integer(args, "quantity")
    return value


@dataclass(frozen=True)
class RuleCheck:
    """Whether a rule holds, and which of the fields it reads changed since the basis."""

    holds: bool
    changed_fields: tuple[str, ...]


def check_rule(
    dependency: Dependency, args: Mapping[str, object], *, cited: FactValue, current: FactValue
) -> RuleCheck:
    """Check one dependency rule (0004).

    `cited` is the fact at the basis version and `current` the fact just before the
    commit (or, before a send, the fact now). An exact rule holds when every field it
    compares is equal. A sufficient rule reads only `current`: what is left must cover
    the write. `changed_fields` lists the fields the rule reads whose values differ
    between the two versions, in the rule's field order.
    """
    rule = dependency.rule
    changed = tuple(name for name in rule.fields if cited.get(name) != current.get(name))
    if rule.kind is RuleKind.EXACT:
        return RuleCheck(holds=not changed, changed_fields=changed)
    if rule is Rule.BUDGET_COVERS:
        left = _integer(current, "limit_minor") - _integer(current, "committed_minor")
        return RuleCheck(holds=left >= order_amount_minor(args), changed_fields=changed)
    left = _integer(current, "available")
    return RuleCheck(holds=left >= _integer(args, "quantity"), changed_fields=changed)


class BasisFailure(StrEnum):
    """Why a dependency fails property 3's check (0004), in the order they are tested."""

    UNDECLARED_INPUT = "undeclared_input"
    """The basis does not name the dependency's key."""

    UNOBSERVED_INPUT = "unobserved_input"
    """The cited version was not served to the run before the write reached the simulator."""

    STALE_INPUT = "stale_input"
    """The rule does not hold between the cited version and the fact just before commit."""


@dataclass(frozen=True)
class DependencyCheck:
    """The result of checking one dependency of a write. `failure` is None when it passes."""

    dependency: Dependency
    failure: BasisFailure | None
    cited_version: int | None
    current_version: int | None
    changed_fields: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Whether the dependency passes every check."""
        return self.failure is None


class FactHistory(Protocol):
    """The facts a basis check reads, as of the moment it judges.

    `current` is the fact just before the commit being judged. `was_served` says whether
    the version was served to the run before the write reached the simulator.
    """

    def value_at(self, key: str, version: int) -> FactValue | None:
        """The fact `key` at `version`, or None if that version does not exist."""
        ...

    def current(self, key: str) -> tuple[int, FactValue] | None:
        """The latest version of `key` and its value, or None if the fact does not exist."""
        ...

    def was_served(self, key: str, version: int) -> bool:
        """Whether `version` of `key` was served to the run in time."""
        ...


def check_basis(
    operation: Operation,
    args: Mapping[str, object],
    basis: Mapping[str, int],
    history: FactHistory,
    *,
    exact_only: bool = False,
    memory_enabled: bool = False,
) -> tuple[DependencyCheck, ...]:
    """Check a write's basis against its dependencies (0004, property 3).

    Returns one result per dependency checked, in dependency order. With `exact_only`,
    sufficient rules are skipped, as before a retry at C2 (0003, I3). Keys in the basis
    that are not dependencies are ignored: extra citations are recorded, not checked (0001).
    """
    results: list[DependencyCheck] = []
    failure: BasisFailure | None
    for dependency in dependencies(operation, args, memory_enabled=memory_enabled):
        if exact_only and dependency.kind is RuleKind.SUFFICIENT:
            continue
        current = history.current(dependency.key)
        current_version = None if current is None else current[0]
        version = basis.get(dependency.key)
        if version is None:
            failure = BasisFailure.UNDECLARED_INPUT
            results.append(DependencyCheck(dependency, failure, None, current_version, ()))
            continue
        if not history.was_served(dependency.key, version):
            failure = BasisFailure.UNOBSERVED_INPUT
            results.append(DependencyCheck(dependency, failure, version, current_version, ()))
            continue
        cited = history.value_at(dependency.key, version)
        if cited is None or current is None:
            raise ValueError(f"{dependency.key} version {version} was served but is not on record")
        check = check_rule(dependency, args, cited=cited, current=current[1])
        failure = None if check.holds else BasisFailure.STALE_INPUT
        results.append(
            DependencyCheck(dependency, failure, version, current_version, check.changed_fields)
        )
    return tuple(results)
