"""The determinism check: each demo, run twice from its seed, gives the same records.

`docs/spec.md`, Runs and determinism (record 0009): "the same scenario, configuration,
contract level, seed and model outputs produce byte-identical logs, ledgers and results,
ignoring wall-clock columns. CI runs each demo twice and compares digests."

Each demo of record 0006 runs twice in B0, G1 and G2, into two fresh databases. Record
0006 shows the demos in B0 and G2; the contract holds for every configuration. The check
reads every record that recorded playback compares (the harness's logs, the effects
ledger, the approval and action events, and the evaluation), with the same reader, and
needs each one to match row for row and by digest.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import mcp_types as types
import pytest
from langgraph.pregel import _retry

from fivefold.agents.connection import GatewayConnection
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.demo import DEMOS, DEV_001, Demo
from fivefold.harness.run import Config, InprocRun, RunSpec
from fivefold.harness.transport import Reply, Transport
from fivefold.recorder.cassette import sha256
from fivefold.recorder.playback import RECORDS, _comparable, compare, records

pytestmark = pytest.mark.pg

SEED = 7
"""The demos' committed seed."""


def run_twice(
    run_database: Callable[[str], str], demo: Demo, config: Config
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """The records of two runs of `demo` in `config` from the same seed."""
    runs: list[dict[str, list[str]]] = []
    for attempt in (1, 2):
        dsn = run_database(f"det_{demo.key}_{config.lower()}_{attempt}")
        asyncio.run(InprocRun(demo.spec(config, SEED), dsn).execute())
        runs.append(records(dsn))
    return runs[0], runs[1]


def digests(run: dict[str, list[str]]) -> dict[str, str]:
    """The SHA-256 of each record, as canonical JSON lines."""
    return {name: sha256("\n".join(rows)) for name, rows in run.items()}


@pytest.mark.parametrize("config", ["B0", "G1", "G2"])
@pytest.mark.parametrize("demo", DEMOS, ids=[demo.key for demo in DEMOS])
def test_each_demo_run_twice_from_its_seed_gives_the_same_records(
    run_database: Callable[[str], str], demo: Demo, config: Config
) -> None:
    first, second = run_twice(run_database, demo, config)
    assert compare(first, second) == ()
    assert digests(first) == digests(second)
    assert set(first) == {name for name, _ in RECORDS}, "every record playback compares"
    assert first["message log"], "the run sent messages"
    assert len(first["evaluation"]) == 1, "the run was evaluated"
    assert len(first["run stop"]) == 1, "the run stopped"


def test_another_seed_gives_other_records(run_database: Callable[[str], str]) -> None:
    """The check can fail: another seed draws other latencies, so the message log differs."""
    demo = DEMOS[0]
    runs: list[dict[str, list[str]]] = []
    for seed in (SEED, SEED + 1):
        dsn = run_database(f"det_seed_{seed}")
        asyncio.run(InprocRun(demo.spec("G2", seed), dsn).execute())
        runs.append(records(dsn))
    differing = {difference.records for difference in compare(runs[0], runs[1])}
    assert "message log" in differing


@pytest.mark.parametrize("config", ["B0", "G1", "G2"])
def test_f9_workers_with_equal_virtual_retry_times_ignore_host_jitter(
    run_database: Callable[[str], str], monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    scenario = load_scenario_file(DEV_001)
    hop = "agent_sim" if config == "B0" else "agent_gw"
    order = "submit_purchase_order"
    plan = [
        {"fault": "F9", "variant": "same_args", "at": {"point": "agent.step"}},
        *[
            {
                "fault": "F12",
                "at": {"point": f"net.{hop}.request", "occurrence": n, "match": {"api": order}},
            }
            for n in (1, 2)
        ],
    ]

    def observe(trial: int, jitter: float) -> dict[str, list[str]]:
        dsn = run_database(f"det_lockstep_{config.lower()}_{trial}")
        scheduler_run = InprocRun(RunSpec(scenario, config, Contract.C2, SEED, plan=plan), dsn)
        gate = asyncio.Event()
        arrivals: list[int] = []
        jitters: list[float] = []

        async def meet() -> None:
            if not gate.is_set():
                arrivals.append(scheduler_run.scheduler.now)
                if len(arrivals) == 2:
                    gate.set()
                # A wall-clock timer in this loop would hold virtual time and
                # prevent the second worker from reaching the gate.
                await scheduler_run.scheduler.wait_for(gate.wait(), timeout_ms=20_000)

        original_call = Transport.call
        original_tool = GatewayConnection.call_tool

        async def baseline(transport: Transport, **kwargs: Any) -> Reply:
            if kwargs["api"] == order and kwargs["hop"] == "agent_sim":
                await meet()
            return await original_call(transport, **kwargs)

        async def gateway(
            connection: GatewayConnection, name: str, arguments: dict[str, Any], timeout_ms: int
        ) -> types.CallToolResult:
            if name == order:
                await meet()
            return await original_tool(connection, name, arguments, timeout_ms)

        def uniform(low: float, high: float) -> float:
            assert (low, high) == (0, 1)
            jitters.append(jitter)
            return jitter

        with monkeypatch.context() as patch:
            patch.setattr(Transport, "call", baseline)
            patch.setattr(GatewayConnection, "call_tool", gateway)
            patch.setattr(_retry, "random", SimpleNamespace(uniform=uniform))
            asyncio.run(scheduler_run.execute())
        assert len(arrivals) == 2 and len(jitters) == 2
        trace = records(dsn)
        faults = [json.loads(row) for row in trace["fault log"]]
        drops = [row for row in faults if row["fault"] == "F12"]
        messages = {row["seq"]: row for row in map(json.loads, trace["message log"])}
        assert len(drops) == 2
        sent = [messages[drop["target_seq"]]["sent_vt"] for drop in drops]
        assert sent[0] == sent[1]
        return trace

    first, second = observe(0, 0.0), observe(1, 0.9)
    assert compare(first, second) == ()
    assert digests(first) == digests(second)


def test_the_check_ignores_wall_clock_fields_in_the_evaluation_and_nothing_else() -> None:
    """0009, as amended by 0014: a run's recovery latency by the wall clock differs from
    one run to the next; its virtual latency must not."""

    def evaluation(latency_ms: int, wall_ms: int) -> str:
        entry = {"intent_id": "REQ-1", "latency_ms": latency_ms, "latency_wall_ms": wall_ms}
        row = {"run_id": "RUN-1", "result": {"recovery": [entry], "run": {"seed": 7}}}
        return _comparable("evaluation", row)

    assert evaluation(2_257, 109) == evaluation(2_257, 1_371)
    assert evaluation(2_257, 109) != evaluation(2_258, 109)
    message = {"seq": 1, "wall_at": "now", "body": {"latency_wall_ms": 1}}
    assert _comparable("message log", message) != _comparable(
        "message log", {**message, "body": {"latency_wall_ms": 2}}
    ), "only the evaluation's own fields are left out, at any depth"


def test_the_check_sees_a_difference_in_any_record() -> None:
    """A changed row in one record is reported, with its record and row."""
    run = {name: [f'{{"row":{n}}}' for n in (1, 2)] for name, _ in RECORDS}
    changed = {**run, "effects ledger": ['{"row":1}', '{"row":3}']}
    (difference,) = compare(run, changed)
    assert (difference.records, difference.row) == ("effects ledger", 2)
    assert digests(run)["effects ledger"] != digests(changed)["effects ledger"]
