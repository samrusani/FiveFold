"""The run's longest status wait, `max_wait_ms` (0005), as the status tools check it.

A status tool's request is validated with the run's `WaitLimit`, so a wait above it is
refused like any other bad argument, while the JSON Schema the agent's model sees stays
the same in every run. The agent's own timeout for a gateway status call counts at most
`max_wait_ms` of wait, so the call ends even when the gateway never answers.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import mcp_types as types
import pytest
from pydantic import ValidationError

from fivefold.agents.connection import GatewayConnection
from fivefold.agents.tools import Timeouts, gateway_tools
from fivefold.domain.api import GetApprovalStatus, WaitLimit
from fivefold.gateway.tools import TOOLS, ActionStatusArgs


@pytest.mark.parametrize("model", [GetApprovalStatus, ActionStatusArgs])
def test_a_wait_above_the_run_s_limit_is_refused_only_where_the_run_gives_its_limit(
    model: type[GetApprovalStatus] | type[ActionStatusArgs],
) -> None:
    ids = {"approval_id": "APR-0001"} if model is GetApprovalStatus else {"action_id": "A-0001"}
    assert model.model_validate({**ids, "wait_ms": 5_001}).wait_ms == 5_001
    assert (
        model.model_validate({**ids, "wait_ms": 5_000}, context=WaitLimit(5_000)).wait_ms == 5_000
    )
    with pytest.raises(ValidationError, match="the run's max_wait_ms is 5000"):
        model.model_validate({**ids, "wait_ms": 5_001}, context=WaitLimit(5_000))


def test_the_status_tools_publish_the_same_wait_schema_in_every_run() -> None:
    """The run's limit is checked at validation time, never written into the schema:
    B0's schema has no maximum, and the gateway's keeps 0005's 60,000."""
    wait = {"default": 0, "minimum": 0, "title": "Wait Ms", "type": "integer"}
    assert GetApprovalStatus.model_json_schema()["properties"]["wait_ms"] == wait
    assert ActionStatusArgs.model_json_schema()["properties"]["wait_ms"] == {
        **wait,
        "maximum": 60_000,
    }
    (status,) = [tool for tool in TOOLS if tool.name == "get_action_status"]
    assert status.arguments is ActionStatusArgs


class Unanswered:
    """A gateway connection that records each call's timeout and answers a tool error."""

    def __init__(self) -> None:
        self.timeouts: list[int] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any], timeout_ms: int
    ) -> types.CallToolResult:
        self.timeouts.append(timeout_ms)
        text = types.TextContent(text='{"error": "invalid_args", "message": "wait_ms"}')
        return types.CallToolResult(content=[text], is_error=True)


def test_the_agent_s_timeout_for_a_gateway_status_call_counts_at_most_max_wait_ms() -> None:
    connection = Unanswered()
    listed = types.ListToolsResult(
        tools=[
            types.Tool(name=t.name, description=t.description, input_schema=t.input_schema())
            for t in TOOLS
        ]
    )
    timeouts = Timeouts(t_ms=2_000, tool_wait_ms=2_500, max_wait_ms=5_000)
    tools = gateway_tools(cast(GatewayConnection, connection), listed, timeouts)
    (status,) = [tool for tool in tools if tool.name == "get_action_status"]

    async def calls() -> None:
        for wait_ms in (0, 5_000, 60_000):
            await status.ainvoke({"action_id": "A-0001", "wait_ms": wait_ms})

    asyncio.run(calls())
    assert connection.timeouts == [2_000, 7_000, 7_000]
