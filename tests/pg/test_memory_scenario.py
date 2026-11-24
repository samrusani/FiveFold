"""Scenario-selected memory on each route and at each check of the write basis."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from fivefold.agents.tools import Timeouts, baseline_tools
from fivefold.domain.api import FactRead, GetMemory, Refusal, RefusalReason
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.rng import Generators
from fivefold.harness.run import Config, InprocRun, RunSpec
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.split.faults import SplitRefused
from fivefold.harness.split.lifecycle import start_run
from fivefold.harness.transport import Outgoing, Transport
from fivefold.paths import DEV_001, DEV_MEMORY_PO
from fivefold.sim import Simulator

pytestmark = pytest.mark.pg

KEY = "memory/S-1/W-100"
CONFIGS: tuple[Config, ...] = ("B0", "B1", "B2", "G1", "G2")


def test_direct_reads_and_tool_list(make_sim: Callable[..., Simulator]) -> None:
    scheduler = Scheduler()
    transport = Transport(
        scheduler, MemoryHarnessLog(), Generators(7).get("latency"), max_hop_latency_ms=50
    )
    dev = load_scenario_file(DEV_001)
    memory = load_scenario_file(DEV_MEMORY_PO)
    original, expanded = [
        [
            tool.name
            for tool in baseline_tools(
                transport,
                scheduler,
                Timeouts(t_ms=2_000, tool_wait_ms=2_500),
                worker="agent-1",
                status=False,
                memory_enabled=scenario.memory_enabled,
            )
        ]
        for scenario in (dev, memory)
    ]
    assert original == [
        "get_quote",
        "list_suppliers",
        "get_budget",
        "get_inventory",
        "request_approval",
        "get_approval_status",
        "submit_purchase_order",
        "reserve_inventory",
        "submit_final_report",
    ]
    assert expanded == [*original, "get_memory"]

    sim = make_sim(data=memory.model_dump(mode="json"), credentials=("agent-1",))
    request = GetMemory(supplier_id="S-1", sku="W-100")
    first = sim.get_memory(request, caller="agent-1", vt=1)
    assert isinstance(first, FactRead)
    assert first.model_dump() == {
        "answer": "fact",
        "key": KEY,
        "version": 1,
        "data": {"order_channel": "edi", "notes": "Use the standard order channel."},
    }
    corrected = sim.memory.correct(KEY, {"order_channel": "portal"}, actor="test", vt=2)
    assert corrected.version == 2
    assert sim.get_memory(request, caller="agent-1", vt=3) == corrected
    missing = sim.get_memory(GetMemory(supplier_id="S-2", sku="W-100"), caller="agent-1", vt=4)
    assert isinstance(missing, Refusal) and missing.reason is RefusalReason.NOT_FOUND


@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("contract", list(Contract))
def test_fault_free_cells(
    run_database: Callable[[str], str], admin_dsn: str, config: Config, contract: Contract
) -> None:
    scenario = load_scenario_file(DEV_MEMORY_PO)
    dsn = run_database(f"memory_clean_{config.lower()}_{str(contract).lower()}")
    spec = RunSpec(scenario, config, contract, seed=7, plan=[])
    run = InprocRun(spec, dsn, admin_dsn=admin_dsn)
    result = asyncio.run(run.execute()).evaluation
    assert result is not None and result.verdict == "pass"
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT basis FROM sim.effect").fetchall()[0][0][KEY] == 1
        caller = "gateway" if config in ("G1", "G2") else "agent-1"
        assert (1, caller) in connection.execute(
            "SELECT version, caller FROM sim.read_log WHERE key = %s", (KEY,)
        ).fetchall()


class CorrectBeforeOrder(InprocRun):
    """Change the cited channel before the order reaches the gateway or simulator."""

    corrected = False

    def _before_tool_call(self, outgoing: Outgoing) -> None:
        if (
            not self.corrected
            and outgoing.kind == "request"
            and outgoing.sender == "agent-1"
            and outgoing.api == "submit_purchase_order"
        ):
            self.simulator.memory.correct(
                KEY, {"order_channel": "portal"}, actor="test", vt=self.scheduler.now
            )
            self.corrected = True
        super()._before_tool_call(outgoing)


@pytest.mark.parametrize(
    ("config", "contract", "states", "version"),
    [
        ("B0", Contract.C2, ["committed"], 1),
        ("B0", Contract.C3, ["rejected", "committed"], 2),
        ("G2", Contract.C2, ["committed"], 2),
    ],
)
def test_gateway_commit_and_evaluator_use_the_same_memory_rule(
    run_database: Callable[[str], str],
    config: Config,
    contract: Contract,
    states: list[str],
    version: int,
) -> None:
    scenario = load_scenario_file(DEV_MEMORY_PO)
    dsn = run_database(f"memory_stale_{config.lower()}_{str(contract).lower()}")
    run = CorrectBeforeOrder(RunSpec(scenario, config, contract, seed=7, plan=[]), dsn)
    evaluation = asyncio.run(run.execute()).evaluation
    assert run.corrected and evaluation is not None
    failures = [(p.property, str(f.code)) for p in evaluation.properties for f in p.failures]
    assert failures == ([(3, "stale_input")] if version == 1 else [])
    with psycopg.connect(dsn) as connection:
        writes = connection.execute("SELECT state, basis FROM sim.write ORDER BY received_seq")
        assert [(state, basis[KEY]) for state, basis in writes] == list(
            zip(states, [1, 2] if len(states) == 2 else [version], strict=True)
        )
        assert connection.execute("SELECT basis FROM sim.effect").fetchall()[0][0][KEY] == version


def test_split_start_refuses_before_creating_resources(tmp_path: Path) -> None:
    run_dir = tmp_path / "split-run"
    with pytest.raises(SplitRefused, match="split topology does not support memory-enabled"):
        start_run(
            admin_dsn="unused",
            scenario_path=DEV_MEMORY_PO,
            config="B0",
            contract=Contract.C2,
            seed=7,
            run_dir=run_dir,
        )
    assert not run_dir.exists()
