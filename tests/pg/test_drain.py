"""The run stops at the end of the drain window, even when nothing happens in it (0009).

0009, run end: the scheduler keeps running everything except the agents for a drain window
of `2 x (D + max_hop_latency) + L` virtual ms, then it stops; "run end" in the property
checks means this point. A quiet drain must still last the whole window, or property 4
judges "valid and unused at run end" too early.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.cells import Cell, cell_plan
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import InprocRun, RunSpec, drain_window_ms

pytestmark = pytest.mark.pg


def test_a_quiet_run_still_stops_a_full_drain_window_after_the_last_report(
    run_database: Callable[[str], str],
) -> None:
    """F2 at C0 in G2: the agent reports unknown, and nothing happens after that."""
    dsn = run_database("drain_quiet")
    cell = Cell(fault="F2", variant=None, contract=Contract.C0, config="G2", verdict="", failing=())
    scenario = load_scenario_file(DEV_001)
    inproc = InprocRun(RunSpec(scenario, "G2", Contract.C0, 7, plan=cell_plan(cell)), dsn)
    asyncio.run(inproc.execute())
    with psycopg.connect(dsn) as conn:
        ((last_report_vt,),) = conn.execute("SELECT max(vt) FROM harness.final_report").fetchall()
        ((stopped_vt,),) = conn.execute("SELECT stopped_vt FROM harness.run_stop").fetchall()
    assert stopped_vt == inproc.result.stopped_vt
    assert stopped_vt >= last_report_vt + drain_window_ms(scenario)
    assert inproc.result.evaluation is not None
    assert inproc.result.evaluation.run.end_vt == stopped_vt
