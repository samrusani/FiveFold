"""Whole runs with random fault plans keep I1 to I7 and can be scored (0003, 0004, 0006).

Hypothesis draws a configuration, a contract level, faults that can fire there
(`drawable_faults`), the most to draw, and a seed. Each example runs dev-001 with the
random plan `{random: {from: FAULTS, max: N}}`, which the run draws from its seed (0006,
Plans; 0009), in a fresh run database, and checks:

- in G1 and G2, that the gateway's records at the end of the run hold I1 to I7
  (`fivefold.gateway.invariants`), as G1 follows them (0003, 0008). The stores check them
  before every commit; this checks the final records once more, run closing included;
- in G1 and G2, that no tool call raised: a check that fails inside a tool call, before
  its commit, leaves no record, and the MCP layer turns it into an error answer while the
  run goes on (`TOOL_ERRORS`);
- that the evaluator scored the run from its records, all five properties (0004);
- in G2, that properties 1 and 2 hold, unless the plan holds a fault whose G2 cell at the
  run's contract level fails one of them in `faults/expected.yaml`. Those cells are the
  limits record 0006 documents: F4 on the gateway's hop to the simulator at C0 and C1,
  F5 beyond the deadline at C1, and F6 late at C0 to C2.

The last check stops at properties 1 and 2. Record 0006 documents more G2 limits for
properties 3 to 5 (F7 late `price_up`, a lost request at C0, F10 stale beyond L), and a
contract violation drawn with another fault can fail property 5 where no single cell
does: F5 beyond the deadline with F2 at C1 did in a trial run.

A run takes about a second, so the suite runs one example for every 20 of the profile's:
20 under the CI and default profiles, which derandomize or explore 100, and 100 nightly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import LiteralString

import psycopg
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp_types import CONNECTION_CLOSED
from psycopg import sql
from psycopg.conninfo import make_conninfo

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.faults.catalog import EARLY_POINTS, F5_IN_CONTRACT, FaultId, point_hop
from fivefold.faults.plan import PlannedFault, drawable_faults
from fivefold.gateway.invariants import check_invariants
from fivefold.gateway.pg_store import PgStore
from fivefold.harness.cells import load_expected
from fivefold.harness.demo import DEV_001
from fivefold.harness.migrations import drop_database
from fivefold.harness.registry import forget
from fivefold.harness.run import CONFIGS, GATEWAY_CONFIGS, InprocRun, RunSpec, run_plan
from fivefold.harness.study import MAX_SEED

DEV = load_scenario_file(DEV_001)

LIMITS = frozenset(
    (cell.fault, cell.variant, cell.contract)
    for cell in load_expected().cells
    if cell.config == "G2" and {1, 2} & set(cell.failing)
)
"""The G2 cells of faults/expected.yaml that fail property 1 or 2: record 0006's documented
limits, by fault, variant and contract level."""

TOOL_ERRORS: LiteralString = (
    "SELECT seq, body FROM harness.message_log WHERE hop = 'agent_gw' AND kind = 'answer'"
    " AND sender = 'gateway' AND body -> 'error' -> 'code' <> %s::jsonb ORDER BY seq"
)
"""The gateway's error answers to the agent, but those for a closed connection. A tool
that raises anything but its own `ToolError` ends the call with an error answer
(`fivefold.gateway.server`; mcp 2.3.0, `JSONRPCDispatcher`, code 0 for an exception it
does not know). The only other error answer in these runs is the SDK's connection-closed
answer, written when an agent's crash ends its connection under a call."""


def cell_of(entry: PlannedFault) -> tuple[str, str | None]:
    """The fault and variant of the cell `entry` is a case of, named as
    `fivefold.harness.cells` names them: F4 and F12 by their hop, F7 by its timing and its
    change, and F5 with no variant as in contract."""
    fault = str(entry.fault)
    if entry.fault in (FaultId.F4, FaultId.F12):
        return fault, point_hop(entry.at.point)
    if entry.fault is FaultId.F7:
        timing = "early" if entry.at.point in EARLY_POINTS else "late"
        return fault, f"{timing}_{entry.variant or 'price_up'}"
    if entry.fault is FaultId.F5:
        return fault, entry.variant or F5_IN_CONTRACT
    return fault, entry.variant


def test_the_limits_are_the_ones_record_0006_documents() -> None:
    """So the check below exempts exactly these, and nothing else."""
    below_c3 = (Contract.C0, Contract.C1, Contract.C2)
    documented = {
        *(("F4", "gw_sim", level) for level in (Contract.C0, Contract.C1)),
        ("F5", "beyond_deadline", Contract.C1),
        *(("F6", "late", level) for level in below_c3),
    }
    assert documented == LIMITS


@st.composite
def random_runs(draw: st.DrawFn) -> RunSpec:
    """dev-001 in a configuration and at a contract level, with a random plan of faults
    that can fire there, from a seed."""
    config = draw(st.sampled_from(CONFIGS))
    contract = draw(st.sampled_from(DEV.contracts))
    drawable = drawable_faults(scenario=DEV, config=config, contract=contract)
    faults = draw(
        st.lists(st.sampled_from(drawable), min_size=1, max_size=len(drawable), unique=True)
    )
    most = draw(st.integers(min_value=1, max_value=len(faults)))
    seed = draw(st.integers(min_value=0, max_value=MAX_SEED))
    plan = {"random": {"from": [str(fault) for fault in faults], "max": most}}
    return RunSpec(DEV, config, contract, seed, plan=plan)


@pytest.mark.pg
@settings(max_examples=max(20, settings().max_examples // 20), deadline=None)
@given(spec=random_runs())
def test_a_random_run_keeps_the_invariants_and_can_be_scored(
    admin_dsn: str,
    sim_template: str,
    run_database_name: Callable[[str, str], str],
    spec: RunSpec,
) -> None:
    name = run_database_name(sim_template, "random_run")
    dsn = make_conninfo(admin_dsn, dbname=name)
    drop_database(admin_dsn, name)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(sim_template)
            )
        )
    try:
        run = InprocRun(spec, dsn)
        asyncio.run(run.execute())
        evaluation = run.result.evaluation
        assert evaluation is not None, "the evaluator scored the run"
        assert [result.property for result in evaluation.properties] == [1, 2, 3, 4, 5]
        if spec.config in GATEWAY_CONFIGS:
            gateway = run.store.settings
            intents = {intent.id: str(intent.operation) for intent in DEV.intents}
            with PgStore(
                make_conninfo(dsn, user="ff_gw"),
                task_id=DEV.task_id,
                intents=intents,
                contract=spec.contract,
                settings=gateway,
                identity=spec.config == "G2",
            ) as store:
                assert check_invariants(store.snapshot(), gateway) == []
            with psycopg.connect(dsn) as conn:
                errors = conn.execute(TOOL_ERRORS, (str(CONNECTION_CLOSED),)).fetchall()
            assert errors == [], "a tool call raised, perhaps a failed I1 to I7 check"
        if spec.config == "G2":
            limited = {(*cell_of(entry), spec.contract) for entry in run_plan(spec)} & LIMITS
            failing = {r.property for r in evaluation.properties if str(r.verdict) == "fail"}
            assert limited or not failing & {1, 2}, f"G2 failed {sorted(failing)}"
    finally:
        drop_database(admin_dsn, name)
        forget(admin_dsn, name)
