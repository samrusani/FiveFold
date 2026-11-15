"""The evaluator's outputs across runs, from hand-built results (0004, Metrics; 0009).

A CSV with one row per run and a Markdown report: their columns, tables and
denominators, tokens and, with a price file, each run's exact cost and the cost per
passing run, recovery latency (0014), a fixed order whatever order the results come in,
and the files they refuse. `python -m fivefold.evaluator report` writes both.
"""

from __future__ import annotations

import csv
import io
import itertools
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from langchain_core.messages.ai import UsageMetadata
from pydantic import JsonValue

from fivefold.agents.live import SpendMeter, live_name, parse_live_name
from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.prices import ModelPrice, PriceFile, price_file_from_text
from fivefold.domain.scenario import load_scenario_file, scenario_digest
from fivefold.evaluator import (
    COST_COLUMN,
    CSV_COLUMNS,
    METRICS,
    TOKEN_METRICS,
    Config,
    Destination,
    FactVersion,
    Knowledge,
    Outcome,
    RecoveryEntry,
    ReportError,
    RunInfo,
    RunRecords,
    SettledBy,
    comparable_json,
    evaluate,
    load_prices,
    load_results,
    markdown_report,
    result_json,
    run_cost,
    runs_csv,
)
from fivefold.evaluator.__main__ import main
from fivefold.evaluator.metrics import RECOVERY_METRICS
from fivefold.evaluator.report import (
    EXACT,
    RECOVERY_COLUMNS,
    SCRIPTED_MODEL,
    PlanEntry,
    cost_per_passing_run,
    load_result,
    model_price,
    plan_label,
    planned_faults,
    usd,
)
from fivefold.evaluator.result import (
    PROPERTY_NAMES,
    Failure,
    PropertyResult,
    PropertyVerdict,
    ReasonCode,
    RunResult,
    RunSummary,
    Verdict,
)
from fivefold.harness.models import SCRIPTED_MODEL as HARNESS_SCRIPTED_MODEL

ROOT = Path(__file__).resolve().parents[2]
ORDER = "submit_purchase_order"
DIGEST = "ab" * 32
B0_FAILURES = {
    1: (ReasonCode.DUPLICATE_EFFECTS,),
    2: (ReasonCode.APPROVAL_USED,),
    3: (ReasonCode.STALE_INPUT,),
}
"""How B0 fails demo 1: a second order, on the first approval, which the budget no longer
covers."""


def planned(
    fault: str,
    point: str,
    match: Mapping[str, str | int] | None = None,
    *,
    variant: str | None = None,
    occurrence: int = 1,
    params: Mapping[str, str | int] | None = None,
) -> dict[str, JsonValue]:
    """A planned fault as the harness records it in a run's params."""
    return {
        "fault": fault,
        "variant": variant,
        "at": {"point": point, "match": dict(match or {}), "occurrence": occurrence},
        "params": dict(params or {}),
    }


def lost_reply(hop: str) -> dict[str, JsonValue]:
    return planned("F2", f"net.{hop}.answer", {"api": ORDER, "answer": "committed"})


def demo_1_plan(config: str) -> list[JsonValue]:
    """Demo 1's plan: F2 on the order's answer, and F11 after the order call."""
    hop = "agent_sim" if config == "B0" else "gw_sim"
    killed = planned(
        "F11", "agent.after_tool_call", {"tool": ORDER}, params={"restart_delay_ms": 10_000}
    )
    return [lost_reply(hop), killed]


def recovered(
    latency_ms: int | None,
    wall_ms: int | None = None,
    *,
    intent_id: str = "REQ-1",
    fault: str = "F2",
    start_vt: int = 4_815,
) -> RecoveryEntry:
    """A recovery entry, resolved by a write answer `latency_ms` after the fault, or
    unresolved when `latency_ms` is None (0014)."""
    if latency_ms is None:
        return RecoveryEntry(
            intent_id=intent_id,
            fault_seq=64,
            fault=fault,
            start_vt=start_vt,
            outcome=Outcome.COMMITTED,
            settled_by=None,
            end_seq=None,
            end_vt=None,
            latency_ms=None,
            latency_wall_ms=None,
            knowledge_at_stop=Knowledge.UNKNOWABLE,
        )
    return RecoveryEntry(
        intent_id=intent_id,
        fault_seq=64,
        fault=fault,
        start_vt=start_vt,
        outcome=Outcome.COMMITTED,
        settled_by=SettledBy.WRITE_ANSWER,
        end_seq=82,
        end_vt=start_vt + latency_ms,
        latency_ms=latency_ms,
        latency_wall_ms=wall_ms,
        knowledge_at_stop=None,
    )


def make_result(
    *,
    config: str = "G2",
    contract: str = "C2",
    seed: int = 7,
    plan: list[JsonValue] | None = None,
    params: JsonValue = None,
    failing: Mapping[int, Sequence[ReasonCode]] | None = None,
    unknown: bool = False,
    evaluated: tuple[int, ...] = (1, 2, 3, 4, 5),
    metrics: Mapping[str, int] | None = None,
    code_version: str = "0.0.0",
    scenario_id: str = "dev-001-single-po",
    digest: str = DIGEST,
    model: str = "scripted",
    adapter: str = "scripted",
    cassette: str | None = None,
    parent: str | None = None,
    point: int = 12,
    recovery: Sequence[RecoveryEntry] = (),
) -> RunResult:
    """A run's result as the evaluator writes it. The plan is demo 1's unless given; with
    `params` given, the run records those params instead, plan or not. The recovery counts
    follow the recovery entries unless `metrics` sets them."""
    failing = failing or {}
    properties = tuple(
        PropertyResult(
            property=number,
            name=PROPERTY_NAMES[number],
            verdict=PropertyVerdict.FAIL if failing.get(number) else PropertyVerdict.PASS,
            reason_codes=tuple(failing.get(number, ())),
            failures=tuple(
                Failure(code=code, detail="it failed") for code in failing.get(number, ())
            ),
        )
        for number in evaluated
    )
    if any(failing.values()):
        verdict = Verdict.FAIL
    else:
        verdict = Verdict.PASS_UNRESOLVED if unknown else Verdict.PASS
    if params is None:
        params = {
            "params": {"commit_deadline_ms": 10_000},
            "plan": demo_1_plan(config) if plan is None else plan,
            "workers": 1,
            "agent_workers": 1,
        }
    return RunResult(
        code_version=code_version,
        run=RunSummary(
            run_id=f"{scenario_id}.{config}.{contract}.{seed}",
            scenario_id=scenario_id,
            scenario_digest=digest,
            config=config,
            contract=contract,
            seed=seed,
            topology="inproc",
            model_adapter=adapter,
            model=model,
            cassette_digest=cassette,
            parent_run_id=parent,
            branch_point=None if parent is None else point,
            params=params,
            end_vt=58_200,
        ),
        verdict=verdict,
        evaluated=evaluated,
        properties=properties,
        metrics={
            **dict.fromkeys(METRICS, 0),
            "affected_intents": len(recovery),
            "unresolved_intents": sum(1 for entry in recovery if not entry.resolved),
            **(metrics or {}),
        },
        excused_intents=(),
        report=None,
        recovery=tuple(recovery),
    )


def b0_demo_1(seed: int = 7) -> RunResult:
    return make_result(
        config="B0",
        seed=seed,
        failing=B0_FAILURES,
        metrics={
            "duplicates": 1,
            "unauthorized_effects": 1,
            "stale_effects": 1,
            "success_claimed_with_duplicate": 1,
            "approval_requests": 1,
        },
        recovery=[recovered(11_048, 11_160, fault="F11", start_vt=5_292)],
    )


def g2_demo_1(seed: int = 7) -> RunResult:
    return make_result(
        config="G2",
        seed=seed,
        metrics={"approval_requests": 1},
        recovery=[recovered(405, 268, fault="F11", start_vt=4_464)],
    )


def csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def section(markdown: str, title: str) -> list[str]:
    """The lines of one `##` section, without its title."""
    lines = markdown.splitlines()
    start = lines.index(f"## {title}") + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return lines[start:end]


def table(markdown: str, title: str) -> list[list[str]]:
    """The rows of the table in one section, header first, without the rule."""
    rows = [line for line in section(markdown, title) if line.startswith("|")]
    cells = [[cell.strip() for cell in row.strip("|").split(" | ")] for row in rows]
    return [cells[0], *cells[2:]]


def save(directory: Path, name: str, result: RunResult) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(result_json(result), encoding="utf-8")
    return path


# The CSV ---------------------------------------------------------------------------------


def test_the_csv_has_a_header_and_one_row_per_run() -> None:
    text = runs_csv([g2_demo_1(), b0_demo_1()])
    assert "\r" not in text
    assert text.splitlines()[0] == ",".join(CSV_COLUMNS)
    b0, g2 = csv_rows(text)
    assert b0["run_id"] == "dev-001-single-po.B0.C2.7"
    assert (b0["config"], b0["contract"], b0["seed"], b0["verdict"]) == ("B0", "C2", "7", "fail")
    assert [b0[f"p{n}_{PROPERTY_NAMES[n]}"] for n in range(1, 6)] == [
        "fail",
        "fail",
        "fail",
        "pass",
        "pass",
    ]
    assert b0["reason_codes"] == "duplicate_effects; approval_used; stale_input"
    assert b0["planned_faults"] == (
        "F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order"
        " + F11 at agent.after_tool_call when tool=submit_purchase_order"
        " with restart_delay_ms=10000"
    )
    assert [b0[name] for name in METRICS] == [
        "1",
        "0",
        "1",
        "1",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "1",
        "0",
        "1",
        "0",
        "1",
        "0",
        "0",
        "0",
    ]
    assert (b0["recovery_ms_max"], b0["recovery_wall_ms_max"]) == ("11048", "11160")
    assert (g2["config"], g2["verdict"], g2["reason_codes"]) == ("G2", "pass", "")
    assert g2["planned_faults"].startswith("F2 at net.gw_sim.answer when")
    identity = {
        "scenario_id": "dev-001-single-po",
        "scenario_digest": DIGEST,
        "topology": "inproc",
        "model_adapter": "scripted",
        "model": "scripted",
        "cassette_digest": "",
        "parent_run_id": "",
        "branch_point": "",
        "code_version": "0.0.0",
        "end_vt": "58200",
    }
    assert {name: g2[name] for name in identity} == identity


def test_the_csv_columns_are_fixed_and_name_every_metric_in_record_0004s_order() -> None:
    assert CSV_COLUMNS == (
        "run_id",
        "scenario_id",
        "scenario_digest",
        "config",
        "contract",
        "seed",
        "topology",
        "model_adapter",
        "model",
        "cassette_digest",
        "parent_run_id",
        "branch_point",
        "code_version",
        "planned_faults",
        "end_vt",
        "verdict",
        "p1_exactly_once",
        "p2_authorized_at_commit",
        "p3_inputs_still_fresh",
        "p4_actually_committed",
        "p5_truthfully_reported",
        "reason_codes",
        "duplicates",
        "unintended_effects",
        "unauthorized_effects",
        "stale_effects",
        "missing_effects",
        "excused_intents",
        "false_done_claims",
        "false_not_done_claims",
        "unjustified_claims",
        "unknown_claims",
        "success_claimed_with_duplicate",
        "human_interventions",
        "approval_requests",
        "approvals_decided_by_human",
        "affected_intents",
        "unresolved_intents",
        "tokens_in",
        "tokens_out",
        "recovery_ms_max",
        "recovery_wall_ms_max",
    )


def test_the_metrics_are_the_ones_the_evaluator_computes() -> None:
    """A guard: a metric added to the evaluator must be added to the outputs too."""
    scenario = load_scenario_file(ROOT / "scenarios" / "dev" / "dev-001-single-po.yaml")
    records = RunRecords(
        run=RunInfo(
            run_id="RUN-metrics",
            scenario_id=scenario.id,
            scenario_digest=scenario_digest(scenario),
            config=Config.G2,
            contract=Contract.C2,
            seed=7,
            topology="inproc",
            model_adapter="scripted",
            model="scripted",
        ),
        scenario=scenario,
        destination=Destination(
            scenario_id=scenario.id,
            task_id=scenario.task_id,
            capabilities=frozenset(),
            params=ContractParams(
                max_hop_latency_ms=50, commit_deadline_ms=10_000, status_lag_ms=0
            ),
        ),
        fact_versions=tuple(
            FactVersion(
                key=key,
                version=1,
                value=fact.model_dump(mode="json"),
                valid_from_seq=1,
                valid_from_vt=0,
                cause="initial",
            )
            for key, fact in scenario.facts.items()
        ),
    )
    result = evaluate(records)
    assert set(result.metrics) == set(METRICS)
    assert len(METRICS) == 18
    assert runs_csv([result]).count("\n") == 2


def test_a_run_with_no_plan_or_an_empty_plan_says_so() -> None:
    unrecorded = make_result(seed=1, params={"max_hop_latency_ms": 50})
    empty = make_result(seed=2, plan=[])
    assert planned_faults(unrecorded) is None
    assert planned_faults(empty) == ()
    rows = csv_rows(runs_csv([unrecorded, empty]))
    assert [row["planned_faults"] for row in rows] == ["none", "not recorded"]


# The report ------------------------------------------------------------------------------


def test_the_report_says_what_was_run() -> None:
    results = [
        b0_demo_1(seed=10),
        g2_demo_1(seed=8),
        make_result(config="G1", seed=7, code_version="0.1.0", cassette="cd" * 32),
        make_result(
            config="G2",
            contract="C0",
            seed=7,
            digest="ef" * 32,
            model="model-x-2026-01",
            adapter="live",
            parent="dev-001-single-po.G2.C2.7",
        ),
    ]
    lines = section(markdown_report(results), "What was run")
    assert lines == [
        "",
        "- Runs: 4.",
        "- Code versions: 0.0.0 (3 runs), 0.1.0 (1 run).",
        "- Scenarios, with the SHA-256 digest each run recorded:",
        f"  - dev-001-single-po, {DIGEST} (3 runs).",
        f"  - dev-001-single-po, {'ef' * 32} (1 run).",
        "- Configurations: B0 (1 run), G1 (1 run), G2 (2 runs).",
        "- Contract levels: C0 (1 run), C2 (3 runs).",
        "- Models: model-x-2026-01 through the live adapter (1 run),"
        " scripted through the scripted adapter (3 runs).",
        "- Seeds: 7 (2 runs), 8 (1 run), 10 (1 run).",
        "- Topologies: inproc (4 runs).",
        "- Runs that name a cassette: 1 of 4.",
        "- Runs branched from another run: 1 of 4. A branch repeats its parent's first model"
        " calls, so the tables never count it in its parent's cell: they show it in rows of"
        " its own, by configuration and branch point, the number of its parent's model calls"
        " it replayed, such as `G2, branch point 4`. Its tokens include those of the calls it"
        " replayed, and its cost prices them at its own model's prices.",
        "",
    ]


def test_verdicts_are_counted_by_configuration_and_contract_level_with_denominators() -> None:
    results = [
        b0_demo_1(seed=7),
        b0_demo_1(seed=8),
        make_result(config="B0", contract="C0", seed=7),
        make_result(config="G1", seed=7, failing={1: (ReasonCode.DUPLICATE_EFFECTS,)}),
        g2_demo_1(seed=7),
        make_result(config="G2", seed=8, unknown=True),
        make_result(config="G2", contract="C0", seed=7, unknown=True),
    ]
    assert table(markdown_report(results), "Verdicts by configuration and contract level") == [
        ["Configuration", "Contract", "pass", "pass_unresolved", "fail"],
        ["B0", "C0", "1/1", "0/1", "0/1"],
        ["B0", "C2", "0/2", "0/2", "2/2"],
        ["G1", "C2", "0/1", "0/1", "1/1"],
        ["G2", "C0", "0/1", "1/1", "0/1"],
        ["G2", "C2", "1/2", "1/2", "0/2"],
    ]


def test_safety_and_liveness_are_counted_apart_with_denominators() -> None:
    """Record 0004's headline numbers: safe when 1, 2, 3 and 5 hold, live when 4 holds."""
    results = [
        b0_demo_1(seed=7),
        make_result(config="B0", seed=8),
        make_result(config="G2", seed=7, failing={4: (ReasonCode.MISSING_EFFECT,)}),
        make_result(config="G2", seed=8, unknown=True),
        make_result(config="G2", seed=9, failing={5: (ReasonCode.DONE_UNSUPPORTED,)}),
        make_result(config="G1", seed=7, evaluated=(1, 5)),
    ]
    assert table(markdown_report(results), "Safety and liveness") == [
        ["Configuration", "Contract", "safe", "live"],
        ["B0", "C2", "1/2", "2/2"],
        ["G1", "C2", "not evaluated", "not evaluated"],
        ["G2", "C2", "2/3", "2/3"],
    ]


def test_failures_are_counted_by_property_over_the_runs_that_evaluated_it() -> None:
    results = [
        b0_demo_1(seed=7),
        make_result(config="B0", seed=8),
        make_result(config="G2", seed=7, failing={4: (ReasonCode.MISSING_EFFECT,)}),
        make_result(
            config="G2", seed=8, evaluated=(1, 5), failing={5: (ReasonCode.DONE_UNSUPPORTED,)}
        ),
        make_result(config="G1", seed=7, evaluated=(1, 5)),
    ]
    assert table(markdown_report(results), "Failures by property and configuration") == [
        [
            "Configuration",
            "1 exactly_once",
            "2 authorized_at_commit",
            "3 inputs_still_fresh",
            "4 actually_committed",
            "5 truthfully_reported",
        ],
        ["B0", "1/2", "1/2", "1/2", "0/2", "0/2"],
        ["G1", "0/1", "not evaluated", "not evaluated", "not evaluated", "0/1"],
        ["G2", "0/2", "0/1", "0/1", "1/1", "1/2"],
    ]


def test_the_fault_table_groups_runs_by_their_planned_faults_without_parameters() -> None:
    order_request = {"api": ORDER}
    status = planned("F10", "sim.status.answer", {"api": "get_order_status"}, variant="delayed")
    results = [
        make_result(
            seed=7,
            plan=[planned("F4", "net.gw_sim.request", order_request, params={"copy_delay_ms": 31})],
        ),
        make_result(
            seed=8,
            plan=[planned("F4", "net.gw_sim.request", order_request, params={"copy_delay_ms": 12})],
        ),
        make_result(seed=7, plan=[planned("F12", "net.gw_sim.request", order_request)]),
        make_result(seed=7, contract="C1", plan=[lost_reply("gw_sim"), status], unknown=True),
        make_result(seed=7, plan=[lost_reply("gw_sim")]),
        make_result(config="B0", seed=7, plan=[lost_reply("agent_sim")], failing=B0_FAILURES),
        make_result(seed=7, plan=[]),
        make_result(seed=7, params={"commit_deadline_ms": 10_000}),
    ]
    report = markdown_report(results)
    lost_b0 = "F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order"
    lost_g2 = "F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order"
    assert table(report, "Verdicts by fault plan") == [
        [
            "Faults planned",
            "Configuration",
            "Contract",
            "pass",
            "pass_unresolved",
            "fail",
            "Properties failed",
        ],
        [lost_b0, "B0", "C2", "0/1", "0/1", "1/1", "1 (1/1), 2 (1/1), 3 (1/1)"],
        [lost_g2, "G2", "C2", "1/1", "0/1", "0/1", "none"],
        [
            f"{lost_g2} + F10 delayed at sim.status.answer when api=get_order_status",
            "G2",
            "C1",
            "0/1",
            "1/1",
            "0/1",
            "none",
        ],
        [
            "F4 at net.gw_sim.request when api=submit_purchase_order",
            "G2",
            "C2",
            "2/2",
            "0/2",
            "0/2",
            "none",
        ],
        [
            "F12 at net.gw_sim.request when api=submit_purchase_order",
            "G2",
            "C2",
            "1/1",
            "0/1",
            "0/1",
            "none",
        ],
        ["none", "G2", "C2", "1/1", "0/1", "0/1", "none"],
        ["not recorded", "G2", "C2", "1/1", "0/1", "0/1", "none"],
    ]
    copies = [
        row["planned_faults"]
        for row in csv_rows(runs_csv(results))
        if row["planned_faults"].startswith("F4")
    ]
    assert copies == [
        "F4 at net.gw_sim.request when api=submit_purchase_order with copy_delay_ms=31",
        "F4 at net.gw_sim.request when api=submit_purchase_order with copy_delay_ms=12",
    ], "the CSV keeps each run's parameters, in seed order"


def test_a_planned_fault_reads_as_words() -> None:
    entry = PlanEntry.model_validate(
        planned(
            "F7",
            "gw.dispatch.before_check",
            {"worker": "agent-1", "api": ORDER},
            variant="price_up",
            occurrence=2,
            params={"percent": 5},
        )
    )
    assert entry.label() == (
        "F7 price_up at gw.dispatch.before_check when api=submit_purchase_order,"
        " worker=agent-1 occurrence 2 with percent=5"
    )
    assert entry.label(params=False) == (
        "F7 price_up at gw.dispatch.before_check when api=submit_purchase_order,"
        " worker=agent-1 occurrence 2"
    )
    assert plan_label(None) == "not recorded"
    assert plan_label(()) == "none"


def test_metric_totals_are_summed_by_configuration_with_the_number_of_runs() -> None:
    results = [
        b0_demo_1(seed=7),
        make_result(config="B0", seed=8, metrics={"duplicates": 2, "approval_requests": 2}),
        g2_demo_1(),
    ]
    rows = table(markdown_report(results), "Metric totals by configuration")
    assert rows[:3] == [["Metric", "B0", "G2"], ["runs", "2", "1"], ["duplicates", "3", "0"]]
    assert [row[0] for row in rows[2:]] == list(METRICS)
    assert ["approval_requests", "3", "1"] in rows


def test_every_metric_of_record_0004_is_computed_and_only_cost_needs_a_price_file() -> None:
    """Record 0004's list of what the report does not compute yet is empty since record
    0014: recovery latency is computed. Without a price file, cost shows no value, not even
    a zero."""
    results = [b0_demo_1(), g2_demo_1()]
    report = markdown_report(results)
    assert "Not computed yet" not in report and "not computed yet" not in report
    assert section(report, "Cost") == [
        "",
        "Cost is not computed, because no price file was given. Give one with `--prices`, in"
        " the format of `prices/example.yaml`. The tokens each run used are in the metric"
        " totals and in the CSV.",
    ]
    assert report.endswith("totals and in the CSV.\n")
    assert "USD" not in report and "passing run" not in report
    header = runs_csv(results).splitlines()[0]
    assert header.endswith("tokens_in,tokens_out,recovery_ms_max,recovery_wall_ms_max")
    assert "cost" not in header


# Recovery latency (0014) ------------------------------------------------------------------


RECOVERY_RESULTS = [
    make_result(config="G2", seed=7, recovery=[recovered(2_257, 109)]),
    make_result(config="G2", seed=8, recovery=[recovered(2_301, 2_016)]),
    make_result(config="G2", seed=9, recovery=[recovered(1_999, 500)]),
    make_result(config="G2", seed=10, recovery=[recovered(2_100, None)]),
    make_result(config="G2", contract="C0", seed=7, unknown=True, recovery=[recovered(None)]),
    make_result(
        config="B0",
        seed=7,
        plan=[lost_reply("agent_sim")],
        failing=B0_FAILURES,
        recovery=[recovered(2_013, 627)],
    ),
    make_result(
        config="B0",
        seed=8,
        plan=[lost_reply("agent_sim")],
        failing=B0_FAILURES,
        recovery=[recovered(2_013, 1_371)],
    ),
    make_result(config="G2", seed=7, plan=[]),
]
"""Demo 1 in G2 four times, its C0 run unresolved, F2 in B0 twice, and a run with no
fault. The fourth G2 run's wall-clock times are missing."""


def test_the_recovery_table_shows_counts_medians_and_the_largest_latency() -> None:
    """Medians are over resolved intents, the lower middle value for an even count; a row
    with no resolved intent shows none."""
    report = markdown_report(RECOVERY_RESULTS)
    lost_b0 = "F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order"
    demo = plan_label(planned_faults(g2_demo_1()), params=False)
    assert table(report, "Recovery latency by fault plan") == [
        [
            "Faults planned",
            "Configuration",
            "Contract",
            "affected",
            "resolved",
            "unresolved",
            "median latency (virtual ms)",
            "largest latency (virtual ms)",
            "median wall-clock latency (ms)",
        ],
        [lost_b0, "B0", "C2", "2", "2", "0", "2013", "2013", "627"],
        [demo, "G2", "C0", "1", "0", "1", "none", "none", "none"],
        [demo, "G2", "C2", "4", "4", "0", "2100", "2301", "500"],
        ["none", "G2", "C2", "0", "0", "0", "none", "none", "none"],
    ]
    lines = section(report, "Recovery latency by fault plan")
    assert "Latencies are never summed." in lines[1]
    assert report.index("## Verdicts by fault plan") < report.index("## Recovery latency")
    assert report.index("## Recovery latency") < report.index("## Metric totals")


def test_a_wall_clock_median_without_any_wall_clock_time_says_so() -> None:
    results = [make_result(seed=seed, recovery=[recovered(1_000 + seed)]) for seed in (1, 2)]
    (_, row) = table(markdown_report(results), "Recovery latency by fault plan")
    assert row[3:] == ["2", "2", "0", "1001", "1002", "not recorded"]


def test_the_csv_holds_each_run_s_largest_latencies_and_never_a_sum() -> None:
    """The largest latency among the run's resolved intents, virtual and by the wall clock;
    empty when no intent was resolved. The metric totals count intents; they never sum a
    latency."""
    two = make_result(
        seed=11,
        recovery=[
            recovered(1_200, 900, intent_id="REQ-1"),
            recovered(800, 1_300, intent_id="REQ-2"),
            recovered(None, intent_id="REQ-3"),
        ],
    )
    rows = csv_rows(runs_csv([*RECOVERY_RESULTS, two]))
    shown = ("config", "contract", "seed", *RECOVERY_COLUMNS, *RECOVERY_METRICS)
    assert [tuple(row[name] for name in shown) for row in rows] == [
        ("B0", "C2", "7", "2013", "627", "1", "0"),
        ("B0", "C2", "8", "2013", "1371", "1", "0"),
        ("G2", "C0", "7", "", "", "1", "1"),
        ("G2", "C2", "7", "2257", "109", "1", "0"),
        ("G2", "C2", "8", "2301", "2016", "1", "0"),
        ("G2", "C2", "9", "1999", "500", "1", "0"),
        ("G2", "C2", "10", "2100", "", "1", "0"),
        ("G2", "C2", "11", "1200", "1300", "3", "1"),
        ("G2", "C2", "7", "", "", "0", "0"),
    ]
    totals = table(markdown_report([*RECOVERY_RESULTS, two]), "Metric totals by configuration")
    assert ["affected_intents", "2", "8"] in totals and ["unresolved_intents", "0", "2"] in totals
    assert not any("latency" in row[0] or "recovery" in row[0] for row in totals)


def test_results_that_differ_only_in_wall_clock_fields_are_the_same_result(
    tmp_path: Path,
) -> None:
    """Two files of one run scored twice: equal but for the wall clock, so the second would
    count the run twice (0014). Another virtual latency is another result."""
    first = make_result(recovery=[recovered(2_257, 109)])
    again = make_result(recovery=[recovered(2_257, 3_296)])
    other = make_result(recovery=[recovered(2_258, 109)])
    assert result_json(first) != result_json(again)
    assert comparable_json(first) == comparable_json(again)
    save(tmp_path / "twice", "a.json", first)
    save(tmp_path / "twice", "b.json", again)
    with pytest.raises(ReportError, match=r"a\.json and .*b\.json hold the same result"):
        load_results([tmp_path / "twice"])
    save(tmp_path / "two", "a.json", first)
    save(tmp_path / "two", "b.json", other)
    assert len(load_results([tmp_path / "two"])) == 2


def test_the_wall_clock_never_decides_the_order_of_two_different_results() -> None:
    """Two runs alike down to their results: one settled by a status answer, the other by a
    write answer. In their JSON the wall-clock latency comes before `settled_by`, so
    ordering by the whole JSON would let the wall clock decide; the virtual content comes
    first instead. Two results that differ only by the wall clock still have an order."""
    by_status = recovered(2_257, 9).model_copy(update={"settled_by": SettledBy.STATUS_ANSWER})
    for status_wall, write_wall in ((9, 1), (1, 9)):
        status = make_result(
            recovery=[by_status.model_copy(update={"latency_wall_ms": status_wall})]
        )
        write = make_result(recovery=[recovered(2_257, write_wall)])
        for results in ([status, write], [write, status]):
            walls = [row["recovery_wall_ms_max"] for row in csv_rows(runs_csv(results))]
            assert walls == [str(status_wall), str(write_wall)], "status_answer sorts first"
    twins = [make_result(recovery=[recovered(2_257, wall)]) for wall in (9, 1)]
    assert runs_csv(twins) == runs_csv(twins[::-1])


@pytest.mark.parametrize(
    ("result", "why"),
    [
        (
            make_result(recovery=[recovered(405)], metrics={"affected_intents": 2}),
            "its recovery entries give 1 affected and 0 unresolved intents, but its metrics say"
            " 2 and 0",
        ),
        (
            make_result(recovery=[recovered(None)], metrics={"unresolved_intents": 0}),
            "give 1 affected and 1 unresolved intents, but its metrics say 1 and 0",
        ),
        (
            make_result(recovery=[recovered(-1)]),
            "the recovery entry of REQ-1 must have a latency of 0 ms or more exactly when it"
            " has an end",
        ),
        (
            make_result(recovery=[recovered(5).model_copy(update={"latency_ms": None})]),
            "must have a latency of 0 ms or more exactly when it has an end",
        ),
    ],
)
def test_recovery_entries_the_outputs_cannot_show_are_refused(result: RunResult, why: str) -> None:
    with pytest.raises(ReportError, match=why):
        runs_csv([result])


# Tokens and cost ---------------------------------------------------------------------------

PRICE_TEXT = (
    "price_date: 2026-11-15\n"
    "prices:\n"
    "  fake:\n"
    "    m:\n"
    "      input_usd_per_million_tokens: 0.15\n"
    "      output_usd_per_million_tokens: 0.6\n"
    "    n:\n"
    "      input_usd_per_million_tokens: 1\n"
    "      output_usd_per_million_tokens: 2\n"
    "  ollama:\n"
    '    "llama3.1:8b":\n'
    "      input_usd_per_million_tokens: 0\n"
    "      output_usd_per_million_tokens: 0\n"
)
PRICES = price_file_from_text(PRICE_TEXT)
"""Two priced fake models, at 0.15 and 0.6 USD, and at 1 and 2 USD, per million input
and output tokens, and a free local model."""


def tokens(tokens_in: int, tokens_out: int, **metrics: int) -> dict[str, int]:
    return {"tokens_in": tokens_in, "tokens_out": tokens_out, **metrics}


def live(model: str = "fake:m", **result: Any) -> RunResult:
    """A result of a live run of `model`, by default the fake provider's model `m`."""
    return make_result(model=model, adapter="live", **result)


COST_RESULTS = [
    live(config="G2", seed=7, metrics=tokens(7_000, 350)),
    live(config="G2", seed=8, unknown=True, metrics=tokens(3_000, 100)),
    live(config="G2", seed=9, failing={4: (ReasonCode.MISSING_EFFECT,)}, metrics=tokens(1_000, 0)),
    live(config="B0", seed=7, failing=B0_FAILURES, metrics=tokens(2_000, 100)),
    live("fake:n", config="G1", seed=7, metrics=tokens(1, 0)),
    live("fake:n", config="G1", seed=8),
    live("fake:n", config="G1", seed=9),
    make_result(
        config="G2",
        seed=7,
        model="ollama:llama3.1:8b",
        adapter="replay",
        cassette="cd" * 32,
        metrics=tokens(5_000, 500),
    ),
    make_result(config="G2", contract="C0", seed=7),
]
"""Runs of four models: passing, passing unresolved and failing, with a group whose cost
per passing run ends, one with no passing run, and one whose division does not end."""


def test_the_tokens_are_in_the_csv_and_summed_by_configuration() -> None:
    results = [
        live(config="G2", seed=7, metrics=tokens(7_000, 350)),
        live(config="G2", seed=8, metrics=tokens(3_000, 100)),
        b0_demo_1(),
    ]
    rows = csv_rows(runs_csv(results))
    assert [(row["config"], row["tokens_in"], row["tokens_out"]) for row in rows] == [
        ("B0", "0", "0"),
        ("G2", "7000", "350"),
        ("G2", "3000", "100"),
    ]
    totals = table(markdown_report(results), "Metric totals by configuration")
    assert totals[-2:] == [["tokens_in", "0", "10000"], ["tokens_out", "0", "450"]]


def test_a_run_s_cost_is_its_tokens_at_its_model_s_prices_exactly() -> None:
    """7,000 input tokens at 0.15 USD and 350 output tokens at 0.6 USD per million cost
    1,050 plus 210 millionths of a USD."""
    assert run_cost(live(metrics=tokens(7_000, 350)), PRICES) == Decimal("0.00126")
    assert run_cost(live(metrics=tokens(1, 0)), PRICES) == Decimal("0.00000015")
    assert run_cost(live(metrics=tokens(10**15, 10**15)), PRICES) == Decimal("750000000")
    assert run_cost(live("fake:n", metrics=tokens(3, 0)), PRICES) == Decimal("0.000003")
    assert run_cost(live("ollama:llama3.1:8b", metrics=tokens(5_000, 500)), PRICES) == 0
    assert run_cost(make_result(), PRICES) == 0, "the scripted model needs no price"
    assert usd(Decimal("0.00000015")) == "0.00000015", "plain notation, never 1.5E-7"
    assert usd(Decimal("750000000.000000")) == "750000000"
    assert usd(Decimal("0E-7")) == "0"


@given(
    calls=st.lists(st.tuples(st.integers(0, 10_000_000), st.integers(0, 10_000_000)), max_size=30),
    input_price=st.decimals(min_value=0, max_value=1_000, places=4),
    output_price=st.decimals(min_value=0, max_value=1_000, places=4),
)
def test_a_run_s_cost_is_what_the_spend_meter_charged_call_by_call(
    calls: list[tuple[int, int]], input_price: Decimal, output_price: Decimal
) -> None:
    """The report prices a run's summed tokens; the live adapter's spend meter charged each
    call as it was answered. Both are exact, so they agree to the last digit."""
    price = ModelPrice(
        input_usd_per_million_tokens=input_price, output_usd_per_million_tokens=output_price
    )
    prices = PriceFile(price_date=date(2026, 11, 15), prices={"fake": {"m": price}})
    meter = SpendMeter(Decimal(10**12), prices)
    for tokens_in, tokens_out in calls:
        usage = UsageMetadata(
            input_tokens=tokens_in, output_tokens=tokens_out, total_tokens=tokens_in + tokens_out
        )
        meter.charge("fake:m", price, usage)
    summed = tokens(sum(call[0] for call in calls), sum(call[1] for call in calls))
    assert run_cost(live(metrics=summed), prices) == meter.spent_usd


def test_a_cost_that_needs_more_digits_than_exact_has_is_refused_not_rounded() -> None:
    """A price with 55 significant digits gives a cost that 50 digits cannot hold."""
    digits = "1." + "1" * 54
    price = ModelPrice(
        input_usd_per_million_tokens=Decimal(digits), output_usd_per_million_tokens=Decimal(0)
    )
    prices = PriceFile(price_date=date(2026, 11, 15), prices={"fake": {"m": price}})
    assert EXACT.prec == 50
    with pytest.raises(ReportError, match="needs more than 50 significant digits"):
        run_cost(live(metrics=tokens(3, 0)), prices)
    assert run_cost(live(metrics=tokens(0, 9)), prices) == 0


def test_the_cost_per_passing_run_is_exact_when_the_division_ends_and_about_otherwise() -> None:
    """Rounded to 6 significant digits when the division does not end; never a zero or an
    infinity when no run passed."""
    assert cost_per_passing_run(Decimal("0.00192"), 2) == "0.00096"
    assert cost_per_passing_run(Decimal(0), 4) == "0"
    assert cost_per_passing_run(Decimal(1), 1024) == "0.0009765625"
    assert cost_per_passing_run(Decimal("0.000001"), 3) == "about 0.000000333333"
    assert cost_per_passing_run(Decimal(2), 3) == "about 0.666667"
    assert cost_per_passing_run(Decimal(1000), 7) == "about 142.857"
    assert cost_per_passing_run(Decimal("0.00036"), 0) == "no passing run"
    assert cost_per_passing_run(Decimal(0), 0) == "no passing run"


def test_with_a_price_file_the_csv_ends_with_each_run_s_exact_cost() -> None:
    text = runs_csv(COST_RESULTS, PRICES)
    assert text.splitlines()[0] == ",".join((*CSV_COLUMNS, COST_COLUMN))
    rows = csv_rows(text)
    assert [(row["model"], row["config"], row["seed"], row[COST_COLUMN]) for row in rows] == [
        ("fake:m", "B0", "7", "0.00036"),
        ("fake:n", "G1", "7", "0.000001"),
        ("fake:n", "G1", "8", "0"),
        ("fake:n", "G1", "9", "0"),
        ("scripted", "G2", "7", "0"),
        ("fake:m", "G2", "7", "0.00126"),
        ("ollama:llama3.1:8b", "G2", "7", "0"),
        ("fake:m", "G2", "8", "0.00051"),
        ("fake:m", "G2", "9", "0.00015"),
    ]
    before_cost = [line.rpartition(",")[0] for line in text.splitlines()]
    assert before_cost == runs_csv(COST_RESULTS).splitlines(), "one more column, no other change"


def test_the_cost_table_counts_runs_passing_runs_tokens_and_cost_by_model() -> None:
    report = markdown_report(COST_RESULTS, PRICES)
    lines = section(report, "Cost")
    assert lines[1] == (
        "Prices come from the price file dated 2026-11-15, in USD per million input tokens"
        " and per million output tokens (record 0004). A run's cost is its input tokens at"
        " its model's input price plus its output tokens at its output price, exactly. The"
        " scripted model costs nothing. A reply that reported no usage counts no tokens; a"
        " call the spend cap refused was never sent, so it counts none either. A passing run"
        " is one that evaluated all five properties, and all five hold: its verdict is pass"
        " or pass_unresolved. Cost per passing run is the cost of all the row's runs, passing"
        " or not, over its passing runs. It is exact when the division ends, and otherwise"
        ' rounded to 6 significant digits and marked "about".'
    )
    assert table(report, "Cost") == [
        [
            "Model",
            "Configuration",
            "Contract",
            "runs",
            "passing runs",
            "tokens in",
            "tokens out",
            "cost (USD)",
            "cost per passing run (USD)",
        ],
        ["fake:m", "B0", "C2", "1", "0", "2000", "100", "0.00036", "no passing run"],
        ["fake:m", "G2", "C2", "3", "2", "11000", "450", "0.00192", "0.00096"],
        ["fake:n", "G1", "C2", "3", "3", "1", "0", "0.000001", "about 0.000000333333"],
        ["ollama:llama3.1:8b", "G2", "C2", "1", "1", "5000", "500", "0", "0"],
        ["scripted", "G2", "C0", "1", "1", "0", "0", "0", "0"],
    ]
    assert "Cost is not computed" not in report
    assert markdown_report(COST_RESULTS).split("## Cost")[0] == report.split("## Cost")[0]


def test_a_run_that_evaluated_fewer_than_five_properties_is_never_a_passing_run() -> None:
    """0004: a passing run is one in which all five properties hold. A result that
    evaluated only properties 1 and 5 can say `pass`, and its cost still counts, but it is
    no passing run: here one of the two runs passes, so the row's cost of 0.00126 plus
    0.00051 USD is over one run."""
    results = [
        live(config="G2", seed=7, metrics=tokens(7_000, 350)),
        live(config="G2", seed=8, evaluated=(1, 5), metrics=tokens(3_000, 100)),
    ]
    assert [str(result.verdict) for result in results] == ["pass", "pass"]
    (row,) = table(markdown_report(results, PRICES), "Cost")[1:]
    assert row == ["fake:m", "G2", "C2", "2", "1", "10000", "450", "0.00177", "0.00177"]


@pytest.mark.parametrize(
    ("verdict", "failing", "why"),
    [
        (
            Verdict.PASS,
            {2: (ReasonCode.APPROVAL_USED,)},
            "its verdict is pass, but properties [2] fail",
        ),
        (
            Verdict.PASS_UNRESOLVED,
            {1: (ReasonCode.DUPLICATE_EFFECTS,), 5: (ReasonCode.DONE_UNSUPPORTED,)},
            "its verdict is pass_unresolved, but properties [1, 5] fail",
        ),
        (Verdict.FAIL, {}, "its verdict is fail, but no property it evaluated fails"),
    ],
)
def test_a_verdict_its_property_results_contradict_is_refused_in_one_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    verdict: Verdict,
    failing: Mapping[int, Sequence[ReasonCode]],
    why: str,
) -> None:
    """0004: the verdict is `fail` exactly when a property fails. A result that says
    otherwise holds records that disagree, so no table can count it."""
    result = make_result(failing=failing).model_copy(update={"verdict": verdict})
    for build in (runs_csv, markdown_report):
        with pytest.raises(ReportError, match=re.escape(f"run dev-001-single-po.G2.C2.7: {why}")):
            build([result])
    path = save(tmp_path / "results", "run.json", result)
    assert main(["report", str(tmp_path / "results"), "--csv", str(tmp_path / "runs.csv")]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"report failed: {path}: run dev-001-single-po.G2.C2.7: its verdict")
    assert err.count("\n") == 1 and not (tmp_path / "runs.csv").exists()


def test_a_run_whose_model_has_no_price_is_refused_in_one_line() -> None:
    """The first such run in the fixed order is named, whatever order the runs come in."""
    unpriced = [
        live("fake:other", config="G2", seed=8),
        live("fake:other", config="B0", seed=7),
        live("nocolon", config="G2", seed=9),
    ]
    for order in itertools.permutations([*unpriced, g2_demo_1()]):
        for build in (runs_csv, markdown_report):
            with pytest.raises(ReportError) as refused:
                build(order, PRICES)
            assert str(refused.value) == (
                "run dev-001-single-po.B0.C2.7: the price file dated 2026-11-15 has no price"
                " for its model fake:other"
            )
    with pytest.raises(ReportError, match="has no price for its model nocolon"):
        run_cost(unpriced[2], PRICES)
    assert runs_csv(unpriced) and markdown_report(unpriced), "without prices, no price is needed"


def test_a_scripted_run_that_used_tokens_is_refused() -> None:
    with pytest.raises(ReportError, match="the scripted model reports no tokens, but the run"):
        runs_csv([make_result(metrics=tokens(5, 0))], PRICES)


def test_the_report_names_models_as_the_harness_does() -> None:
    """The evaluator cannot import the harness or the agents, so it keeps its own copy of
    the scripted model's name and of the `provider:model` rule; they must agree."""
    assert HARNESS_SCRIPTED_MODEL.name == SCRIPTED_MODEL
    for provider, model in [("ollama", "llama3.1:8b"), ("fake", "m"), ("x", "a:b:c")]:
        name = live_name(provider, model)
        assert parse_live_name(name) == (provider, model)
        prices = PriceFile(
            price_date=date(2026, 11, 15),
            prices={
                provider: {
                    model: ModelPrice(
                        input_usd_per_million_tokens=Decimal(1),
                        output_usd_per_million_tokens=Decimal(2),
                    )
                }
            },
        )
        assert model_price(name, prices) == prices.price(provider, model)
    assert model_price("scripted", PRICES) is None


def test_a_negative_metric_is_refused() -> None:
    with pytest.raises(ReportError, match=r"its metrics are counts, but \['tokens_in'\]"):
        runs_csv([live(metrics=tokens(-1, 0))])


def test_the_example_price_file_prices_its_models_at_zero() -> None:
    example = load_prices(ROOT / "prices" / "example.yaml")
    run = make_result(model="ollama:qwen3:8b", adapter="live", metrics=tokens(9_000, 900))
    assert run_cost(run, example) == 0
    assert TOKEN_METRICS == ("tokens_in", "tokens_out")


# Experimental branches (0009) --------------------------------------------------------------

PARENT = "dev-001-single-po.G2.C2.7"
BRANCH_RESULTS = [
    g2_demo_1(seed=7),
    g2_demo_1(seed=8),
    live(seed=7, parent=PARENT, point=4, metrics=tokens(2_000, 100, approval_requests=1)),
    live(
        seed=7,
        parent=PARENT,
        point=4,
        failing={4: (ReasonCode.MISSING_EFFECT,)},
        metrics=tokens(1_000, 50, missing_effects=1),
    ),
    make_result(seed=7, parent=PARENT, point=10, metrics={"approval_requests": 1}),
]
"""Demo 1 in G2 with seeds 7 and 8, and three branches of the first: two to the fake
model `m` at branch point 4, one of which fails, and one to the scripted model at 10."""


def test_a_branch_never_counts_in_its_parent_s_cell() -> None:
    """A branch has its parent's configuration, contract level, seed and fault plan, so
    each table shows it in rows of its own, by its branch point; the parent's cell keeps
    only the runs that are no branch."""
    report = markdown_report(BRANCH_RESULTS, PRICES)
    assert table(report, "Verdicts by configuration and contract level")[1:] == [
        ["G2", "C2", "2/2", "0/2", "0/2"],
        ["G2, branch point 4", "C2", "1/2", "0/2", "1/2"],
        ["G2, branch point 10", "C2", "1/1", "0/1", "0/1"],
    ]
    assert table(report, "Safety and liveness")[1:] == [
        ["G2", "C2", "2/2", "2/2"],
        ["G2, branch point 4", "C2", "2/2", "1/2"],
        ["G2, branch point 10", "C2", "1/1", "1/1"],
    ]
    assert [row[:5] for row in table(report, "Failures by property and configuration")[1:]] == [
        ["G2", "0/2", "0/2", "0/2", "0/2"],
        ["G2, branch point 4", "0/2", "0/2", "0/2", "1/2"],
        ["G2, branch point 10", "0/1", "0/1", "0/1", "0/1"],
    ]
    faults = table(report, "Verdicts by fault plan")[1:]
    assert [row[1:] for row in faults] == [
        ["G2", "C2", "2/2", "0/2", "0/2", "none"],
        ["G2, branch point 4", "C2", "1/2", "0/2", "1/2", "4 (1/2)"],
        ["G2, branch point 10", "C2", "1/1", "0/1", "0/1", "none"],
    ]
    assert len({row[0] for row in faults}) == 1, "the same fault plan in every row"
    metrics = table(report, "Metric totals by configuration")
    assert metrics[:2] == [
        ["Metric", "G2", "G2, branch point 4", "G2, branch point 10"],
        ["runs", "2", "2", "1"],
    ]
    assert ["tokens_in", "0", "3000", "0"] in metrics
    assert table(report, "Cost")[1:] == [
        ["fake:m", "G2, branch point 4", "C2", "2", "1", "3000", "150", "0.00054", "0.00054"],
        ["scripted", "G2", "C2", "2", "2", "0", "0", "0", "0"],
        ["scripted", "G2, branch point 10", "C2", "1", "1", "0", "0", "0", "0"],
    ]
    assert section(report, "What was run")[-2] == (
        "- Runs branched from another run: 3 of 5. A branch repeats its parent's first model"
        " calls, so the tables never count it in its parent's cell: they show it in rows of"
        " its own, by configuration and branch point, the number of its parent's model calls"
        " it replayed, such as `G2, branch point 4`. Its tokens include those of the calls it"
        " replayed, and its cost prices them at its own model's prices."
    )


def test_a_branch_that_evaluated_fewer_than_five_properties_is_never_a_passing_run() -> None:
    """The rule for a passing run holds in a branch's own cost row: two branches say
    `pass`, but one evaluated only properties 1 and 5, so the row's cost of 0.00036 plus
    0.00018 USD is over one passing run."""
    results = [
        live(seed=7, parent=PARENT, point=4, metrics=tokens(2_000, 100)),
        live(seed=7, parent=PARENT, point=4, evaluated=(1, 5), metrics=tokens(1_000, 50)),
    ]
    assert [str(result.verdict) for result in results] == ["pass", "pass"]
    (row,) = table(markdown_report(results, PRICES), "Cost")[1:]
    assert row == [
        "fake:m",
        "G2, branch point 4",
        "C2",
        "2",
        "1",
        "3000",
        "150",
        "0.00054",
        "0.00054",
    ]


def test_the_csv_lists_a_branch_after_the_runs_of_its_configuration() -> None:
    """One row per run, with its own configuration, parent and branch point."""
    rows = csv_rows(runs_csv(reversed(BRANCH_RESULTS)))
    assert [(r["config"], r["seed"], r["parent_run_id"], r["branch_point"]) for r in rows] == [
        ("G2", "7", "", ""),
        ("G2", "8", "", ""),
        ("G2", "7", PARENT, "4"),
        ("G2", "7", PARENT, "4"),
        ("G2", "7", PARENT, "10"),
    ]


def test_a_branch_s_recovery_latency_shows_in_rows_of_its_own() -> None:
    """The recovery table groups runs as the other tables do, so a branch's recovery
    entries never count in its parent's cell (0009, 0014). The CSV keeps each run's own."""
    results = [
        g2_demo_1(seed=7),
        g2_demo_1(seed=8),
        live(seed=7, parent=PARENT, point=4, recovery=[recovered(1_200, 300)]),
        live(seed=7, parent=PARENT, point=4, recovery=[recovered(None)]),
        make_result(seed=7, parent=PARENT, point=10, recovery=[recovered(2_257, 57)]),
    ]
    assert [
        row[1:] for row in table(markdown_report(results), "Recovery latency by fault plan")[1:]
    ] == [
        ["G2", "C2", "2", "2", "0", "405", "405", "268"],
        ["G2, branch point 4", "C2", "2", "1", "1", "1200", "1200", "300"],
        ["G2, branch point 10", "C2", "1", "1", "0", "2257", "2257", "57"],
    ]
    rows = csv_rows(runs_csv(results))
    assert [(r["branch_point"], *(r[name] for name in RECOVERY_COLUMNS)) for r in rows] == [
        ("", "405", "268"),
        ("", "405", "268"),
        ("4", "1200", "300"),
        ("4", "", ""),
        ("10", "2257", "57"),
    ]


def test_a_report_without_branches_does_not_mention_branch_points() -> None:
    report = markdown_report([g2_demo_1(seed=7), b0_demo_1()])
    assert "branch point" not in report
    assert "- Runs branched from another run: 0 of 2." in report.splitlines()


# A fixed order ---------------------------------------------------------------------------


def test_the_outputs_do_not_depend_on_the_order_of_the_results() -> None:
    results = [
        b0_demo_1(seed=8),
        g2_demo_1(seed=7),
        make_result(config="G1", seed=7),
        b0_demo_1(seed=7),
        make_result(config="G2", seed=7, code_version="0.1.0"),
    ]
    expected = (runs_csv(results), markdown_report(results))
    for order in itertools.permutations(results):
        assert (runs_csv(order), markdown_report(order)) == expected
    priced = (runs_csv(COST_RESULTS, PRICES), markdown_report(COST_RESULTS, PRICES))
    for order in itertools.islice(itertools.permutations(COST_RESULTS), 0, None, 997):
        assert (runs_csv(order, PRICES), markdown_report(order, PRICES)) == priced


def test_the_outputs_do_not_depend_on_the_order_of_the_files(tmp_path: Path) -> None:
    results = [b0_demo_1(seed=8), g2_demo_1(seed=7), b0_demo_1(seed=7)]
    names = ["c.json", "a.json", "b.json"]
    paths = [
        save(tmp_path / "runs", name, result) for name, result in zip(names, results, strict=True)
    ]
    expected = (runs_csv(results), markdown_report(results))
    for order in itertools.permutations(paths):
        loaded = load_results(order)
        assert (runs_csv(loaded), markdown_report(loaded)) == expected
    loaded = load_results([tmp_path / "runs"])
    assert (runs_csv(loaded), markdown_report(loaded)) == expected


def test_a_saved_result_reads_back_as_the_same_result(tmp_path: Path) -> None:
    result = b0_demo_1()
    assert load_result(save(tmp_path, "run.json", result)) == result


# Refused input ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("{", "Invalid JSON"),
        ("[]", "Input should be an object"),
        ('{"verdict": "pass"}', "code_version: Field required"),
    ],
)
def test_a_file_that_is_not_a_run_result_is_refused(tmp_path: Path, text: str, why: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ReportError) as refused:
        load_results([path])
    assert str(refused.value).startswith(f"{path} is not a valid run result: ")
    assert why in str(refused.value)


def test_a_result_with_a_field_too_many_or_a_verdict_unknown_is_refused(tmp_path: Path) -> None:
    text = result_json(g2_demo_1())
    extra = save(tmp_path, "extra.json", g2_demo_1())
    extra.write_text(
        text.replace('"end_vt": 58200', '"end_vt": 58200, "wall_ms": 3'), encoding="utf-8"
    )
    unknown = tmp_path / "verdict.json"
    unknown.write_text(
        text.replace('"verdict": "pass"\n}', '"verdict": "passed"\n}'), encoding="utf-8"
    )
    for path, why in [(extra, "run.wall_ms: Extra inputs"), (unknown, "verdict: Input should")]:
        with pytest.raises(ReportError, match=why):
            load_results([path])


@pytest.mark.parametrize(
    ("result", "why"),
    [
        (
            make_result(metrics={"recovery_latency_vt": 0}),
            r"missing none, unknown \['recovery_latency_vt'\]",
        ),
        (make_result(params={"plan": "F2"}), "the plan in its params is not a list"),
        (
            make_result(plan=[{"fault": "F2", "variant": None}]),
            "planned fault 1 does not read as a fault: at: Field required",
        ),
    ],
)
def test_a_result_the_outputs_cannot_show_is_refused(
    tmp_path: Path, result: RunResult, why: str
) -> None:
    path = save(tmp_path, "run.json", result)
    with pytest.raises(ReportError, match=why) as refused:
        load_results([path])
    assert str(refused.value).startswith(f"{path}: run dev-001-single-po.G2.C2.7: ")
    with pytest.raises(ReportError, match=why):
        runs_csv([result])


def test_a_result_missing_a_metric_or_a_property_it_evaluated_is_refused() -> None:
    missing = make_result().model_copy(update={"metrics": {"duplicates": 0}})
    with pytest.raises(ReportError, match=r"missing \['unintended_effects'"):
        markdown_report([missing])
    short = make_result().model_copy(update={"evaluated": (1, 2, 3, 4)})
    with pytest.raises(ReportError, match=r"evaluated properties \[1, 2, 3, 4\]"):
        markdown_report([short])


def test_the_same_result_in_two_files_is_refused(tmp_path: Path) -> None:
    first = save(tmp_path, "a.json", b0_demo_1())
    shutil.copy(first, tmp_path / "b.json")
    with pytest.raises(ReportError, match=r"a\.json and .*b\.json hold the same result"):
        load_results([tmp_path])


def test_a_file_named_twice_is_read_once(tmp_path: Path) -> None:
    path = save(tmp_path, "a.json", b0_demo_1())
    save(tmp_path, "b.json", g2_demo_1())
    assert len(load_results([tmp_path, path, path])) == 2


def test_a_path_that_names_no_result_is_refused(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a result", encoding="utf-8")
    with pytest.raises(ReportError, match=r"holds no \.json file"):
        load_results([tmp_path])
    with pytest.raises(ReportError, match="is neither a file nor a directory"):
        load_results([tmp_path / "missing.json"])
    with pytest.raises(ReportError, match="no result files given"):
        load_results([])


# The command -----------------------------------------------------------------------------


def test_the_command_writes_the_csv_and_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = [b0_demo_1(), g2_demo_1(), make_result(config="G1")]
    for number, result in enumerate(results):
        save(tmp_path / "results", f"run-{number}.json", result)
    csv_path, markdown_path = tmp_path / "out" / "runs.csv", tmp_path / "out" / "report.md"
    status = main(
        [
            "report",
            str(tmp_path / "results"),
            "--csv",
            str(csv_path),
            "--markdown",
            str(markdown_path),
        ]
    )
    assert status == 0
    assert csv_path.read_bytes() == runs_csv(results).encode("utf-8")
    assert markdown_path.read_bytes() == markdown_report(results).encode("utf-8")
    assert capsys.readouterr().out == f"Reported 3 runs in {csv_path} and {markdown_path}.\n"


def test_the_command_refuses_a_bad_file_in_one_line_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save(tmp_path / "results", "good.json", g2_demo_1())
    (tmp_path / "results" / "bad.json").write_text("{", encoding="utf-8")
    csv_path = tmp_path / "runs.csv"
    status = main(["report", str(tmp_path / "results"), "--csv", str(csv_path)])
    assert status == 1
    err = capsys.readouterr().err
    assert err.startswith("report failed: ")
    assert err.count("\n") == 1
    assert not csv_path.exists()


@pytest.mark.parametrize("how", ["same path", "dotted path", "hard link", "symbolic link"])
def test_the_command_never_replaces_a_result_it_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    saved = save(tmp_path / "results", "run.json", g2_demo_1())
    before = saved.read_bytes()
    output = {
        "same path": saved,
        "dotted path": tmp_path / "results" / "new" / ".." / "run.json",
        "hard link": tmp_path / "hard.csv",
        "symbolic link": tmp_path / "soft.csv",
    }[how]
    if how == "hard link":
        output.hardlink_to(saved)
    if how == "symbolic link":
        output.symlink_to(saved)
    assert main(["report", str(tmp_path / "results"), "--csv", str(output)]) == 1
    assert "would replace the result file" in capsys.readouterr().err
    assert saved.read_bytes() == before


def test_the_command_prices_the_runs_with_a_price_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for number, result in enumerate(COST_RESULTS):
        save(tmp_path / "results", f"run-{number}.json", result)
    prices = tmp_path / "prices.yaml"
    prices.write_text(PRICE_TEXT, encoding="utf-8")
    csv_path, markdown_path = tmp_path / "out" / "runs.csv", tmp_path / "out" / "report.md"
    outputs = ["--csv", str(csv_path), "--markdown", str(markdown_path)]
    assert main(["report", str(tmp_path / "results"), *outputs, "--prices", str(prices)]) == 0
    assert csv_path.read_bytes() == runs_csv(COST_RESULTS, PRICES).encode("utf-8")
    assert markdown_path.read_bytes() == markdown_report(COST_RESULTS, PRICES).encode("utf-8")
    assert capsys.readouterr().out == f"Reported 9 runs in {csv_path} and {markdown_path}.\n"
    assert "| fake:m | G2 | C2 | 3 | 2 | 11000 | 450 | 0.00192 | 0.00096 |" in (
        markdown_path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("prices", "why"),
    [
        (
            "price_date: 2026-11-15\nprices:\n  fake:\n    other:\n"
            "      input_usd_per_million_tokens: 1\n      output_usd_per_million_tokens: 1\n",
            "run dev-001-single-po.G2.C2.7: the price file dated 2026-11-15 has no price for its"
            " model fake:m",
        ),
        ("prices: {}\n", "is not a price file: 1 validation error for PriceFile price_date"),
        ("price_date: [unclosed\n", "is not valid YAML:"),
        (b"\xff\xfe", "is not a price file: it is not UTF-8 text: invalid start byte"),
        (None, "cannot read it: No such file or directory"),
    ],
    ids=["model without a price", "no date", "not YAML", "not text", "missing"],
)
def test_the_command_refuses_a_run_or_a_price_file_it_cannot_price_in_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], prices: str | bytes | None, why: str
) -> None:
    save(tmp_path / "results", "run.json", live(metrics=tokens(7_000, 350)))
    path = tmp_path / "prices.yaml"
    if isinstance(prices, str):
        path.write_text(prices, encoding="utf-8")
    elif isinstance(prices, bytes):
        path.write_bytes(prices)
    csv_path, markdown_path = tmp_path / "runs.csv", tmp_path / "report.md"
    outputs = ["--csv", str(csv_path), "--markdown", str(markdown_path)]
    assert main(["report", str(tmp_path / "results"), *outputs, "--prices", str(path)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("report failed: ") and why in err, err
    assert err.count("\n") == 1
    assert not csv_path.exists() and not markdown_path.exists()


@pytest.mark.parametrize("how", ["same path", "hard link", "symbolic link"])
def test_the_command_never_replaces_the_price_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    saved = save(tmp_path / "results", "run.json", g2_demo_1())
    prices = tmp_path / "prices.yaml"
    prices.write_text(PRICE_TEXT, encoding="utf-8")
    output = {
        "same path": prices,
        "hard link": tmp_path / "hard.md",
        "symbolic link": tmp_path / "soft.md",
    }[how]
    if how == "hard link":
        output.hardlink_to(prices)
    if how == "symbolic link":
        output.symlink_to(prices)
    assert main(["report", str(saved), "--markdown", str(output), "--prices", str(prices)]) == 1
    assert "would replace the price file" in capsys.readouterr().err
    assert prices.read_text(encoding="utf-8") == PRICE_TEXT


@pytest.mark.parametrize("how", ["dotted path", "symbolic link"])
def test_the_command_refuses_two_outputs_that_name_one_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    saved = save(tmp_path / "results", "run.json", g2_demo_1())
    csv_path = tmp_path / "out" / "runs.csv"
    markdown_path = tmp_path / "out" / "new" / ".." / "runs.csv"
    if how == "symbolic link":
        markdown_path = tmp_path / "report.md"
        markdown_path.symlink_to(csv_path)
    status = main(["report", str(saved), "--csv", str(csv_path), "--markdown", str(markdown_path)])
    assert status == 1
    assert "name the same file" in capsys.readouterr().err
    assert not csv_path.exists()


def test_the_command_needs_somewhere_to_write(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["report", str(tmp_path)])
    assert stopped.value.code == 2
    with pytest.raises(SystemExit):
        main(["report", str(tmp_path), "--csv", "same", "--markdown", "same"])


def test_the_command_runs_as_a_module() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "fivefold.evaluator", "report", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert "--markdown PATH" in done.stdout
    assert "--prices PATH" in done.stdout
