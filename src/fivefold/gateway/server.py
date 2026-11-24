"""The gateway's MCP server: one low-level MCP `Server` per agent connection (0008, 0009).

Each connection is a `Link` from the harness, whose `agent` names the agent worker. The
worker's name comes from the connection, not from anything the agent sends, and stays
the same when the worker resumes from its checkpoint (0009), so the report check knows
whose report it is (0010). Each request the link delivers carries its message seq on the
handler context's `request`, so the report sink can record which message carried a
report, a copy that F4 delivered included (0004).

A tool's result goes back as JSON text and as structured content. A `ToolError` becomes
an MCP tool error (`is_error`), with the error code and message as JSON text.

MCP SDK calls used (mcp 2.3.0): `mcp.server.lowlevel.server.Server(name, on_list_tools=,
on_call_tool=, get_tool_input_schema=)`, `Server.run(read, write,
Server.create_initialization_options())`, the handler context's `request`
(`mcp.server.context.ServerRequestContext`), and the `mcp_types` models `Tool`,
`ListToolsResult`, `CallToolRequestParams`, `CallToolResult` and `TextContent`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import mcp_types as types
from mcp.server.lowlevel.server import Server

from fivefold.gateway.tools import MEMORY_TOOL, TOOLS, GatewayTools, ToolDefinition, ToolError
from fivefold.harness.mcp_link import Link, delivered_seq

SERVER_NAME = "fivefold-gateway"


def _tool_list(*, memory_enabled: bool) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(name=t.name, description=t.description, input_schema=t.input_schema())
            for t in _definitions(memory_enabled)
        ]
    )


def server_for(tools: GatewayTools, link: Link) -> Server[Any]:
    """An MCP server that serves `tools` to the agent worker at the other end of `link`."""
    return worker_server(lambda: tools, link.agent, memory_enabled=tools.memory_enabled)


def worker_server(
    tools: Callable[[], GatewayTools], worker: str, *, memory_enabled: bool = False
) -> Server[Any]:
    """An MCP server that serves the gateway's tools to the agent worker `worker`.

    `tools` gives the tools of the gateway's current incarnation when a call arrives. The
    split topology serves its one agent worker this way, over streamable HTTP
    (`fivefold.harness.split.gateway`).
    """

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return _tool_list(memory_enabled=memory_enabled)

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            result = await tools().call(
                params.name,
                params.arguments or {},
                worker=worker,
                message_seq=delivered_seq(ctx.request),
            )
        except ToolError as error:
            failure = {"error": error.code, "message": error.message}
            return types.CallToolResult(
                content=[types.TextContent(text=json.dumps(failure, sort_keys=True))],
                is_error=True,
            )
        return types.CallToolResult(
            content=[types.TextContent(text=json.dumps(result, sort_keys=True))],
            structured_content=result,
        )

    return Server(
        SERVER_NAME,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        get_tool_input_schema=lambda name: tool_input_schema(name, memory_enabled=memory_enabled),
    )


def _definitions(memory_enabled: bool) -> tuple[ToolDefinition, ...]:
    return (*TOOLS, MEMORY_TOOL) if memory_enabled else TOOLS


def tool_input_schema(name: str, *, memory_enabled: bool = False) -> dict[str, object] | None:
    """A tool's input schema by name, or None for a name that is no tool.

    The SDK checks a 2026-07-28 request's `Mcp-Param-*` headers against it, instead of
    asking the server for its tool list (mcp 2.3.0, `Server(get_tool_input_schema=)`).
    """
    for tool in _definitions(memory_enabled):
        if tool.name == name:
            return tool.input_schema()
    return None


async def serve(tools: GatewayTools, link: Link) -> None:
    """Serve one connection until the agent's end closes."""
    server = server_for(tools, link)
    await server.run(link.server_read, link.server_write, server.create_initialization_options())
