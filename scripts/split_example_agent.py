"""A small external MCP client for the no-fault G2 quick start."""

import asyncio
import json
from typing import Any

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import TextContent


async def tool(client: Client, name: str, arguments: dict[str, object]) -> dict[str, Any]:
    reply = await client.session.call_tool(name, arguments)
    if reply.is_error:
        raise RuntimeError(f"{name} returned a tool error")
    text = "".join(part.text for part in reply.content if isinstance(part, TextContent))
    data: dict[str, Any] = json.loads(text)
    return data


async def main() -> None:
    async with Client(streamable_http_client("http://gateway:8000/mcp"), mode="auto") as client:
        print("Connected to the gateway.", flush=True)
        quote = await tool(client, "get_quote", {"supplier_id": "S-1", "sku": "W-100"})
        budget = await tool(client, "get_budget", {"cost_center": "CC-OPS"})
        result = await tool(
            client,
            "submit_purchase_order",
            {
                "intent_id": "REQ-1",
                "supplier_id": "S-1",
                "sku": "W-100",
                "quantity": 100,
                "unit_price_minor": quote["data"]["unit_price_minor"],
                "currency": quote["data"]["currency"],
                "cost_center": "CC-OPS",
                "based_on": [quote["read_id"], budget["read_id"]],
            },
        )
        action_id = result["action_id"]
        for _ in range(10):
            if result["state"] == "confirmed":
                report = await tool(
                    client,
                    "submit_final_report",
                    {
                        "claims": [
                            {
                                "intent_id": "REQ-1",
                                "status": "done",
                                "effect_ids": [result["effect_id"]],
                            }
                        ]
                    },
                )
                print(json.dumps(report))
                return
            result = await tool(
                client, "get_action_status", {"action_id": action_id, "wait_ms": 2000}
            )
        raise RuntimeError(f"{action_id} did not confirm in the no-fault tutorial")


if __name__ == "__main__":
    asyncio.run(main())
