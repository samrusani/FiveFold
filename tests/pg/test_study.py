"""A study with the scripted model: the files it saves, their verdicts, and the report
built from them (docs/plan.md, M4; 0006; 0009).

The study runs dev-001 with no fault and with F2, at C0, in B0, G1 and G2, with the seeds
7 and 8: 12 episodes, by seed and then by cell. Each runs in its own database under this
test run's prefix, dropped once the episode ends. Each result is saved under the name of
its cell and seed, and the F2 cells get the verdicts `faults/expected.yaml` expects, with
either seed. The same inputs give the same bytes. `python -m fivefold.evaluator report`
builds the CSV and the Markdown report from the directory.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from contextlib import redirect_stdout
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from fivefold.evaluator.__main__ import main as report
from fivefold.evaluator.report import load_result, planned_faults
from fivefold.harness.cells import load_expected
from fivefold.harness.migrations import drop_database
from fivefold.harness.study import main as study_main

pytestmark = pytest.mark.pg

EXPECTED = {cell.name: cell for cell in load_expected().cells}
ARGS = ["--cells", "none", "F2", "--contracts", "C0", "--episodes", "2"]
CELLS = ["none-C0-B0", "none-C0-G1", "none-C0-G2", "F2-C0-B0", "F2-C0-G1", "F2-C0-G2"]
"""The study's cells, in its order: no fault first."""
EPISODES = [f"{cell}-seed-{seed}" for seed in (7, 8) for cell in CELLS]
"""Its episodes, in the order they run: by seed, then by cell."""


def databases(admin_dsn: str, prefix: str) -> list[str]:
    """The databases a study with `prefix` names: `prefix`, an underscore and a number."""
    with psycopg.connect(admin_dsn) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname ~ %s ORDER BY datname",
            (f"^{prefix}_[0-9]+$",),
        ).fetchall()
    return [str(row[0]) for row in rows]


def run_study(admin_dsn: str, out: Path, prefix: str, args: list[str]) -> tuple[int, str]:
    """Run the study command; return its exit status and what it printed. Drop any
    database it left behind under `prefix`, whatever happens."""
    printed = io.StringIO()
    argv = ["--dsn", admin_dsn, "--out", str(out), "--database-prefix", prefix, *args]
    try:
        with redirect_stdout(printed):
            status = study_main(argv)
    finally:
        left = databases(admin_dsn, prefix)
        for name in left:
            drop_database(admin_dsn, name)
    assert left == [], "each episode's database is dropped once it ends"
    return status, printed.getvalue()


@pytest.fixture(scope="module")
def study(
    admin_dsn: str, db_prefix: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[Path, str]]:
    """The study's output directory and what the command printed."""
    out = tmp_path_factory.mktemp("study") / "results"
    status, printed = run_study(admin_dsn, out, f"{db_prefix}_study", ARGS)
    assert status == 0, printed
    yield out, printed


def test_the_study_saves_one_result_per_episode_named_for_its_cell_and_seed(
    study: tuple[Path, str],
) -> None:
    out, _ = study
    assert sorted(path.name for path in out.iterdir()) == sorted(f"{e}.json" for e in EPISODES)
    for name in EPISODES:
        result = load_result(out / f"{name}.json")
        run = result.run
        cell, _, seed = name.rpartition("-seed-")
        fault, contract, config = cell.split("-")
        assert (run.scenario_id, run.config, run.contract, run.seed) == (
            "dev-001-single-po",
            config,
            contract,
            int(seed),
        )
        assert (run.model_adapter, run.model, run.cassette_digest) == ("scripted", "scripted", None)
        plan = planned_faults(result)
        assert plan is not None
        assert [entry.fault for entry in plan] == ([] if fault == "none" else [fault])


def test_each_cell_gets_the_verdict_faults_expected_yaml_expects_with_either_seed(
    study: tuple[Path, str],
) -> None:
    out, _ = study
    for name in EPISODES:
        result = load_result(out / f"{name}.json")
        failing = tuple(prop.property for prop in result.properties if str(prop.verdict) == "fail")
        cell = name.rpartition("-seed-")[0]
        if cell.startswith("none-"):
            assert (str(result.verdict), failing) == ("pass", ()), name
        else:
            expected = EXPECTED[cell]
            assert (str(result.verdict), failing) == (expected.verdict, expected.failing), name


def test_the_command_prints_each_episode_in_order(study: tuple[Path, str]) -> None:
    out, printed = study
    lines = printed.splitlines()
    assert lines[0] == (
        "Study of dev-001-single-po with the scripted model: 12 episodes, 6 cells, seeds 7 to 8."
    )
    assert [line.split(": ")[0] for line in lines[1:13]] == [
        f"[{number}/12] {name}" for number, name in enumerate(EPISODES, start=1)
    ]
    assert lines[1] == "[1/12] none-C0-B0-seed-7: pass; agent-1 finished"
    assert lines[4] == (
        "[4/12] F2-C0-B0-seed-7: fail (1, 2, 3), expected fail (1, 2, 3); agent-1 finished"
    )
    assert lines[6] == (
        "[6/12] F2-C0-G2-seed-7: pass_unresolved, expected pass_unresolved; agent-1 finished"
    )
    assert lines[13].startswith(f"Ran all 12 episodes. Results are in {out}.")
    assert len(lines) == 14


def test_the_report_is_built_from_the_study_s_directory(
    study: tuple[Path, str], tmp_path: Path
) -> None:
    out, _ = study
    csv_path, markdown_path = tmp_path / "runs.csv", tmp_path / "report.md"
    assert (
        report(["report", str(out), "--csv", str(csv_path), "--markdown", str(markdown_path)]) == 0
    )
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
    runs = sorted(
        (row["config"], row["seed"], row["planned_faults"].partition(" at ")[0]) for row in rows
    )
    assert runs == sorted(
        (config, seed, faults)
        for config in ("B0", "G1", "G2")
        for seed in ("7", "8")
        for faults in ("none", "F2")
    )
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "- Runs: 12." in markdown
    assert "| B0 | C0 | 2/4 | 0/4 | 2/4 |" in markdown
    assert "| G1 | C0 | 2/4 | 0/4 | 2/4 |" in markdown
    assert "| G2 | C0 | 2/4 | 2/4 | 0/4 |" in markdown
    assert "| none | G2 | C0 | 2/2 | 0/2 | 0/2 | none |" in markdown


def test_two_studies_with_the_same_inputs_save_the_same_bytes(
    study: tuple[Path, str], admin_dsn: str, db_prefix: str, tmp_path: Path
) -> None:
    """0009: the same scenario, configuration, contract level, seed and model outputs give
    byte-identical results. Each study runs in databases of its own."""
    first, _ = study
    second = tmp_path / "again"
    status, _ = run_study(admin_dsn, second, f"{db_prefix}_study_again", ARGS)
    assert status == 0
    names = sorted(path.name for path in first.iterdir())
    assert sorted(path.name for path in second.iterdir()) == names
    for name in names:
        assert (second / name).read_bytes() == (first / name).read_bytes(), name


def test_the_study_keeps_each_episode_s_database_when_asked(
    admin_dsn: str, db_prefix: str, tmp_path: Path
) -> None:
    prefix = f"{db_prefix}_study_kept"
    argv = ["--dsn", admin_dsn, "--out", str(tmp_path / "kept"), "--database-prefix", prefix]
    argv += ["--cells", "F2-C2-G2", "--keep-databases"]
    printed = io.StringIO()
    try:
        with redirect_stdout(printed):
            assert study_main(argv) == 0
        assert databases(admin_dsn, prefix) == [f"{prefix}_0001"]
        with psycopg.connect(make_conninfo(admin_dsn, dbname=f"{prefix}_0001")) as conn:
            runs = conn.execute("SELECT run_id, config, contract, seed FROM harness.run").fetchall()
        assert runs == [("dev-001-single-po.G2.C2.7", "G2", "C2", 7)]
    finally:
        for name in databases(admin_dsn, prefix):
            drop_database(admin_dsn, name)
    line = printed.getvalue().splitlines()[1]
    assert line == (
        f"[1/1] F2-C2-G2-seed-7: pass, expected pass; agent-1 finished;"
        f" kept in the database {prefix}_0001"
    )
