"""The gateway's calls to the simulator, on the `gw_sim` hop (0003, 0005, 0008).

`SimClient` implements `Destination` for the dispatcher and serves the read tools. It
sends each request through the harness transport, so every message is logged and can be
faulted (0006), and waits at most T, the dispatch timeout, for the answer (0005). In the
split topology the transport is HTTP, logged and faulted the same way
(`fivefold.harness.split.hops`). The
gateway is the only caller with a simulator credential in G modes (0008); requests name
it as the sender, and the simulator checks that name.

How answers map (0003):

- A write's answer: `committed` is committed; `rejected` is rejected, with reason
  `precondition_failed` when a C3 check failed and `destination_rejected` otherwise;
  `in_progress` is in progress; no answer within T is unknown (`timeout`), and a
  `server_error` answer is unknown (`server_error`), because a proxy can return one
  after the backend committed. A failed connection is unknown (`transport_error`).
- A read, an approval call or a status call that gets no usable answer raises
  `Unreachable`, and the caller learns nothing. A status call that times out is an
  unanswered probe (0005).
- A read the simulator refuses, such as an unknown supplier, raises `ReadRefused`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, TypeAdapter, ValidationError

from fivefold.domain.api import (
    ApprovalAnswer,
    ApprovalReply,
    FactRead,
    GetApprovalStatus,
    GetBudget,
    GetInventory,
    GetMemory,
    GetOrderStatus,
    GetQuote,
    GetReservationStatus,
    ListSuppliers,
    PurchaseOrderScope,
    ReadReply,
    Refusal,
    RequestApproval,
    ReservationScope,
    ReserveInventory,
    StatusAnswer,
    StatusReply,
    SubmitPurchaseOrder,
    WriteCommitted,
    WriteInProgress,
    WriteRejected,
    WriteRejectReason,
    WriteReply,
)
from fivefold.domain.rules import FactKind, Operation, fact_kind
from fivefold.gateway.destination import ApprovalNow, FactNow, Unreachable
from fivefold.gateway.records import Action, Answer, ApprovalStatus, Attempt, ProbeAnswer
from fivefold.gateway.state_machine import AnswerClass, Reason
from fivefold.harness.transport import Calls, TransportError, TransportTimeout

logger = logging.getLogger(__name__)


class MismatchedApproval(RuntimeError):
    """The simulator returned an approval for a different action or scope."""


ReadRequest = GetQuote | ListSuppliers | GetBudget | GetInventory | GetMemory

SERVER_ERROR = "server_error"
"""The `answer` of a reply that reports a server error instead of an answer."""

_WRITE_ANSWER: TypeAdapter[WriteCommitted | WriteRejected | WriteInProgress] = TypeAdapter(
    WriteReply
)
_READ_ANSWER: TypeAdapter[FactRead | Refusal] = TypeAdapter(ReadReply)
_APPROVAL_ANSWER: TypeAdapter[ApprovalAnswer | Refusal] = TypeAdapter(ApprovalReply)
_STATUS_ANSWER: TypeAdapter[StatusAnswer | Refusal] = TypeAdapter(StatusReply)


class ReadRefused(Exception):
    """The simulator refused a read; the reason and detail are its own."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class SimClient:
    """The simulator as the gateway reaches it."""

    def __init__(
        self,
        transport: Calls,
        *,
        timeout_ms: int,
        component: str = "gateway",
        simulator: str = "simulator",
    ) -> None:
        self._transport = transport
        self._timeout_ms = timeout_ms
        self._component = component
        self._simulator = simulator

    async def _call(
        self,
        request: BaseModel,
        api: str,
        *,
        intent_id: str | None = None,
        idempotency_key: str | None = None,
        sent_vt: int | None = None,
    ) -> object:
        """Send one request and return its answer, or raise a transport exception.

        `sent_vt` is the send time the gateway recorded for a write's attempt.
        """
        reply = await self._transport.call(
            hop="gw_sim",
            sender=self._component,
            receiver=self._simulator,
            api=api,
            body=request.model_dump(mode="json"),
            timeout_ms=self._timeout_ms,
            intent_id=intent_id,
            idempotency_key=idempotency_key,
            sent_vt=sent_vt,
        )
        return reply.body

    # Reads ---------------------------------------------------------------------------------

    async def read(self, request: ReadRequest) -> FactRead:
        """Read one fact. Raises `Unreachable` or `ReadRefused`."""
        try:
            body = await self._call(request, str(request.api))
        except (TransportTimeout, TransportError) as error:
            raise Unreachable(str(error)) from None
        answer = _parse(_READ_ANSWER, body)
        if isinstance(answer, Refusal):
            raise ReadRefused(str(answer.reason), answer.detail)
        return answer

    async def facts(self, keys: Sequence[str]) -> Mapping[str, FactNow]:
        """The current version and value of each fact, one read per key."""
        found: dict[str, FactNow] = {}
        for key in keys:
            try:
                fact = await self.read(read_request(key))
            except ReadRefused as error:
                raise Unreachable(f"{key}: {error}") from None
            found[key] = FactNow(fact.key, fact.version, fact.data)
        return found

    # Approvals ------------------------------------------------------------------------------

    async def request_approval(self, action: Action) -> ApprovalNow:
        """File the action's approval request, keyed by its action id (0002)."""
        operation = Operation(action.operation)
        request = RequestApproval(
            intent_id=action.intent_id,
            operation=operation,
            scope=scope_of(operation, action.args),
            client_request_key=action.action_id,
        )
        return await self._approval(action, request)

    async def approval(self, action: Action, approval_id: str) -> ApprovalNow:
        """The filed approval's status now."""
        request = GetApprovalStatus(approval_id=approval_id)
        return await self._approval(action, request)

    async def _approval(
        self, action: Action, request: RequestApproval | GetApprovalStatus
    ) -> ApprovalNow:
        intent_id = request.intent_id if isinstance(request, RequestApproval) else None
        try:
            body = await self._call(request, str(request.api), intent_id=intent_id)
        except (TransportTimeout, TransportError) as error:
            raise Unreachable(str(error)) from None
        answer = _parse(_APPROVAL_ANSWER, body)
        if isinstance(answer, Refusal):
            logger.warning("simulator refused the approval check: %s", answer.detail)
            raise Unreachable(f"simulator: {answer.reason}")
        operation = Operation(action.operation)
        if (
            answer.intent_id != action.intent_id
            or answer.operation != operation
            or answer.scope != scope_of(operation, action.args)
            or (
                isinstance(request, GetApprovalStatus) and answer.approval_id != request.approval_id
            )
        ):
            raise MismatchedApproval("the simulator returned a mismatched approval")
        status = ApprovalStatus(str(answer.status))
        expires_at = answer.expires_at if status is ApprovalStatus.APPROVED else None
        return ApprovalNow(answer.approval_id, status, expires_at)

    # Writes ---------------------------------------------------------------------------------

    async def send(self, action: Action, attempt: Attempt, approval_id: str) -> Answer:
        """Send one attempt and classify the answer (0003).

        The request carries the action's idempotency key only where the contract takes
        keys (0005), and never in G1, which has none (0008). It carries the basis in
        every configuration: G1 forwards the cited reads without checking them. It goes
        out with the attempt's recorded send time, `attempt.sent_vt`, which the harness's
        log gives the request too.
        """
        operation = Operation(action.operation)
        keyed = action.request_key is not None
        common = {
            "intent_id": action.intent_id,
            "approval_id": approval_id,
            "basis": dict(action.basis),
            "idempotency_key": attempt.idempotency_key if keyed else None,
            "client_ref": attempt.attempt_id,
            **dict(action.args),
        }
        request: SubmitPurchaseOrder | ReserveInventory
        if operation is Operation.SUBMIT_PURCHASE_ORDER:
            request = SubmitPurchaseOrder.model_validate(common)
        else:
            request = ReserveInventory.model_validate(common)
        try:
            body = await self._call(
                request,
                str(request.api),
                intent_id=action.intent_id,
                idempotency_key=request.idempotency_key,
                sent_vt=attempt.sent_vt,
            )
        except TransportTimeout:
            return Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)
        except TransportError:
            return Answer(AnswerClass.UNKNOWN, Reason.TRANSPORT_ERROR)
        try:
            answer = _parse(_WRITE_ANSWER, body)
        except Unreachable:
            return Answer(AnswerClass.UNKNOWN, Reason.SERVER_ERROR)
        if isinstance(answer, WriteCommitted):
            return Answer(AnswerClass.COMMITTED, Reason.COMMITTED, answer.effect_id)
        if isinstance(answer, WriteInProgress):
            return Answer(AnswerClass.IN_PROGRESS, Reason.IN_PROGRESS)
        if answer.reason is WriteRejectReason.PRECONDITION_FAILED:
            return Answer(AnswerClass.REJECTED, Reason.PRECONDITION_FAILED)
        return Answer(AnswerClass.REJECTED, Reason.DESTINATION_REJECTED)

    # Status (C1) ------------------------------------------------------------------------------

    async def lookup(self, action: Action) -> ProbeAnswer:
        """Which effects exist for the action's intent, and as of when (0005)."""
        request: GetOrderStatus | GetReservationStatus
        if Operation(action.operation) is Operation.SUBMIT_PURCHASE_ORDER:
            request = GetOrderStatus(intent_id=action.intent_id)
        else:
            request = GetReservationStatus(intent_id=action.intent_id)
        try:
            body = await self._call(request, str(request.api), intent_id=action.intent_id)
            answer = _parse(_STATUS_ANSWER, body)
        except (TransportTimeout, TransportError, Unreachable):
            return ProbeAnswer(answered=False)
        if isinstance(answer, Refusal):
            return ProbeAnswer(answered=False)
        effects = tuple(effect.effect_id for effect in answer.effects)
        return ProbeAnswer(answered=True, effect_ids=effects, as_of_vt=answer.as_of_vt)


def read_request(key: str) -> ReadRequest:
    """The read that serves a fact key, such as `get_quote` for `quote/S-1/W-100`."""
    kind = fact_kind(key)
    parts = key.split("/")
    if kind is FactKind.QUOTE:
        return GetQuote(supplier_id=parts[1], sku=parts[2])
    if kind is FactKind.MEMORY:
        return GetMemory(supplier_id=parts[1], sku=parts[2])
    if kind is FactKind.BUDGET:
        return GetBudget(cost_center=parts[1])
    if kind is FactKind.INVENTORY:
        return GetInventory(warehouse_id=parts[1], sku=parts[2])
    return ListSuppliers()


def scope_of(
    operation: Operation, args: Mapping[str, str | int]
) -> PurchaseOrderScope | ReservationScope:
    """What an approval for exactly this write covers (0002, rule 5)."""
    if operation is Operation.SUBMIT_PURCHASE_ORDER:
        quantity, price = int(args["quantity"]), int(args["unit_price_minor"])
        return PurchaseOrderScope(
            supplier_id=str(args["supplier_id"]),
            sku=str(args["sku"]),
            max_quantity=quantity,
            max_amount_minor=quantity * price,
            currency=str(args["currency"]),
            cost_center=str(args["cost_center"]),
        )
    return ReservationScope(
        warehouse_id=str(args["warehouse_id"]),
        sku=str(args["sku"]),
        max_quantity=int(args["quantity"]),
    )


def _is_server_error(body: object) -> bool:
    return isinstance(body, dict) and body.get("answer") == SERVER_ERROR


def _parse[T](adapter: TypeAdapter[T], body: object) -> T:
    """Parse an answer. An answer the gateway cannot read counts as a server error."""
    if _is_server_error(body):
        raise Unreachable("the simulator answered with a server error")
    try:
        return adapter.validate_python(body)
    except ValidationError as error:
        logger.warning("simulator answer did not parse: %s", error)
        raise Unreachable("simulator: ValidationError") from None
