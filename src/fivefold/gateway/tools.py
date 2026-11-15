"""The gateway's tools, as an agent worker calls them over MCP (0001, 0003, 0008).

G1 and G2 expose the same tools with the same schemas; only their behaviour differs
(0008). The list below is G2's. G1, authorization only, is the store with identity off:

- Every write call is a new action with its own approval request, and no fact is
  checked. When the send's outcome is unknown at the end of the call's wait (a timeout
  or a server error), the call fails with the tool error `gateway_timeout`, as a proxy
  returns 504, so the agent's framework retries it. The action stays OutcomeUnknown.
- `submit_final_report` hands the report to the harness's report sink as it came and
  checks nothing. The sink records it and says what breaks the schema, as it does for
  B0's reports, and the agent is told the same.

- Reads (`get_quote`, `list_suppliers`, `get_budget`, `get_inventory`) take the
  simulator's own arguments, read through the gateway's simulator client, record the
  read and return `{read_id, key, version, data}`.
- Writes (`submit_purchase_order`, `reserve_inventory`) take the business arguments,
  `intent_id`, `based_on` (read ids) and an optional `note`. Fields outside the schema
  are allowed and ignored, because they are volatile (0001, item 7). A call runs
  admission (0001, item 17), starts the action's driver, waits at most `tool_wait_ms`
  for a state the agent can act on, and returns the action's result (0003).
- `get_action_status(action_id, wait_ms)` waits at most `wait_ms` for a final state. It
  never contacts the destination (0003). Its schema allows at most 60,000 ms, 0005's
  default; a wait above the run's own `max_wait_ms` is refused too, as `invalid_args`,
  so the schema the agent sees stays the same in every run.
- `submit_final_report(claims)` refuses a report that breaks the schema with
  `invalid_args`, checks the rest against the records, and hands an accepted report to
  the harness's report sink (0003, 0008).

What has no refusal reason in 0003 is a tool error, as MCP reports one: a read the
simulator refused or did not answer in time (`gateway_timeout`, as a proxy's 504), a
status call for an action that does not exist, and an unknown tool.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
)

from fivefold.domain.api import (
    FactRead,
    GetBudget,
    GetInventory,
    GetQuote,
    Id,
    ListSuppliers,
    Minor,
    Quantity,
    WaitLimit,
    within_wait_limit,
)
from fivefold.domain.identity import InvalidArgs, action_key, normalize, target_of
from fivefold.domain.report import FinalReport, InvalidReport, parse_report, validation_messages
from fivefold.domain.rules import Operation, dependencies
from fivefold.gateway import wire
from fivefold.gateway.destination import Unreachable
from fivefold.gateway.dispatcher import Dispatcher
from fivefold.gateway.records import (
    ActionResult,
    Claim,
    ClaimStatus,
    Refusal,
    RefusalReason,
    ReportAccepted,
    WriteCall,
)
from fivefold.gateway.sim_client import ReadRefused, ReadRequest
from fivefold.gateway.state_machine import FINAL_STATES, ActionState, EventKind
from fivefold.gateway.store import ActionStore, UnknownAction
from fivefold.harness.scheduler import Scheduler

_S = ActionState

ANSWERED_STATES = frozenset(
    {_S.PENDING_APPROVAL, _S.CONFIRMED, _S.REJECTED, _S.FAILED, _S.OUTCOME_UNKNOWN}
)
"""The states a write call returns as soon as the action reaches one (0003)."""

ReadId = Annotated[str, StringConstraints(pattern=r"^[^\x00-\x1f\x7f]{1,64}$")]
"""A cited read id. One the gateway never issued is refused as `unknown_read`."""

Note = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$", max_length=2_000)]
AnyCaseCurrency = Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{3}$")]
"""A currency code in any case. The gateway uppercases it (0001, item 7)."""


class WriteArgs(BaseModel):
    """What every write tool takes besides its business arguments (0001, 0008)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    intent_id: Id
    based_on: list[ReadId] = []
    note: Note | None = None


class PurchaseOrderArgs(WriteArgs):
    """Arguments of `submit_purchase_order`."""

    supplier_id: Id
    sku: Id
    quantity: Quantity
    unit_price_minor: Minor
    currency: AnyCaseCurrency
    cost_center: Id


class ReservationArgs(WriteArgs):
    """Arguments of `reserve_inventory`."""

    warehouse_id: Id
    sku: Id
    quantity: Quantity


class ActionStatusArgs(BaseModel):
    """Arguments of `get_action_status`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: Annotated[str, StringConstraints(pattern=r"^A-[0-9]{4,}$")]
    wait_ms: Annotated[StrictInt, Field(ge=0, le=60_000), AfterValidator(within_wait_limit)] = 0
    """At most 60,000 by the schema, and at most the run's `max_wait_ms` when validated
    with a `WaitLimit`."""


@dataclass(frozen=True)
class ToolDefinition:
    """A tool as `tools/list` describes it."""

    name: str
    description: str
    arguments: type[BaseModel]

    def input_schema(self) -> dict[str, object]:
        """The tool's input as JSON Schema."""
        return self.arguments.model_json_schema()


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition("get_quote", "Read a supplier's quote for a sku.", GetQuote),
    ToolDefinition("list_suppliers", "Read the supplier directory.", ListSuppliers),
    ToolDefinition("get_budget", "Read a cost center's budget.", GetBudget),
    ToolDefinition("get_inventory", "Read a sku's stock in a warehouse.", GetInventory),
    ToolDefinition(
        "submit_purchase_order",
        "Propose a purchase order for an intent, citing the reads it relies on.",
        PurchaseOrderArgs,
    ),
    ToolDefinition(
        "reserve_inventory",
        "Propose an inventory reservation for an intent, citing the reads it relies on.",
        ReservationArgs,
    ),
    ToolDefinition(
        "get_action_status",
        "An action's result. Waits up to wait_ms (at most 60000) for a final state.",
        ActionStatusArgs,
    ),
    ToolDefinition(
        "submit_final_report",
        "Report, for every intent of the task, whether it is done, not done or unknown.",
        FinalReport,
    ),
)
"""Every tool G modes expose, in `tools/list` order."""


class ToolError(Exception):
    """A tool call that failed without a refusal reason; MCP reports it as an error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Reader(Protocol):
    """Reads facts from the simulator: the gateway's simulator client."""

    async def read(self, request: ReadRequest) -> FactRead:
        """Read one fact. Raises `Unreachable` or `ReadRefused`."""


class ReportSink(Protocol):
    """The harness's report sink."""

    def submit(
        self,
        worker: str,
        report: object,
        *,
        accepted_by: Literal["gateway", "sink"],
        message_seq: int | None = None,
    ) -> tuple[str, ...]:
        """Record a report; return what breaks its schema."""


@dataclass(frozen=True)
class ToolTiming:
    """How long tools wait, in virtual ms (0003, 0005)."""

    tool_wait_ms: int = 2_500
    max_wait_ms: int = 60_000
    """The longest `wait_ms` `get_action_status` accepts: the scenario's `max_wait_ms`."""


Json = dict[str, object]
Handler = Callable[[Mapping[str, object], "Call"], Awaitable[Json]]


@dataclass(frozen=True)
class Call:
    """Who made a tool call: the agent worker, and the message that carried the call."""

    worker: str
    message_seq: int | None


class GatewayTools:
    """G2's tools for one run. `call` serves one tool call of one agent worker."""

    def __init__(
        self,
        scheduler: Scheduler,
        store: ActionStore,
        dispatcher: Dispatcher,
        reader: Reader,
        reports: ReportSink,
        *,
        task_id: str,
        intents: Mapping[str, str],
        timing: ToolTiming | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._store = store
        self._dispatcher = dispatcher
        self._reader = reader
        self._reports = reports
        self._task_id = task_id
        self._intents = dict(intents)
        self._timing = timing if timing is not None else ToolTiming()
        self._handlers: dict[str, Handler] = {
            "get_quote": self._read_with(GetQuote),
            "list_suppliers": self._read_with(ListSuppliers),
            "get_budget": self._read_with(GetBudget),
            "get_inventory": self._read_with(GetInventory),
            "submit_purchase_order": self._write_with(PurchaseOrderArgs),
            "reserve_inventory": self._write_with(ReservationArgs),
            "get_action_status": self._status,
            "submit_final_report": self._report,
        }

    async def call(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        worker: str,
        message_seq: int | None = None,
    ) -> Json:
        """Serve one tool call from `worker`. Raises `ToolError`.

        `message_seq` is the harness's seq of the message that carried the call.
        """
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError("unknown_tool", f"no tool named {name!r}")
        return await handler(arguments, Call(worker, message_seq))

    @property
    def _now(self) -> int:
        return self._scheduler.now

    # Reads -----------------------------------------------------------------------------------

    def _read_with(self, model: type[ReadRequest]) -> Handler:
        async def read(arguments: Mapping[str, object], call: Call) -> Json:
            try:
                request = model.model_validate(dict(arguments))
            except ValidationError as error:
                raise ToolError("invalid_args", "; ".join(validation_messages(error))) from None
            try:
                fact = await self._reader.read(request)
            except Unreachable as error:
                raise ToolError("gateway_timeout", str(error)) from None
            except ReadRefused as error:
                raise ToolError(error.reason, error.detail) from None
            record = self._store.record_read(fact.key, fact.version, fact.data, self._now)
            return wire.read(record)

        return read

    # Writes ----------------------------------------------------------------------------------

    def _write_with(self, model: type[PurchaseOrderArgs] | type[ReservationArgs]) -> Handler:
        async def write(arguments: Mapping[str, object], caller: Call) -> Json:
            operation = (
                Operation.SUBMIT_PURCHASE_ORDER
                if model is PurchaseOrderArgs
                else Operation.RESERVE_INVENTORY
            )
            try:
                args = model.model_validate(dict(arguments))
                normal = normalize(operation, args.model_dump())
            except ValidationError as error:
                return self._invalid(operation, arguments, "; ".join(validation_messages(error)))
            except InvalidArgs as error:
                return self._invalid(operation, arguments, str(error))
            call = WriteCall(
                intent_id=args.intent_id,
                operation=str(operation),
                target=target_of(operation, normal),
                args=normal,
                action_key=action_key(self._task_id, args.intent_id, operation, normal),
                based_on=tuple(args.based_on),
                dependencies=tuple(d.key for d in dependencies(operation, normal)),
            )
            outcome = self._store.propose(call, self._now)
            if isinstance(outcome, Refusal):
                return wire.refusal(outcome)
            self._dispatcher.drive(outcome.action_id)
            await self._dispatcher.wait(
                outcome.action_id, ANSWERED_STATES.__contains__, self._timing.tool_wait_ms
            )
            if self._store.authorization_only:
                self._fail_unknown_outcome(outcome.action_id)
            result = self._store.status(outcome.action_id, self._now, tool=str(operation))
            return wire.action(result)

        return write

    def _fail_unknown_outcome(self, action_id: str) -> None:
        """G1: a send with no usable answer ends the call as a proxy's 504 does (0008).

        Raises `ToolError("gateway_timeout")` when the action's outcome is unknown. The
        records keep the action OutcomeUnknown; the response holds no action state, so,
        like every tool error, it is not logged as a response (I5).
        """
        view = self._store.view(action_id)
        action = view.action
        if action.state is not _S.OUTCOME_UNKNOWN:
            return
        reason = next(e.reason for e in reversed(view.events) if e.kind is EventKind.TRANSITION)
        raise ToolError(
            "gateway_timeout",
            f"{action.operation} for {action.intent_id}: no usable answer from the simulator"
            f" ({reason})",
        )

    def _invalid(self, operation: Operation, arguments: Mapping[str, object], why: str) -> Json:
        refusal = self._store.refuse_invalid(str(operation), dict(arguments), why, self._now)
        return wire.refusal(refusal)

    # Status ----------------------------------------------------------------------------------

    async def _status(self, arguments: Mapping[str, object], call: Call) -> Json:
        limit = WaitLimit(self._timing.max_wait_ms)
        try:
            args = ActionStatusArgs.model_validate(dict(arguments), context=limit)
        except ValidationError as error:
            raise ToolError("invalid_args", "; ".join(validation_messages(error))) from None
        try:
            self._store.view(args.action_id)
        except UnknownAction:
            raise ToolError("unknown_action", f"no action {args.action_id}") from None
        await self._dispatcher.wait(args.action_id, FINAL_STATES.__contains__, args.wait_ms)
        result: ActionResult = self._store.status(args.action_id, self._now)
        return wire.action(result)

    # The final report --------------------------------------------------------------------------

    async def _report(self, arguments: Mapping[str, object], call: Call) -> Json:
        if self._store.authorization_only:
            return self._forward_report(arguments, call)
        try:
            report = parse_report(dict(arguments), self._intents)
        except InvalidReport as invalid:
            message = "; ".join(invalid.errors)
            return wire.refusal(Refusal(RefusalReason.INVALID_ARGS, message=message))
        claims = [
            Claim(c.intent_id, ClaimStatus(str(c.status)), tuple(c.effect_ids), c.note)
            for c in report.claims
        ]
        outcome = self._store.submit_report(claims, self._now, worker=call.worker)
        if isinstance(outcome, ReportAccepted):
            self._reports.submit(
                call.worker,
                report.model_dump(mode="json"),
                accepted_by="gateway",
                message_seq=call.message_seq,
            )
        return wire.report(outcome)

    def _forward_report(self, arguments: Mapping[str, object], call: Call) -> Json:
        """G1: hand the report to the report sink unchanged, and check nothing (0008).

        The sink records every report with whether it is valid, and returns what breaks
        the report schema; the agent is told that, as B0's sink tells it, so it can fix the
        report and resubmit. G1 keeps no record of reports.
        """
        errors = self._reports.submit(
            call.worker, dict(arguments), accepted_by="gateway", message_seq=call.message_seq
        )
        if errors:
            return wire.refusal(Refusal(RefusalReason.INVALID_ARGS, message="; ".join(errors)))
        return wire.accepted()
