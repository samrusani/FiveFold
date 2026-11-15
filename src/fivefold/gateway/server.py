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
on_call_tool=)`, `Server.run(read, write, Server.create_initialization_options())`, the
handler context's `request` (`mcp.server.context.ServerRequestContext`), and the
`mcp_types` models `Tool`, `ListToolsResult`, `CallToolRequestParams`, `CallToolResult`
and `TextContent`.
"""

from __future__ import annotations

import json
from typing import Any

import mcp_types as types
from mcp.server.lowlevel.server import Server

from fivefold.gateway.tools import TOOLS, GatewayTools, ToolError
from fivefold.harness.mcp_link import Link, delivered_seq

SERVER_NAME = "fivefold-gateway"


def _tool_list() -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(name=t.name, description=t.description, input_schema=t.input_schema())
            for t in TOOLS
        ]
    )


def server_for(tools: GatewayTools, link: Link) -> Server[Any]:
    """An MCP server that serves `tools` to the agent worker at the other end of `link`."""

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return _tool_list()

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            result = await tools.call(
                params.name,
                params.arguments or {},
                worker=link.agent,
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

    return Server(SERVER_NAME, on_list_tools=list_tools, on_call_tool=call_tool)


async def serve(tools: GatewayTools, link: Link) -> None:
    """Serve one connection until the agent's end closes."""
    server = server_for(tools, link)
    await server.run(link.server_read, link.server_write, server.create_initialization_options())
