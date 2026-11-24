"""The scenario file format (0009), its loader, and the feasibility check (0004).

A scenario holds the task, its intents with their acceptance constraints, the initial
facts, the authority policy, the contract levels, the fault plan, timing parameters and
the agents' budgets. The `Scenario` model refuses a file that breaks the format, and one
whose required intents cannot all be completed together on the initial facts (0004,
property 4; 0009). The loader takes any path, so held-out scenarios stay outside the
repository.

`scenarios/scenario.schema.json` is this model's JSON Schema. A test keeps the two equal;
regenerate the file with `python -m fivefold.domain.scenario > scenarios/scenario.schema.json`.

YAML parsing sits behind `parse_yaml`, the loader's only use of a YAML library.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from fivefold.domain.authority import Decision, PolicyDecision, policy_decision
from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.rules import (
    CURRENCY_PATTERN,
    ID_BODY,
    ID_PATTERN,
    ORDERABLE_STATUS,
    FactKind,
    Operation,
    budget_key,
    fact_kind,
    inventory_key,
    order_amount_minor,
    quote_key,
)

Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
Currency = Annotated[str, StringConstraints(pattern=CURRENCY_PATTERN)]
Quantity = Annotated[StrictInt, Field(ge=1)]
Minor = Annotated[StrictInt, Field(ge=0)]
PositiveMs = Annotated[StrictInt, Field(ge=1)]
Ms = Annotated[StrictInt, Field(ge=0)]
Count = Annotated[StrictInt, Field(ge=1)]

SCENARIO_FACT_KEY = (
    rf"^(quote/{ID_BODY}/{ID_BODY}|memory/{ID_BODY}/{ID_BODY}"
    rf"|budget/{ID_BODY}|inventory/{ID_BODY}/{ID_BODY})$"
)
"""Keys a scenario may set. The `suppliers` directory is derived, never written."""

MatchField = Literal[
    "intent_id",
    "operation",
    "supplier_id",
    "sku",
    "max_quantity",
    "max_amount_minor",
    "currency",
    "cost_center",
    "warehouse_id",
]
"""Request fields an approver rule can match: the intent, the operation and the scope."""


class ScenarioError(ValueError):
    """The scenario cannot be read, breaks the format, or is infeasible."""


class ScenarioModel(BaseModel):
    """Base of the scenario models: unknown fields are refused and values never change."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_attribute_docstrings=True)


# Intents -------------------------------------------------------------------------------


class PurchaseOrderAccept(ScenarioModel):
    """Acceptance constraints of a purchase-order intent (0004, property 4)."""

    sku: Id
    quantity: Quantity
    suppliers: list[Id] = Field(min_length=1)
    """The suppliers the task allows."""
    max_unit_price_minor: Minor
    currency: Currency
    cost_center: Id


class ReservationAccept(ScenarioModel):
    """Acceptance constraints of a reservation intent (0004, property 4)."""

    warehouse_id: Id
    sku: Id
    quantity: Quantity


class Intent(ScenarioModel):
    """One unit of work with a stable id, such as requisition REQ-1 (0001)."""

    id: Id
    operation: Operation
    required: StrictBool = True
    """Whether property 4 requires an effect for this intent."""
    accept: PurchaseOrderAccept | ReservationAccept

    @model_validator(mode="after")
    def _accept_fits_operation(self) -> Intent:
        expected = (
            PurchaseOrderAccept
            if self.operation is Operation.SUBMIT_PURCHASE_ORDER
            else ReservationAccept
        )
        if not isinstance(self.accept, expected):
            raise ValueError(
                f"intent {self.id}: a {self.operation} intent needs {expected.__name__}"
            )
        return self

    def accepts(self, operation: Operation, args: Mapping[str, object]) -> bool:
        """Whether a write with these args is acceptable for this intent (0004, definitions).

        Same operation, sku and quantity; for purchase orders, a supplier in the allowed
        set, a unit price at or below the maximum, and the same currency and cost center;
        for reservations, the same warehouse.
        """
        if operation is not self.operation:
            return False
        accept = self.accept
        if args.get("sku") != accept.sku or args.get("quantity") != accept.quantity:
            return False
        if isinstance(accept, ReservationAccept):
            return args.get("warehouse_id") == accept.warehouse_id
        price = args.get("unit_price_minor")
        return (
            args.get("supplier_id") in accept.suppliers
            and isinstance(price, int)
            and not isinstance(price, bool)
            and price <= accept.max_unit_price_minor
            and args.get("currency") == accept.currency
            and args.get("cost_center") == accept.cost_center
        )


# Facts ---------------------------------------------------------------------------------


class QuoteFact(ScenarioModel):
    """A supplier's quote for a sku: `quote/{supplier_id}/{sku}`."""

    unit_price_minor: Minor
    currency: Currency
    min_quantity: Quantity
    lead_time_days: Annotated[StrictInt, Field(ge=0)]
    status: Annotated[str, StringConstraints(min_length=1)]
    """`active` means the quote is on offer; any other value means it is not."""


class BudgetFact(ScenarioModel):
    """A cost center's budget: `budget/{cost_center}`. What is left is limit minus committed."""

    limit_minor: Minor
    committed_minor: Minor


class InventoryFact(ScenarioModel):
    """A sku's stock in a warehouse: `inventory/{warehouse_id}/{sku}`."""

    available: StrictInt
    """Units free to reserve. A destination below C3 can take it below zero."""


class MemoryFact(ScenarioModel):
    """The remembered order channel and notes for a supplier and sku (0023)."""

    order_channel: str
    notes: str


FactModel = QuoteFact | BudgetFact | InventoryFact | MemoryFact

FACT_MODELS: dict[FactKind, type[FactModel]] = {
    FactKind.QUOTE: QuoteFact,
    FactKind.BUDGET: BudgetFact,
    FactKind.INVENTORY: InventoryFact,
    FactKind.MEMORY: MemoryFact,
}
"""The model of each kind of fact a scenario sets."""


# Authority -----------------------------------------------------------------------------


class ApproverRule(ScenarioModel):
    """A rule of the scripted approver (0002, rule 7). The first matching rule decides."""

    match: dict[MatchField, StrictInt | str] = Field(default_factory=dict)
    """Request fields that must be equal; an empty match accepts every request."""
    decision: Decision
    after_ms: Ms
    """The approver's delay after the request, in virtual milliseconds."""

    def matches(self, fields: Mapping[str, object]) -> bool:
        """Whether a request with these fields matches the rule."""
        return all(fields.get(name) == value for name, value in self.match.items())


class Resolver(ScenarioModel):
    """The scripted resolver. It attests only after the settle time, and tells the truth."""

    after_ms: Ms
    attests: Literal["truth"]


class Authority(ScenarioModel):
    """The run's authority policy, enforced by the approvals service (0002)."""

    allowlist: list[Id]
    """Suppliers that policy allows. Orders from any other supplier are denied."""
    per_order_ceiling_minor: Minor
    """Orders above this amount are denied by policy."""
    auto_approve_up_to_minor: Minor
    """Orders at or below this amount are approved by policy at once."""
    approval_ttl_ms: PositiveMs
    """An approval expires this long after its decision."""
    approval_wait_ms: PositiveMs
    """How long a caller waits for a decision before it gives up (0002, rule 14)."""
    approver: list[ApproverRule] = Field(default_factory=list)
    resolver: Literal["none"] | Resolver = "none"

    def policy_decision(self, operation: Operation, scope: Mapping[str, object]) -> PolicyDecision:
        """What policy does with a request on arrival (0002, rule 6)."""
        return policy_decision(
            operation,
            scope,
            allowlist=self.allowlist,
            per_order_ceiling_minor=self.per_order_ceiling_minor,
            auto_approve_up_to_minor=self.auto_approve_up_to_minor,
        )

    def approver_rule(self, fields: Mapping[str, object]) -> ApproverRule | None:
        """The scripted approver's first rule that matches a request, or None."""
        return next((rule for rule in self.approver if rule.matches(fields)), None)


# Parameters and budgets ----------------------------------------------------------------


class Params(ScenarioModel):
    """Contract and timing values in virtual milliseconds (0005, 0009). All optional."""

    max_hop_latency_ms: PositiveMs = 50
    commit_deadline_ms: PositiveMs = 10_000
    """D: a request not committed within this time of its arrival never commits."""
    status_lag_ms: Ms = 0
    """L: a status answer is accurate to at most this long before it is produced."""
    dispatch_timeout_ms: PositiveMs = 2_000
    """T: must exceed twice `max_hop_latency_ms`."""
    max_attempts: Count = 5
    status_probe_interval_ms: PositiveMs = 1_000
    max_probes: Count = 20
    tool_wait_ms: Ms = 2_500
    approval_poll_interval_ms: PositiveMs = 500
    max_wait_ms: Ms = Field(default=60_000, le=60_000)
    """The longest `wait_ms` a status tool accepts, at most 60,000 (0015)."""
    think_ms: Ms = 1_000
    """Virtual time each model call takes (0009)."""

    @model_validator(mode="after")
    def _timeout_exceeds_round_trip(self) -> Params:
        if self.dispatch_timeout_ms <= 2 * self.max_hop_latency_ms:
            raise ValueError("dispatch_timeout_ms must exceed 2 x max_hop_latency_ms (0005)")
        return self

    def contract_params(self) -> ContractParams:
        """The promises the destination publishes (0005)."""
        return ContractParams(
            max_hop_latency_ms=self.max_hop_latency_ms,
            commit_deadline_ms=self.commit_deadline_ms,
            status_lag_ms=self.status_lag_ms,
        )


class Budget(ScenarioModel):
    """Limits for each agent worker (0009)."""

    max_agent_steps: Count
    max_tokens: Count


# The scenario --------------------------------------------------------------------------


class Scenario(ScenarioModel):
    """A scenario file (0009)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_attribute_docstrings=True,
        json_schema_extra={"$schema": "https://json-schema.org/draft/2020-12/schema"},
    )

    id: Id
    task_id: Id
    title: Annotated[str, StringConstraints(min_length=1)]
    task: Annotated[str, StringConstraints(min_length=1)]
    """The task text the agent receives."""
    intents: list[Intent] = Field(min_length=1)
    facts: dict[Annotated[str, StringConstraints(pattern=SCENARIO_FACT_KEY)], FactModel]
    """Initial facts. Every fact starts at version 1."""
    memory_dependency: Literal["memory_channel_unchanged"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    """Select the exact order channel dependency for a scenario with memory facts."""
    authority: Authority
    contracts: list[Contract] = Field(min_length=1)
    faults: list[dict[str, JsonValue]] | dict[str, JsonValue] = Field(default_factory=list)
    """The fault plan (0006): a list of faults, or a random plan. Not interpreted here."""
    params: Params = Field(default_factory=Params)
    budget: Budget

    @model_validator(mode="after")
    def _consistent_and_feasible(self) -> Scenario:
        ids = [intent.id for intent in self.intents]
        if len(set(ids)) != len(ids):
            raise ValueError(f"intent ids must be unique: {ids}")
        if len(set(self.contracts)) != len(self.contracts):
            raise ValueError(f"contracts must be unique: {self.contracts}")
        for key, value in self.facts.items():
            expected = FACT_MODELS[fact_kind(key)]
            if not isinstance(value, expected):
                raise ValueError(f"fact {key} needs the fields of {expected.__name__}")
        if self.memory_enabled != any(isinstance(fact, MemoryFact) for fact in self.facts.values()):
            raise ValueError("memory facts and memory_dependency must be declared together")
        required = [intent for intent in self.intents if intent.required]
        if not feasible_together(required, self.authority, self.fact_values()):
            raise ValueError(
                "the required intents are not feasible together on the initial facts (0004)"
            )
        return self

    @property
    def memory_enabled(self) -> bool:
        """Whether the scenario adds the exact memory dependency to purchase orders."""
        return self.memory_dependency is not None

    def fact_values(self) -> dict[str, dict[str, object]]:
        """The initial facts as plain values, in key order."""
        return {key: self.facts[key].model_dump() for key in sorted(self.facts)}

    def intent(self, intent_id: str) -> Intent:
        """The intent with this id. Raises `KeyError` if the task has none."""
        for intent in self.intents:
            if intent.id == intent_id:
                return intent
        raise KeyError(intent_id)


# Feasibility (0004, property 4) --------------------------------------------------------


@dataclass(frozen=True)
class Option:
    """One allowed way to complete an intent: the write's args, and what it draws on.

    `resource` is the key of the budget or stock the write uses up, and `need` how much:
    an amount in minor units, or a quantity.
    """

    args: Mapping[str, str | int]
    resource: str
    need: int


def options(
    intent: Intent, authority: Authority, facts: Mapping[str, Mapping[str, object]]
) -> tuple[Option, ...]:
    """The allowed options for one intent on these facts, alone.

    A purchase-order option is an active quote from a supplier in the intent's set whose
    write would be acceptable (0004), whose quantity meets the quote's minimum, and which
    policy would not deny (0002, rule 6). A reservation's option is the stock it names.
    The budget or stock must exist; whether it covers the write is decided jointly by
    `feasible_together`.
    """
    accept = intent.accept
    if isinstance(accept, ReservationAccept):
        key = inventory_key(accept.warehouse_id, accept.sku)
        if key not in facts:
            return ()
        args: dict[str, str | int] = {
            "warehouse_id": accept.warehouse_id,
            "sku": accept.sku,
            "quantity": accept.quantity,
        }
        return (Option(args, key, accept.quantity),)
    budget = budget_key(accept.cost_center)
    if budget not in facts:
        return ()
    found: list[Option] = []
    for supplier_id in sorted(set(accept.suppliers)):
        quote = facts.get(quote_key(supplier_id, accept.sku))
        if quote is None or quote.get("status") != ORDERABLE_STATUS:
            continue
        price = quote.get("unit_price_minor")
        minimum = quote.get("min_quantity")
        currency = quote.get("currency")
        if not isinstance(price, int) or not isinstance(minimum, int):
            continue
        if not isinstance(currency, str) or accept.quantity < minimum:
            continue
        args = {
            "supplier_id": supplier_id,
            "sku": accept.sku,
            "quantity": accept.quantity,
            "unit_price_minor": price,
            "currency": currency,
            "cost_center": accept.cost_center,
        }
        if not intent.accepts(intent.operation, args):
            continue
        amount = order_amount_minor(args)
        scope: dict[str, object] = {
            "supplier_id": supplier_id,
            "sku": accept.sku,
            "max_quantity": accept.quantity,
            "max_amount_minor": amount,
            "currency": currency,
            "cost_center": accept.cost_center,
        }
        if authority.policy_decision(intent.operation, scope) is PolicyDecision.DENY:
            continue
        found.append(Option(args, budget, amount))
    return tuple(found)


def _capacity(key: str, facts: Mapping[str, Mapping[str, object]]) -> int:
    value = facts[key]
    if fact_kind(key) is FactKind.BUDGET:
        limit, committed = value.get("limit_minor"), value.get("committed_minor")
        if not isinstance(limit, int) or not isinstance(committed, int):
            raise ValueError(f"budget {key} needs integer limit_minor and committed_minor")
        return limit - committed
    available = value.get("available")
    if not isinstance(available, int):
        raise ValueError(f"stock {key} needs an integer available")
    return available


def feasible_together(
    intents: Collection[Intent],
    authority: Authority,
    facts: Mapping[str, Mapping[str, object]],
) -> bool:
    """Whether one allowed option per intent fits these facts at the same time (0004).

    Options that share a budget or a stock must fit within it together. Every option of
    one intent draws on the same budget or stock, because the intent fixes its cost
    center, or its warehouse and sku. So each intent's cheapest option is always the best
    choice, and the check needs no search: it sums those per budget or stock.

    The loader checks the required intents on the initial facts (0009). For property 4's
    `infeasible` excuse, pass one intent and the facts at run end with every effect of
    the run removed.
    """
    used: dict[str, int] = {}
    for intent in intents:
        found = options(intent, authority, facts)
        if not found:
            return False
        if len({option.resource for option in found}) != 1:
            raise ValueError(f"the options of {intent.id} draw on more than one resource")
        cheapest = min(option.need for option in found)
        used[found[0].resource] = used.get(found[0].resource, 0) + cheapest
    return all(need <= _capacity(key, facts) for key, need in used.items())


# Loading -------------------------------------------------------------------------------


def parse_yaml(text: str) -> object:
    """Parse YAML text into plain data. The loader's only use of a YAML library.

    It uses PyYAML's `safe_load`, which builds only plain data: a tag that would build a
    Python object is refused.
    """
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ScenarioError(f"the scenario is not valid YAML: {error}") from error
    return data


def scenario_from_data(data: object) -> Scenario:
    """Validate parsed data as a scenario. Raises `ScenarioError` on any problem."""
    try:
        return Scenario.model_validate(data)
    except ValidationError as error:
        raise ScenarioError(str(error)) from error


def load_scenario(text: str) -> Scenario:
    """Load a scenario from YAML text, checking its format and feasibility."""
    return scenario_from_data(parse_yaml(text))


def load_scenario_file(path: str | Path) -> Scenario:
    """Load a scenario file from any path, inside the repository or not."""
    return load_scenario(Path(path).read_text(encoding="utf-8"))


def json_schema_text() -> str:
    """The scenario format's JSON Schema, as committed in `scenarios/scenario.schema.json`."""
    return json.dumps(Scenario.model_json_schema(), indent=2) + "\n"


def scenario_digest(scenario: Scenario) -> str:
    """The SHA-256 of the scenario's canonical JSON, as a run records it (0009).

    The evaluator compares it with the run's record, so a run is never scored against a
    scenario that changed since it ran.
    """
    text = json.dumps(scenario.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    print(json_schema_text(), end="")
