"""The evaluator's outputs across runs, from hand-built results (0004, Metrics; 0009).

A CSV with one row per run and a Markdown report: their columns, tables and
denominators, a fixed order whatever order the results come in, the files they refuse,
and the metrics that are not computed yet. `python -m fivefold.evaluator report` writes
both.
"""

from __future__ import annotations

import csv
import io
import itertools
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.scenario import load_scenario_file, scenario_digest
from fivefold.evaluator import (
    CSV_COLUMNS,
    METRICS,
    Config,
    Destination,
    FactVersion,
    ReportError,
    RunInfo,
    RunRecords,
    config_hash,
    evaluate,
    load_results,
    markdown_report,
    result_json,
    runs_csv,
)
from fivefold.evaluator.__main__ import main
from fivefold.evaluator.report import PlanEntry, load_result, plan_label, planned_faults
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
) -> RunResult:
    """A run's result as the evaluator writes it. The plan is demo 1's unless given; with
    `params` given, the run records those params instead, plan or not."""
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
            branch_point=None if parent is None else 12,
            config_hash=config_hash(
                config=config, contract=contract, topology="inproc", params=params
            ),
            params=params,
            end_vt=58_200,
        ),
        verdict=verdict,
        evaluated=evaluated,
        properties=properties,
        metrics={**dict.fromkeys(METRICS, 0), **(metrics or {})},
        excused_intents=(),
        report=None,
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
    )


def g2_demo_1(seed: int = 7) -> RunResult:
    return make_result(config="G2", seed=seed, metrics={"approval_requests": 1})


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
    ]
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
        "config_hash": config_hash(
            config="G2", contract="C2", topology="inproc", params=g2_demo_1().run.params
        ),
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
        "config_hash",
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
    assert len(METRICS) == 14
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
        "- Runs branched from another run: 1 of 4.",
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


def test_the_report_says_what_is_not_computed_and_shows_no_value_for_it() -> None:
    results = [b0_demo_1(), g2_demo_1()]
    report = markdown_report(results)
    lines = section(report, "Not computed yet")
    assert lines[1] == (
        "Record 0004 also lists these metrics, which the evaluator does not compute yet:"
        " recovery latency per affected action, in virtual and wall milliseconds; tokens in"
        " and out; cost, from a price file that records its price date; cost per passing"
        " run. No result holds a value for them, so this report shows none, not zeros."
    )
    elsewhere = report.split("## Not computed yet")[0].lower()
    assert not any(word in elsewhere for word in ("latency", "token", "cost"))
    header = runs_csv(results).splitlines()[0]
    assert not any(word in header for word in ("latency", "token", "cost"))


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
