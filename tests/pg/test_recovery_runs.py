"""Recovery latency in whole runs of dev-001 on seed 7 (record 0014).

Each row of 0014's evidence table is a cell of `faults/expected.yaml`, run with the cell's
plan: dev-001 has one intent, REQ-1, and the first fault that touched it starts its
recovery. The test pins where recovery starts and ends, what settled it, and the latency
in virtual ms. So do the record's other findings that one run shows: F6 early in G2 sends
no write, F9 in G2 at C2 ends at the first known order, and with record 0009's resolver,
F2 at C0 in G2 ends at the attestation. The three F10 cells of record 0013 log one fault
row per status answer F10 changed; stale beyond L, which fires where the order arrives,
starts recovery before F2 does.

The evaluator decides from the simulator's and the harness's records. These tests also
read the gateway's events, only to compare the end with the gateway's own first Confirmed
`vt`; `test_expected.py` does so in every G-mode cell.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, LiteralString

import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Resolver, Scenario, load_scenario_file
from fivefold.evaluator import Knowledge, Outcome, RecoveryEntry, SettledBy
from fivefold.harness.cells import Cell, cell_plan, cell_scenario
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import InprocRun, RunSpec

pytestmark = pytest.mark.pg

SEED = 7


@dataclass(frozen=True)
class Row:
    """A row of 0014's evidence table, or another finding of the record, as one cell."""

    fault: str
    variant: str | None
    contract: Contract
    config: str
    start_vt: int
    settled_by: SettledBy | None
    end_vt: int | None
    """None for an unresolved intent."""
    outcome: Outcome = Outcome.COMMITTED

    @property
    def name(self) -> str:
        parts = [self.fault, self.variant, str(self.contract), self.config]
        return "-".join(part for part in parts if part)


TABLE = [
    Row("F2", None, Contract.C2, "B0", 5_311, SettledBy.WRITE_ANSWER, 7_324),
    Row("F2", None, Contract.C2, "G1", 4_648, SettledBy.WRITE_ANSWER, 6_797),
    Row("F2", None, Contract.C2, "G2", 4_815, SettledBy.WRITE_ANSWER, 7_072),
    Row("F2", None, Contract.C0, "G2", 4_815, None, None),
    Row("F1", "a", Contract.C2, "G2", 4_788, SettledBy.WRITE_ANSWER, 6_072),
    Row("F6", "late", Contract.C2, "G2", 4_788, SettledBy.WRITE_ANSWER, 4_853),
    Row("F11", "after_tool_call", Contract.C2, "B0", 5_292, SettledBy.WRITE_ANSWER, 6_340),
    Row("F11", "after_tool_call", Contract.C2, "G2", 4_464, SettledBy.WRITE_ANSWER, 4_869),
    Row("F12", "gw_sim", Contract.C2, "G2", 4_788, SettledBy.WRITE_ANSWER, 7_061),
]
"""0014's evidence table, row by row. The latencies it gives are the end minus the start:
2,013, 2,149, 2,257, unresolved, 1,284, 65, 1,048, 405 and 2,273 ms."""

MORE = [
    Row(
        "F6",
        "early",
        Contract.C2,
        "G2",
        4_670,
        SettledBy.NO_WRITE,
        4_670,
        outcome=Outcome.NOT_COMMITTED,
    ),
    Row("F9", "same_args", Contract.C2, "G2", 1_167, SettledBy.WRITE_ANSWER, 4_982),
]
"""Two more of 0014's findings: F6 early in G2 sends no write, so 0 ms; F9 fires at the
first step, so its latency is the time to the first known order, 3,815 ms."""


def run(
    run_database: Callable[[str], str], row: Row, scenario: Scenario | None = None
) -> tuple[InprocRun, str]:
    """Run dev-001, or `scenario`, with the plan of the row's cell, on seed 7."""
    cell = Cell(
        fault=row.fault,
        variant=row.variant,
        contract=row.contract,
        config=row.config,
        verdict="",
        failing=(),
    )
    dsn = run_database(f"rec_{row.name.lower().replace('-', '_')}")
    base = scenario if scenario is not None else load_scenario_file(DEV_001)
    spec = RunSpec(cell_scenario(cell, base), row.config, row.contract, SEED, plan=cell_plan(cell))  # type: ignore[arg-type]
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    return inproc, dsn


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def entry_of(inproc: InprocRun) -> RecoveryEntry:
    """REQ-1's recovery entry: the one task intent of dev-001."""
    evaluation = inproc.result.evaluation
    assert evaluation is not None
    (entry,) = evaluation.recovery
    assert entry.intent_id == "REQ-1"
    affected = (evaluation.metrics["affected_intents"], evaluation.metrics["unresolved_intents"])
    assert affected == (1, 0 if entry.resolved else 1)
    return entry


def first_confirmed_vt(dsn: str) -> int | None:
    """When the gateway first recorded one of REQ-1's actions as Confirmed: read here only
    to compare, never by the evaluator (0004)."""
    ((vt,),) = rows(
        dsn,
        "SELECT min(e.vt) FROM gw.action_event e JOIN gw.action a USING (action_id)"
        " WHERE e.to_state = 'confirmed' AND a.intent_id = 'REQ-1'",
    )
    return None if vt is None else int(vt)


def check(row: Row, entry: RecoveryEntry) -> None:
    assert (entry.fault, entry.start_vt, entry.outcome) == (row.fault, row.start_vt, row.outcome)
    assert (entry.settled_by, entry.end_vt) == (row.settled_by, row.end_vt)
    if row.end_vt is None:
        assert (entry.end_seq, entry.latency_ms, entry.latency_wall_ms) == (None, None, None)
        return
    assert entry.latency_ms == row.end_vt - row.start_vt
    assert entry.latency_wall_ms is not None and entry.latency_wall_ms >= 0, "by the database"


@pytest.mark.parametrize("row", [*TABLE, *MORE], ids=lambda row: row.name)
def test_the_run_gives_record_0014_s_numbers(run_database: Callable[[str], str], row: Row) -> None:
    inproc, dsn = run(run_database, row)
    entry = entry_of(inproc)
    check(row, entry)
    if row.end_vt is None:
        assert entry.knowledge_at_stop is Knowledge.UNKNOWABLE, "escalated, with no resolver"
    if row.config != "B0" and entry.resolved and entry.outcome is Outcome.COMMITTED:
        assert entry.end_vt == first_confirmed_vt(dsn)


def test_the_table_s_latencies_are_the_record_s() -> None:
    latencies = [None if row.end_vt is None else row.end_vt - row.start_vt for row in TABLE]
    assert latencies == [2_013, 2_149, 2_257, None, 1_284, 65, 1_048, 405, 2_273]
    assert [row.end_vt - row.start_vt for row in MORE if row.end_vt is not None] == [0, 3_815]


def test_with_the_resolver_f2_at_c0_ends_at_the_attestation(
    run_database: Callable[[str], str],
) -> None:
    """0014: with record 0009's resolver, F2 at C0 in G2 ends at the attestation, 31,973 ms
    after the fault. The gateway confirms the order at the attestation's time."""
    scenario = load_scenario_file(DEV_001)
    authority = scenario.authority.model_copy(
        update={"resolver": Resolver(after_ms=30_000, attests="truth")}
    )
    row = Row("F2", None, Contract.C0, "G2", 4_815, SettledBy.ATTESTATION, 36_788)
    inproc, dsn = run(run_database, row, scenario.model_copy(update={"authority": authority}))
    entry = entry_of(inproc)
    check(row, entry)
    assert entry.latency_ms == 31_973
    ((seq, vt),) = rows(dsn, "SELECT seq, vt FROM harness.actor_log WHERE action = 'attest'")
    assert (entry.end_seq, entry.end_vt) == (seq, vt)
    assert entry.end_vt == first_confirmed_vt(dsn)


@dataclass(frozen=True)
class F10Cell:
    """An F10 cell of record 0013 (G2 at C1, with F2 on the order's committed answer), and
    its recovery on seed 7."""

    variant: str
    fault: str
    """The first fault that touched REQ-1: F2 as the answer drops, or F10 where the order
    arrived (stale beyond L), which comes first."""
    firing: tuple[str, str]
    """Where F10 fired: its point, and the API of the request it acted on."""
    settled_by: SettledBy | None
    end_vt: int | None
    knowledge_at_stop: Knowledge | None


F10_CELLS = [
    F10Cell(
        "delayed",
        "F2",
        ("sim.status.answer", "get_order_status"),
        SettledBy.STATUS_ANSWER,
        22_832,
        None,
    ),
    F10Cell(
        "stale_within_lag",
        "F2",
        ("sim.status.answer", "get_order_status"),
        SettledBy.STATUS_ANSWER,
        7_851,
        None,
    ),
    F10Cell(
        "stale_beyond_lag",
        "F10",
        ("sim.write.received", "submit_purchase_order"),
        None,
        None,
        Knowledge.NOT_COMMITTED,
    ),
]
"""Delayed five times, G2 hears the sixth status answer; stale within L, the first
answer lists the order. Stale beyond L hides the order from every answer, so G2 marks it
failed while it exists: the evidence never settles the committed outcome."""


@pytest.mark.parametrize("cell", F10_CELLS, ids=lambda cell: cell.variant)
def test_f10_logs_every_status_answer_it_changes_and_recovery_starts_at_the_first_fault(
    run_database: Callable[[str], str], cell: F10Cell
) -> None:
    """Each status answer F10 changed has a row of its own, with the answer as its target
    and the answer's intent, whichever variant and wherever F10 fired (0014). Every delivery
    has a wall-clock time, and no dropped message has one."""
    row = Row("F10", cell.variant, Contract.C1, "G2", 4_815, cell.settled_by, cell.end_vt)
    inproc, dsn = run(run_database, row)
    fired = rows(
        dsn,
        "SELECT f.fault, f.point, f.intent_id, m.kind, m.api, f.params"
        " FROM harness.fault_log f JOIN harness.message_log m ON m.seq = f.target_seq"
        " WHERE f.fault = 'F10' ORDER BY f.seq",
    )
    count = fired[0][5]["count"]
    assert fired[0] == (
        "F10",
        *cell.firing[:1],
        "REQ-1",
        "request",
        cell.firing[1],
        {"count": count},
    )
    answers = rows(
        dsn,
        "SELECT seq FROM harness.message_log WHERE kind = 'answer' AND api = 'get_order_status'"
        " ORDER BY seq",
    )
    changed = min(count, len(answers))
    assert fired[1:] == [
        (
            "F10",
            "sim.status.answer",
            "REQ-1",
            "answer",
            "get_order_status",
            {"count": count, "answer_no": number},
        )
        for number in range(1, changed + 1)
    ]
    assert rows(
        dsn,
        "SELECT fate, bool_and(delivered_wall_at IS NOT NULL), bool_or(delivered_wall_at IS NOT"
        " NULL) FROM harness.message_log WHERE fate IS NOT NULL GROUP BY fate ORDER BY fate",
    ) == [("delivered", True, True), ("dropped", False, False)]
    entry = entry_of(inproc)
    assert (entry.fault, entry.start_vt, entry.outcome) == (cell.fault, 4_815, Outcome.COMMITTED)
    assert (entry.settled_by, entry.end_vt, entry.knowledge_at_stop) == (
        cell.settled_by,
        cell.end_vt,
        cell.knowledge_at_stop,
    )
    assert entry.end_vt == first_confirmed_vt(dsn), "None: G2 never confirmed the hidden order"
    starts = rows(dsn, "SELECT fault FROM harness.fault_log ORDER BY seq LIMIT 1")
    assert starts == [(cell.fault,)], "the first fault by seq starts recovery"
