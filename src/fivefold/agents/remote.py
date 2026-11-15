"""The scripted agent over the network: a stand-in for your own agent (0008, 0009).

In the split topology the agent is the user's own. This is the shared LangGraph agent of
record 0008 with the scripted model, reaching a split run's services as any agent would.
In B0 it calls the simulator's HTTP API with the agent's credential. Use it to check a
split setup before bringing your own agent; the tests run the split topology end to end
with it.

Its tools are the inproc agent's (`fivefold.agents.tools`), with the same timeouts: T plus
each tool's declared wait (0008). It runs on a `HostScheduler`, so the timeouts pass in
wall time. A model call takes no time: `think_ms` is virtual time the inproc scheduler
spends, and no clock here moves for it. It keeps its checkpoints in memory.

As in the inproc topology, a call with no answer within its timeout raises
`TransportTimeout`; a call whose connection fails raises `ConnectionError`; and a server
error raises `ServerError`. LangGraph's default retry policy retries all three (0008).

    python -m fivefold.agents.remote --scenario PATH --config B0 --contract C2 \\
        --simulator-url http://127.0.0.1:8100 --token-file split-run/agent-1.token

Checked in langgraph-checkpoint 4.3.2: `langgraph.checkpoint.memory.InMemorySaver`.
Checked in httpx 0.28.1: `AsyncClient(base_url=, timeout=)`, `post(url, json=, headers=)`
and `HTTPError`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from fivefold.agents.policy import Mode, scripted_policy
from fivefold.agents.scripted import ScriptedModel
from fivefold.agents.tools import ServerError, Timeouts, baseline_tools
from fivefold.agents.worker import WorkerEnd, WorkerSpec, build_graph, run_worker
from fivefold.domain.contracts import Capability, Contract
from fivefold.domain.scenario import Scenario, load_scenario_file
from fivefold.harness.clock import HostClock
from fivefold.harness.log import JsonValue
from fivefold.harness.scheduler import HostScheduler, Scheduler, SchedulerTimeout
from fivefold.harness.transport import Hop, Reply, TransportTimeout

WORKER = "agent-1"
"""The split run's one agent worker (0009)."""

Config = Literal["B0", "G1", "G2"]


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
    ) -> Reply:
        """Send one call; wait at most `timeout_ms` for its answer.

        Raises `TransportTimeout` with no answer in time, `ConnectionError` when the
        connection fails, and `ServerError` for a 5xx status.
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
    simulator_url: str | None = None
    """The simulator's service, in B0."""
    secret: str | None = None
    """The agent's credential, in B0."""


async def run_remote_agent(agent: RemoteAgent) -> tuple[WorkerEnd, str]:
    """Run the scripted agent to its end; return how it ended (`run_worker`)."""
    if agent.config != "B0":
        raise ValueError(f"{agent.config} reaches the gateway, which this agent cannot reach yet")
    if agent.simulator_url is None or agent.secret is None:
        raise ValueError("B0 reaches the simulator: give its address and the agent's credential")
    scenario = agent.scenario
    scheduler = HostScheduler(HostClock(HostClock.host_ms()))
    params = scenario.params
    timeouts = Timeouts(
        t_ms=params.dispatch_timeout_ms,
        tool_wait_ms=params.tool_wait_ms,
        max_wait_ms=params.max_wait_ms,
    )

    def think() -> Any:
        return scheduler.sleep(0)

    def policy() -> Any:
        return scripted_policy(scenario.intents, mode=Mode.BASELINE, rank=0)

    model = ScriptedModel(policy, think)
    budget = scenario.budget
    spec = WorkerSpec(WORKER, scenario.task, budget.max_agent_steps, budget.max_tokens)
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
