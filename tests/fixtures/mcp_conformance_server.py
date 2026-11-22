"""Two disposable MCP fixtures for record 0019, sharing an in-memory synthetic ledger."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer


def fixture(
    deduplicate: bool,
    hooks: bool,
    audit: Path | None,
    *,
    bad_probe: bool = False,
    unsettled: bool = False,
) -> MCPServer[None]:
    server = MCPServer[None]("synthetic-ledger", version="1", log_level="ERROR")
    ledger: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    armed: set[str] = set()
    held: dict[str, asyncio.Event] = {}

    @server.tool()
    async def add(
        namespace: str, operation_id: str, value: str, key: str | None = None
    ) -> dict[str, object]:
        if audit is not None:
            with audit.open("a", encoding="utf-8") as output:
                output.write(json.dumps([namespace, operation_id, key]) + "\n")
        if namespace in armed:
            armed.remove(namespace)
            event = held[namespace] = asyncio.Event()
            try:
                await event.wait()
                return {"count": 99}
            finally:
                del held[namespace]
        identity = (namespace, key or operation_id)
        if not deduplicate or identity not in seen:
            ledger.setdefault(namespace, []).append(value)
            seen.add(identity)
        # A tool reply alone cannot establish the number of committed effects.
        return {"count": 99}

    @server.tool()
    def read(namespace: str, expected: str) -> dict[str, object]:
        if bad_probe:
            return {"count": "private invalid probe value"}
        effects = ledger.get(namespace, [])
        return {
            "count": len(effects),
            "matches": all(effect == expected for effect in effects),
            "settled": not unsettled and namespace not in held,
        }

    if hooks:

        @server.tool()
        def arm(namespace: str) -> dict[str, bool]:
            armed.add(namespace)
            return {"armed": True}

        @server.tool()
        def holding(namespace: str) -> dict[str, bool]:
            return {"held": namespace in held}

        @server.tool()
        def abort(namespace: str) -> dict[str, bool]:
            event = held.get(namespace)
            if event is not None:
                event.set()
            return {"aborted": event is not None}

    return server


async def http(server: MCPServer[None]) -> None:
    app = server.streamable_http_app(json_response=True)
    host = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        async with asyncio.TaskGroup() as group:
            group.create_task(host.serve(sockets=[listener]))
            async with asyncio.timeout(10):
                while not host.started:
                    await asyncio.sleep(0.01)
            print(listener.getsockname()[1], flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", choices=["deduplicating", "repeating"])
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--hooks", action="store_true")
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--bad-probe", action="store_true")
    parser.add_argument("--unsettled", action="store_true")
    args = parser.parse_args()
    server = fixture(
        args.fixture == "deduplicating",
        args.hooks,
        args.audit,
        bad_probe=args.bad_probe,
        unsettled=args.unsettled,
    )
    if args.http:
        asyncio.run(http(server))
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
