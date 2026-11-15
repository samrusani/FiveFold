"""The scripted agent over the network: a stand-in for your own agent (0008, 0009).

In the split topology the agent is the user's own. This is the shared LangGraph agent of
record 0008 with the scripted model, reaching a split run's services as any agent would:
in G1 and G2 the gateway's MCP endpoint over streamable HTTP, and in B0 the simulator's
HTTP API with the agent's credential. Use it to check a split setup before bringing your
own agent; the tests run the split topology end to end with it.

Its tools are the inproc agent's (`fivefold.agents.tools`), with the same timeouts: T plus
each tool's declared wait (0008). It runs on a `HostScheduler`, so the timeouts pass in
wall time. A model call takes no time: `think_ms` is virtual time the inproc scheduler
spends, and no clock here moves for it. It keeps its checkpoints in memory.

As in the inproc topology, a call whose connection to the gateway closes raises
`ConnectionError`, and the next call opens a new connection; a call with no answer within
its timeout raises `TransportTimeout`; and a server error raises `ServerError`. LangGraph's
default retry policy retries all three (0008). A call the gateway refuses because the
run's agents' phase is over raises `RunOver`, which it does not retry: the run has nothing
more for the agent.

    python -m fivefold.agents.remote --scenario PATH --config G2 --contract C2 \\
        --gateway-url http://gateway:8000/mcp
    python -m fivefold.agents.remote --scenario PATH --config B0 --contract C2 \\
        --simulator-url http://127.0.0.1:8100 --token-file split-run/agent-1.token

In G1 and G2 run it where your own agent would run: in a container on the Docker network
`fivefold-agent`, as the Compose service `agent` does, which reaches the gateway at
`http://gateway:8000/mcp` and nothing else (0008).

MCP SDK calls used (mcp 2.3.0): `mcp.client.streamable_http.streamable_http_client(url)`,
an async context manager that yields the read and write streams
(`mcp/client/streamable_http.py`); `ClientSession(read, write)` as an async context
manager, with `initialize`, `list_tools` and `call_tool`; `MCPError.code`; and
`mcp_types.CONNECTION_CLOSED` and `INTERNAL_ERROR`, the codes a call gets when its
connection closed and when the server answered with an error status. Checked in
langgraph-checkpoint 4.3.2: `langgraph.checkpoint.memory.InMemorySaver`. Checked in httpx
0.28.1: `AsyncClient(base_url=, timeout=)`, `post(url, json=, headers=)` and `HTTPError`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import mcp_types as types
from langgraph.checkpoint.memory import InMemorySaver
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from fivefold.agents.connection import within
from fivefold.agents.policy import Mode, scripted_policy
from fivefold.agents.scripted import ScriptedModel
from fivefold.agents.tools import ServerError, Timeouts, baseline_tools, gateway_tools
from fivefold.agents.worker import WorkerEnd, WorkerSpec, build_graph, run_worker
from fivefold.domain.contracts import Capability, Contract
from fivefold.domain.scenario import Scenario, load_scenario_file
from fivefold.harness.clock import HostClock
from fivefold.harness.log import JsonValue
from fivefold.harness.scheduler import HostScheduler, Scheduler, SchedulerTimeout
from fivefold.harness.split import SERVICE_STOPPED
from fivefold.harness.transport import Hop, Reply, TransportTimeout

WORKER = "agent-1"
"""The split run's one agent worker (0009)."""

Config = Literal["B0", "G1", "G2"]


class RunOver(RuntimeError):
    """The gateway answered that the run's agents' phase is over, or that the run has
    stopped (`fivefold.harness.split.SERVICE_STOPPED`): nothing the agent sends counts any
    more. A `RuntimeError`, which LangGraph's default retry policy never retries
    (langgraph 1.2.22, `langgraph/_internal/_retry.py`), so the worker ends at once."""


# G1 and G2: the gateway's MCP endpoint over streamable HTTP ------------------------------------


@dataclass
class _Session:
    session: ClientSession
    done: asyncio.Event
    ended: asyncio.Event
    holder: asyncio.Task[None]


class HttpGatewayConnection:
    """An agent worker's MCP connection to the gateway over streamable HTTP: one session at
    a time, and a new one after a connection closes."""

    def __init__(self, scheduler: Scheduler, url: str, *, timeout_ms: int) -> None:
        """`url` is the gateway's MCP endpoint; `timeout_ms` bounds each handshake request."""
        self._scheduler = scheduler
        self._url = url
        self._timeout_ms = timeout_ms
        self._current: _Session | None = None
        self._opening = asyncio.Lock()

    async def list_tools(self) -> types.ListToolsResult:
        """The gateway's tools, from `tools/list`."""
        session = await self._session()
        return await self._ask(session.list_tools(), self._timeout_ms, "tools/list")

    async def call_tool(
        self, name: str, arguments: dict[str, Any], timeout_ms: int
    ) -> types.CallToolResult:
        """Call a tool, waiting at most `timeout_ms` for its result."""
        session = await self._session()
        return await self._ask(session.call_tool(name, arguments), timeout_ms, name)

    async def _ask[T](self, call: Awaitable[T], timeout_ms: int, what: str) -> T:
        try:
            return await within(self._scheduler, call, timeout_ms, what)
        except MCPError as error:
            if error.code == SERVICE_STOPPED:
                raise RunOver(f"{what}: {error.message}") from error
            if error.code == types.CONNECTION_CLOSED:
                self.close()
                raise ConnectionError(f"{what}: the connection to the gateway closed") from error
            if error.code == types.INTERNAL_ERROR:
                raise ServerError(f"{what}: {error.message}") from error
            raise

    def close(self) -> None:
        """End the current session, if there is one."""
        current, self._current = self._current, None
        if current is not None:
            current.done.set()

    async def _session(self) -> ClientSession:
        async with self._opening:
            current = self._current
            if current is not None and not current.ended.is_set():
                return current.session
            self.close()
            loop = asyncio.get_running_loop()
            opened: asyncio.Future[ClientSession] = loop.create_future()
            done, ended = asyncio.Event(), asyncio.Event()
            holder = loop.create_task(self._hold(opened, done, ended), name="gateway session")
            session = await opened
            fresh = _Session(session, done, ended, holder)
            self._current = fresh
            try:
                await within(self._scheduler, session.initialize(), self._timeout_ms, "initialize")
            except BaseException as error:
                if self._current is fresh:
                    self.close()
                if isinstance(error, MCPError) and error.code == SERVICE_STOPPED:
                    raise RunOver(f"initialize: {error.message}") from error
                raise
            return session

    async def _hold(
        self, opened: asyncio.Future[ClientSession], done: asyncio.Event, ended: asyncio.Event
    ) -> None:
        """Keep one session open until it is no longer needed, or its connection fails."""
        try:
            async with (
                streamable_http_client(self._url) as (read, write),
                ClientSession(read, write) as session,
            ):
                opened.set_result(session)
                await done.wait()
        except Exception as error:
            if not opened.done():
                opened.set_exception(ConnectionError(f"cannot reach the gateway: {error}"))
        finally:
            ended.set()


# B0: the simulator's HTTP API ----------------------------------------------------------------


class HttpApi:
    """The simulator's API over HTTP, with the agent's credential: B0's tools' transport
    (`fivefold.harness.transport.Calls`). The report goes to the same service."""

    def __init__(self, scheduler: Scheduler, client: httpx.AsyncClient, *, secret: str) -> None:
        self._scheduler = scheduler
        self._client = client
        self._secret = secret

    async def call(
        self,
        *,
        hop: Hop,
        sender: str,
        receiver: str,
        api: str,
        body: JsonValue,
        timeout_ms: int,
        intent_id: str | None = None,
        idempotency_key: str | None = None,
        sent_vt: int | None = None,
    ) -> Reply:
        """Send one call; wait at most `timeout_ms` for its answer.

        Raises `TransportTimeout` with no answer in time, `ConnectionError` when the
        connection fails, and `ServerError` for a 5xx status. `sent_vt` changes nothing:
        the simulator logs the agent's calls as they arrive, and the agent's tools record
        no send time.
        """
        headers = {"authorization": f"Bearer {self._secret}"}
        try:
            response = await self._scheduler.wait_for(
                self._client.post(f"/api/{api}", json=body, headers=headers), timeout_ms
            )
        except SchedulerTimeout:
            raise TransportTimeout(f"{api}: no answer within {timeout_ms} ms") from None
        except httpx.HTTPError as error:
            raise ConnectionError(
                f"{api}: the connection to the simulator failed: {error}"
            ) from None
        if response.status_code >= 500:
            raise ServerError(f"{api}: the simulator answered {response.status_code}")
        return Reply(response.json())


# The agent -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteAgent:
    """The scripted agent of one split run, and where it reaches the run's services."""

    scenario: Scenario
    config: Config
    contract: Contract
    gateway_url: str | None = None
    """The gateway's MCP endpoint, in G1 and G2."""
    simulator_url: str | None = None
    """The simulator's service, in B0."""
    secret: str | None = None
    """The agent's credential, in B0."""


async def run_remote_agent(agent: RemoteAgent) -> tuple[WorkerEnd, str]:
    """Run the scripted agent to its end; return how it ended (`run_worker`)."""
    scenario = agent.scenario
    scheduler = HostScheduler(HostClock(HostClock.host_ms()))
    params = scenario.params
    timeouts = Timeouts(
        t_ms=params.dispatch_timeout_ms,
        tool_wait_ms=params.tool_wait_ms,
        max_wait_ms=params.max_wait_ms,
    )
    gateway = agent.config != "B0"
    mode = Mode.GATEWAY if gateway else Mode.BASELINE

    def think() -> Any:
        return scheduler.sleep(0)

    model = ScriptedModel(lambda: scripted_policy(scenario.intents, mode=mode, rank=0), think)
    budget = scenario.budget
    spec = WorkerSpec(WORKER, scenario.task, budget.max_agent_steps, budget.max_tokens)
    if gateway:
        if agent.gateway_url is None:
            raise ValueError(f"{agent.config} reaches the gateway: give its MCP endpoint")
        connection = HttpGatewayConnection(
            scheduler, agent.gateway_url, timeout_ms=params.dispatch_timeout_ms
        )
        try:
            tools = gateway_tools(connection, await connection.list_tools(), timeouts)
            graph = build_graph(
                model,
                tools,
                InMemorySaver(),
                max_steps=spec.max_steps,
                max_tokens=spec.max_tokens,
            )
            return await run_worker(graph, spec, resume=False)
        finally:
            connection.close()
    if agent.simulator_url is None or agent.secret is None:
        raise ValueError("B0 reaches the simulator: give its address and the agent's credential")
    async with httpx.AsyncClient(base_url=agent.simulator_url, timeout=None) as client:
        api = HttpApi(scheduler, client, secret=agent.secret)
        tools = baseline_tools(
            api, scheduler, timeouts, worker=WORKER, status=agent.contract.has(Capability.STATUS)
        )
        graph = build_graph(
            model, tools, InMemorySaver(), max_steps=spec.max_steps, max_tokens=spec.max_tokens
        )
        return await run_worker(graph, spec, resume=False)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point. Returns the process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m fivefold.agents.remote",
        description="Run the scripted agent against a split run's services.",
    )
    parser.add_argument("--scenario", type=Path, required=True, help="the run's scenario file")
    parser.add_argument("--config", choices=["B0", "G1", "G2"], required=True)
    parser.add_argument("--contract", choices=[str(level) for level in Contract], required=True)
    parser.add_argument("--gateway-url", help="the gateway's MCP endpoint (G1, G2)")
    parser.add_argument("--simulator-url", help="the simulator's service (B0)")
    parser.add_argument("--token-file", type=Path, help="the agent's credential (B0)")
    args = parser.parse_args(argv)
    secret = None
    if args.token_file is not None:
        secret = args.token_file.read_text(encoding="utf-8").strip()
    agent = RemoteAgent(
        scenario=load_scenario_file(args.scenario),
        config=args.config,
        contract=Contract(args.contract),
        gateway_url=args.gateway_url,
        simulator_url=args.simulator_url,
        secret=secret,
    )
    try:
        end, detail = asyncio.run(run_remote_agent(agent))
    except ValueError as error:
        print(f"agent: {error}", file=sys.stderr)
        return 1
    print(f"{WORKER}: {end} ({detail})")
    return 0 if end is WorkerEnd.FINISHED else 1


if __name__ == "__main__":
    raise SystemExit(main())
