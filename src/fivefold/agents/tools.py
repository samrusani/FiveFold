"""The agent's tools: the gateway's over MCP in G modes, the simulator's in B0 (0008).

Every tool returns the destination's answer to the model as JSON text, successes and
refusals alike. A tool raises only on a transport failure or a server error, so that
LangGraph's retry policy applies, as it would to an HTTP client (0008):

- no answer within the call's timeout raises `TransportTimeout`, which is not an
  `OSError`, so the default retry predicate retries it;
- the gateway's `gateway_timeout` tool error, its 504, raises `ServerError`.

The timeout of a call is the tool's declared server-side wait plus T (0005, 0008): T for
reads and simulator calls, `tool_wait_ms` plus T for gateway writes, and `wait_ms` plus T
for calls that wait. Timeouts run on the scheduler's virtual clock.

Checked in langchain-core 1.8.0: `StructuredTool.from_function(coroutine=, name=,
description=, args_schema=)` takes a Pydantic model or a JSON Schema dict
(`ArgsSchema`, `langchain_core/tools/base.py`), and passes the tool call's arguments to
the coroutine as keyword arguments.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import mcp_types as types
from langchain_core.tools import BaseTool, StructuredTool
from mcp.client.session import ClientSession
from pydantic import BaseModel

from fivefold.domain.api import (
    GetApprovalStatus,
    GetBudget,
    GetInventory,
    GetOrderStatus,
    GetQuote,
    GetReservationStatus,
    ListSuppliers,
    RequestApproval,
    ReserveInventory,
    SubmitPurchaseOrder,
)
from fivefold.domain.report import FinalReport
from fivefold.harness.scheduler import Scheduler, SchedulerTimeout
from fivefold.harness.transport import Transport, TransportTimeout

GATEWAY_WRITES = frozenset({"submit_purchase_order", "reserve_inventory"})
SIMULATOR = "simulator"
REPORT_SINK = "report_sink"


class ServerError(Exception):
    """The destination answered with a server error; the framework's retry applies."""


@dataclass(frozen=True)
class Timeouts:
    """The parts of a call's timeout, in virtual ms (0005)."""

    t_ms: int
    """T: the destination's dispatch timeout."""
    tool_wait_ms: int
    """How long a gateway write waits before it answers."""


async def _within[T](scheduler: Scheduler, call: Awaitable[T], timeout_ms: int, what: str) -> T:
    try:
        return await scheduler.wait_for(call, timeout_ms)
    except SchedulerTimeout:
        raise TransportTimeout(f"{what}: no answer within {timeout_ms} ms") from None


# G modes: the gateway's tools over MCP ------------------------------------------------------


def gateway_tools(
    session: ClientSession,
    listed: types.ListToolsResult,
    scheduler: Scheduler,
    timeouts: Timeouts,
) -> list[BaseTool]:
    """The gateway's tools, as `tools/list` described them."""
    return [_gateway_tool(session, tool, scheduler, timeouts) for tool in listed.tools]


def _gateway_tool(
    session: ClientSession, tool: types.Tool, scheduler: Scheduler, timeouts: Timeouts
) -> BaseTool:
    name = tool.name

    async def call(**arguments: Any) -> str:
        if name in GATEWAY_WRITES:
            timeout = timeouts.tool_wait_ms + timeouts.t_ms
        else:
            timeout = int(arguments.get("wait_ms", 0) or 0) + timeouts.t_ms
        result = await _within(scheduler, session.call_tool(name, arguments), timeout, name)
        text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
        if result.is_error and _error_code(text) == "gateway_timeout":
            raise ServerError(f"{name}: {text}")
        return text

    return StructuredTool.from_function(
        coroutine=call,
        name=name,
        description=tool.description or name,
        args_schema=dict(tool.input_schema),
    )


def _error_code(text: str) -> str | None:
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return None
    return str(body.get("error")) if isinstance(body, dict) else None


# B0: the simulator's API and the report sink ------------------------------------------------


@dataclass(frozen=True)
class _ApiTool:
    name: str
    description: str
    model: type[BaseModel]
    receiver: str = SIMULATOR


BASELINE_TOOLS: tuple[_ApiTool, ...] = (
    _ApiTool("get_quote", "Read a supplier's quote for a sku.", GetQuote),
    _ApiTool("list_suppliers", "Read the supplier directory.", ListSuppliers),
    _ApiTool("get_budget", "Read a cost center's budget.", GetBudget),
    _ApiTool("get_inventory", "Read a sku's stock in a warehouse.", GetInventory),
    _ApiTool("request_approval", "Request approval for an order.", RequestApproval),
    _ApiTool(
        "get_approval_status",
        "An approval's status. Waits up to wait_ms for a decision.",
        GetApprovalStatus,
    ),
    _ApiTool("submit_purchase_order", "Place a purchase order.", SubmitPurchaseOrder),
    _ApiTool("reserve_inventory", "Reserve stock.", ReserveInventory),
    _ApiTool(
        "submit_final_report",
        "Report, for every intent of the task, whether it is done, not done or unknown.",
        FinalReport,
        REPORT_SINK,
    ),
)
"""B0's tools at every contract level (0008)."""

STATUS_TOOLS: tuple[_ApiTool, ...] = (
    _ApiTool("get_order_status", "Which purchase orders exist for an intent.", GetOrderStatus),
    _ApiTool(
        "get_reservation_status", "Which reservations exist for an intent.", GetReservationStatus
    ),
)
"""B0's status tools, at C1 only (0008)."""


def baseline_tools(
    transport: Transport,
    scheduler: Scheduler,
    timeouts: Timeouts,
    *,
    worker: str,
    status: bool,
) -> list[BaseTool]:
    """B0's tools for one agent worker; `status` adds C1's status tools."""
    tools = BASELINE_TOOLS + (STATUS_TOOLS if status else ())
    return [_baseline_tool(transport, scheduler, timeouts, spec, worker) for spec in tools]


def _baseline_tool(
    transport: Transport, scheduler: Scheduler, timeouts: Timeouts, spec: _ApiTool, worker: str
) -> BaseTool:
    async def call(**arguments: Any) -> str:
        request = spec.model.model_validate(_plain(arguments))
        body = request.model_dump(mode="json")
        wait_ms = int(body.get("wait_ms", 0) or 0)
        reply = await transport.call(
            hop="agent_sim",
            sender=worker,
            receiver=spec.receiver,
            api=spec.name,
            body=body,
            timeout_ms=wait_ms + timeouts.t_ms,
            intent_id=_text(body.get("intent_id")),
            idempotency_key=_text(body.get("idempotency_key")),
        )
        return json.dumps(reply.body, sort_keys=True)

    return StructuredTool.from_function(
        coroutine=call, name=spec.name, description=spec.description, args_schema=spec.model
    )


def _plain(value: object) -> object:
    """Arguments as plain data: models a tool layer built become dicts again."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


ToolFactory = Callable[[], Awaitable[list[BaseTool]]]
