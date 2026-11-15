"""Requests and answers of the simulator's API (0002, 0003, 0005, 0008).

These models cross component boundaries: the B0 agent calls the simulator directly, the
gateway calls it in G modes, and the harness carries every message between them. The
simulator answers immediately; waiting (`wait_ms`) is the harness's job, on virtual-time
timers (0009).

Writes answer in the classes of record 0003:

- `WriteCommitted`: committed, new or a replay of a key that already committed.
- `WriteRejected`: a definitive rejection that states nothing was committed, with a reason
  and, for `precondition_failed`, each failed check (0005, conditional_write).
- `WriteInProgress`: a request with the same idempotency key is still in flight (C2, C3).

The fourth class, unknown, never comes from the simulator: it is what a caller concludes
from a timeout or a transport error. `WriteReceived` and `WriteAborted` belong to the
harness's split path, which receives a write now and commits it later (fault F5).

Every answer carries an `answer` field naming its shape, so a union parses unambiguously.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationInfo,
    model_validator,
)

from fivefold.domain.authority import ApprovalStatus
from fivefold.domain.rules import (
    BUSINESS_ARGS,
    CURRENCY_PATTERN,
    FACT_KEY_PATTERN,
    ID_PATTERN,
    Operation,
)

Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
"""An id such as `S-1`, `W-100`, `CC-OPS`, `REQ-1` or `APR-0001`."""

FactKey = Annotated[str, StringConstraints(pattern=FACT_KEY_PATTERN)]
"""A fact key such as `quote/S-1/W-100`."""

Currency = Annotated[str, StringConstraints(pattern=CURRENCY_PATTERN)]
"""An ISO 4217 currency code in capitals."""

Ref = Annotated[str, StringConstraints(pattern=r"^[^\x00-\x1f\x7f]{1,200}$")]
"""A caller's own reference: 1 to 200 characters, no control characters."""

Quantity = Annotated[StrictInt, Field(ge=1)]
Minor = Annotated[StrictInt, Field(ge=0)]
"""An amount of money in minor units, such as cents."""

Version = Annotated[StrictInt, Field(ge=1)]
VirtualMs = Annotated[StrictInt, Field(ge=0)]


@dataclass(frozen=True)
class WaitLimit:
    """The run's longest status-tool wait, given as validation context (0005).

    Record 0005 sets it, 60,000 virtual ms unless the scenario's `max_wait_ms` says
    otherwise. Every place a status tool's wait is checked validates with it:
    `model_validate(data, context=WaitLimit(max_wait_ms))`.
    """

    max_wait_ms: int


def within_wait_limit(wait_ms: int, info: ValidationInfo) -> int:
    """Refuse a wait above the run's `max_wait_ms`, when the context gives a `WaitLimit`.

    The check runs only at validation time, so the field's JSON Schema, which the agent's
    model sees, stays as it was.
    """
    limit = info.context
    if isinstance(limit, WaitLimit) and wait_ms > limit.max_wait_ms:
        raise ValueError(f"the run's max_wait_ms is {limit.max_wait_ms}")
    return wait_ms


WaitMs = Annotated[VirtualMs, AfterValidator(within_wait_limit)]
"""A status tool's wait in virtual ms: at least 0, and at most the run's `max_wait_ms`
when validated with a `WaitLimit` (0005)."""

ArgValue = StrictInt | str
"""A business argument is text or an integer (0001, item 6)."""


class ApiName(StrEnum):
    """The simulator's API calls (0008, tool surfaces)."""

    GET_QUOTE = "get_quote"
    LIST_SUPPLIERS = "list_suppliers"
    GET_BUDGET = "get_budget"
    GET_INVENTORY = "get_inventory"
    REQUEST_APPROVAL = "request_approval"
    GET_APPROVAL_STATUS = "get_approval_status"
    SUBMIT_PURCHASE_ORDER = "submit_purchase_order"
    RESERVE_INVENTORY = "reserve_inventory"
    GET_ORDER_STATUS = "get_order_status"
    GET_RESERVATION_STATUS = "get_reservation_status"


class ApiModel(BaseModel):
    """Base of every API model: unknown fields are refused and values never change."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# Refusals ------------------------------------------------------------------------------


class RefusalReason(StrEnum):
    """Why a read, an approval call or a status call was refused. Nothing was recorded."""

    FORBIDDEN = "forbidden"
    """The caller holds no credential for this run (0008, trust boundaries)."""

    NOT_FOUND = "not_found"
    """No such fact or approval."""

    UNSUPPORTED = "unsupported"
    """The destination lacks the capability, such as status below C1 (0005)."""


class Refusal(ApiModel):
    """A refused call. Writes are never refused this way: they are rejected (0003)."""

    answer: Literal["refused"] = "refused"
    reason: RefusalReason
    detail: str


# Reads ---------------------------------------------------------------------------------


class GetQuote(ApiModel):
    """Read a supplier's quote for a sku: `quote/{supplier_id}/{sku}`."""

    api: ClassVar[ApiName] = ApiName.GET_QUOTE
    supplier_id: Id
    sku: Id


class ListSuppliers(ApiModel):
    """Read the supplier directory: `suppliers`."""

    api: ClassVar[ApiName] = ApiName.LIST_SUPPLIERS


class GetBudget(ApiModel):
    """Read a cost center's budget: `budget/{cost_center}`."""

    api: ClassVar[ApiName] = ApiName.GET_BUDGET
    cost_center: Id


class GetInventory(ApiModel):
    """Read a sku's stock in a warehouse: `inventory/{warehouse_id}/{sku}`."""

    api: ClassVar[ApiName] = ApiName.GET_INVENTORY
    warehouse_id: Id
    sku: Id


class FactRead(ApiModel):
    """A fact as served: its key, its current version and its data (0008)."""

    answer: Literal["fact"] = "fact"
    key: str
    version: Version
    data: dict[str, JsonValue]


# Approvals -----------------------------------------------------------------------------


class PurchaseOrderScope(ApiModel):
    """What a purchase-order approval covers, besides its intent and operation (0002, rule 5)."""

    supplier_id: Id
    sku: Id
    max_quantity: Quantity
    max_amount_minor: Minor
    currency: Currency
    cost_center: Id


class ReservationScope(ApiModel):
    """What a reservation approval covers, besides its intent and operation (0002, rule 5)."""

    warehouse_id: Id
    sku: Id
    max_quantity: Quantity


Scope = PurchaseOrderScope | ReservationScope

SCOPE_MODELS: dict[Operation, type[PurchaseOrderScope] | type[ReservationScope]] = {
    Operation.SUBMIT_PURCHASE_ORDER: PurchaseOrderScope,
    Operation.RESERVE_INVENTORY: ReservationScope,
}
"""The scope model of each operation."""


def _check_scope(operation: Operation, scope: Scope) -> None:
    if not isinstance(scope, SCOPE_MODELS[operation]):
        raise ValueError(f"a {operation} request needs a {SCOPE_MODELS[operation].__name__}")


class RequestApproval(ApiModel):
    """Ask the approvals service for one approval (0002, rule 4).

    A repeated request with the same `client_request_key` returns the original request.
    The gateway uses its action id as the key. The key is optional, so a caller without
    one, such as the B0 agent, files a new request each time.
    """

    api: ClassVar[ApiName] = ApiName.REQUEST_APPROVAL
    intent_id: Id
    operation: Operation
    scope: Scope
    client_request_key: Ref | None = None

    @model_validator(mode="after")
    def _scope_fits_operation(self) -> RequestApproval:
        _check_scope(self.operation, self.scope)
        return self


class GetApprovalStatus(ApiModel):
    """Ask for an approval's current status.

    `wait_ms` belongs to the B0 tool (0008). The harness waits on virtual-time timers; the
    simulator ignores it and answers at once.
    """

    # The docstring above is the request's JSON Schema description, which the agent's
    # model sees, so the run's limit on `wait_ms` is described at `WaitMs` instead.
    api: ClassVar[ApiName] = ApiName.GET_APPROVAL_STATUS
    approval_id: Id
    wait_ms: WaitMs = 0


class ApprovalAnswer(ApiModel):
    """An approval as the approvals service reports it at the time of the answer.

    `expires_at` is set once the approval is approved: `decided_vt + approval_ttl_ms`
    (0002, rule 8). `decided_by` is `policy` for decisions made on arrival, otherwise the
    scripted approver's actor name. `repeated` marks the answer to a request whose
    `client_request_key` was already used.
    """

    answer: Literal["approval"] = "approval"
    approval_id: str
    intent_id: str
    operation: Operation
    scope: Scope
    status: ApprovalStatus
    requested_vt: VirtualMs
    decided_vt: VirtualMs | None = None
    decided_by: str | None = None
    expires_at: VirtualMs | None = None
    revoked_vt: VirtualMs | None = None
    repeated: StrictBool = False

    @model_validator(mode="after")
    def _scope_fits_operation(self) -> ApprovalAnswer:
        _check_scope(self.operation, self.scope)
        return self

    def request_fields(self) -> dict[str, str | int]:
        """The request's fields as the scripted approver's rules match them (0002, rule 7)."""
        fields: dict[str, str | int] = {
            "intent_id": self.intent_id,
            "operation": str(self.operation),
        }
        fields.update(self.scope.model_dump())
        return fields


# Writes --------------------------------------------------------------------------------


class WriteRequest(ApiModel):
    """Fields every write carries, at every contract level (0004, 0008).

    `basis` maps fact keys to the versions the caller relied on. Every level records it;
    only C3 enforces it. `idempotency_key` is honoured only by destinations with the
    idempotency_key capability (C2, C3). `client_ref` is the caller's own reference, such as
    the gateway's attempt id; the simulator stores it and never acts on it.
    """

    operation: ClassVar[Operation]
    intent_id: Id
    approval_id: Id
    basis: dict[FactKey, Version]
    idempotency_key: Id | None = None
    client_ref: Ref | None = None

    def business_args(self) -> dict[str, str | int]:
        """The operation's business arguments, in record 0001's order."""
        values: dict[str, str | int] = self.model_dump()
        return {name: values[name] for name in BUSINESS_ARGS[self.operation]}


class SubmitPurchaseOrder(WriteRequest):
    """Place a purchase order. It charges `quantity x unit_price_minor` to the budget."""

    api: ClassVar[ApiName] = ApiName.SUBMIT_PURCHASE_ORDER
    operation: ClassVar[Operation] = Operation.SUBMIT_PURCHASE_ORDER
    supplier_id: Id
    sku: Id
    quantity: Quantity
    unit_price_minor: Minor
    currency: Currency
    cost_center: Id


class ReserveInventory(WriteRequest):
    """Reserve stock. It takes `quantity` from the warehouse's available stock."""

    api: ClassVar[ApiName] = ApiName.RESERVE_INVENTORY
    operation: ClassVar[Operation] = Operation.RESERVE_INVENTORY
    warehouse_id: Id
    sku: Id
    quantity: Quantity


Write = SubmitPurchaseOrder | ReserveInventory


class WriteCommitted(ApiModel):
    """The write committed one effect, or `replay` returns the stored result of its key."""

    answer: Literal["committed"] = "committed"
    effect_id: str
    replay: StrictBool
    intent_id: str
    operation: Operation
    args: dict[str, ArgValue]
    committed_vt: VirtualMs


class WriteRejectReason(StrEnum):
    """Why a write was definitively rejected. Nothing was committed."""

    FORBIDDEN = "forbidden"
    """The caller holds no credential for this run. In G modes, only the gateway does."""

    UNKNOWN_FACT = "unknown_fact"
    """The budget or stock the write would change does not exist."""

    PRECONDITION_FAILED = "precondition_failed"
    """C3: a check of the basis or the approval failed at commit (0005)."""


class CheckName(StrEnum):
    """A failed check of a conditional write (0005), named after properties 2 and 3 (0004)."""

    APPROVAL_UNKNOWN = "approval_unknown"
    APPROVAL_PENDING = "approval_pending"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_REVOKED = "approval_revoked"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_OUT_OF_SCOPE = "approval_out_of_scope"
    APPROVAL_USED = "approval_used"
    UNDECLARED_INPUT = "undeclared_input"
    UNOBSERVED_INPUT = "unobserved_input"
    STALE_INPUT = "stale_input"


INPUT_CHECKS = frozenset(
    {CheckName.UNDECLARED_INPUT, CheckName.UNOBSERVED_INPUT, CheckName.STALE_INPUT}
)
"""Checks about one input of the basis; they name the fact key."""


class FailedCheck(ApiModel):
    """One failed check of a conditional write.

    Input checks name the fact `key`, the `cited_version` from the basis (None when
    undeclared), the `current_version`, and for `stale_input` the `changed_fields` the rule
    reads. `approval_out_of_scope` lists the `mismatched_fields`. `approval_used` names the
    `effect_id` the approval already backs.
    """

    check: CheckName
    key: str | None = None
    cited_version: Version | None = None
    current_version: Version | None = None
    changed_fields: list[str] = []
    mismatched_fields: list[str] = []
    effect_id: str | None = None

    @model_validator(mode="after")
    def _key_for_input_checks(self) -> FailedCheck:
        if (self.check in INPUT_CHECKS) != (self.key is not None):
            raise ValueError("input checks name a fact key, and only they do")
        return self


class WriteRejected(ApiModel):
    """A definitive rejection: the write did not commit and never will."""

    answer: Literal["rejected"] = "rejected"
    reason: WriteRejectReason
    failed_checks: list[FailedCheck] = []
    detail: str = ""

    @model_validator(mode="after")
    def _checks_with_preconditions(self) -> WriteRejected:
        if (self.reason is WriteRejectReason.PRECONDITION_FAILED) != bool(self.failed_checks):
            raise ValueError("failed checks come with precondition_failed, and only with it")
        return self


class WriteInProgress(ApiModel):
    """A write with the same idempotency key is still in flight (0005). Not definitive."""

    answer: Literal["in_progress"] = "in_progress"
    idempotency_key: str


WriteAnswer = WriteCommitted | WriteRejected | WriteInProgress


class WriteReceived(ApiModel):
    """Harness only: the write reached the destination and waits for `commit_received`.

    It commits only if that happens by `deadline_vt`, its arrival plus the commit deadline
    D, unless a fault waived the deadline (F5 beyond the deadline, a contract violation).
    """

    answer: Literal["received"] = "received"
    received_seq: StrictInt
    deadline_vt: VirtualMs


class WriteAborted(ApiModel):
    """Harness only: the write missed its commit deadline and never commits (0005)."""

    answer: Literal["aborted"] = "aborted"
    received_seq: StrictInt
    deadline_vt: VirtualMs


# Status (C1) ---------------------------------------------------------------------------


class GetOrderStatus(ApiModel):
    """Which purchase orders exist for an intent (0005, status). C1 only."""

    api: ClassVar[ApiName] = ApiName.GET_ORDER_STATUS
    intent_id: Id


class GetReservationStatus(ApiModel):
    """Which reservations exist for an intent (0005, status). C1 only."""

    api: ClassVar[ApiName] = ApiName.GET_RESERVATION_STATUS
    intent_id: Id


class StatusEffect(ApiModel):
    """One effect a status answer lists."""

    effect_id: str
    args: dict[str, ArgValue]


class StatusAnswer(ApiModel):
    """The effects of one intent and operation, accurate to `as_of_vt` (0005).

    The answer reflects every commit up to `as_of_vt`, which is at most `status_lag_ms`
    before the answer was produced, unless a fault breaks that promise (F10).
    """

    answer: Literal["status"] = "status"
    intent_id: str
    operation: Operation
    effects: list[StatusEffect]
    as_of_vt: VirtualMs


REQUESTS: dict[ApiName, type[ApiModel]] = {
    ApiName.GET_QUOTE: GetQuote,
    ApiName.LIST_SUPPLIERS: ListSuppliers,
    ApiName.GET_BUDGET: GetBudget,
    ApiName.GET_INVENTORY: GetInventory,
    ApiName.REQUEST_APPROVAL: RequestApproval,
    ApiName.GET_APPROVAL_STATUS: GetApprovalStatus,
    ApiName.SUBMIT_PURCHASE_ORDER: SubmitPurchaseOrder,
    ApiName.RESERVE_INVENTORY: ReserveInventory,
    ApiName.GET_ORDER_STATUS: GetOrderStatus,
    ApiName.GET_RESERVATION_STATUS: GetReservationStatus,
}
"""The request model of each API call, for a transport that carries a name and a body."""

ReadReply = Annotated[FactRead | Refusal, Field(discriminator="answer")]
ApprovalReply = Annotated[ApprovalAnswer | Refusal, Field(discriminator="answer")]
WriteReply = Annotated[WriteAnswer, Field(discriminator="answer")]
StatusReply = Annotated[StatusAnswer | Refusal, Field(discriminator="answer")]
"""Tagged unions of each call's answers, for parsing with a pydantic `TypeAdapter`."""
