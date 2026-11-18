"""Demo 1's two runs, saved and reported across runs (0004, Metrics; 0009).

`python -m fivefold.harness.demo 1 --out DIR` runs demo 1 in B0 and G2 and saves each
run's result JSON. `python -m fivefold.evaluator report` builds the CSV and the Markdown
report from those files. B0 places a second order and fails properties 1, 2 and 3; G2
passes.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from fivefold.evaluator import load_results, markdown_report, runs_csv
from fivefold.evaluator.__main__ import main as report
from fivefold.harness import demo
from fivefold.harness.demo import Demo
from fivefold.harness.migrations import drop_database
from fivefold.harness.run import Config

pytestmark = pytest.mark.pg


def test_demo_1_saves_both_results_and_the_report_counts_them(
    admin_dsn: str, db_prefix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[str] = []

    def prefixed(example: Demo, config: Config) -> str:
        """The demo's database, under this test run's prefix like every test database."""
        name = f"{db_prefix}_fivefold_demo_{example.key}_{config.lower()}"
        if name not in made:
            made.append(name)
        return name

    monkeypatch.setattr(demo, "_demo_database", prefixed)
    results = tmp_path / "results"
    try:
        assert demo.main(["1", "--seed", "7", "--dsn", admin_dsn, "--out", str(results)]) == 0
    finally:
        for name in made:
            drop_database(admin_dsn, name)
    files = sorted(results.iterdir())
    assert [path.name for path in files] == ["demo-1-B0-seed-7.json", "demo-1-G2-seed-7.json"]

    csv_path, markdown_path = tmp_path / "runs.csv", tmp_path / "report.md"
    command = ["report", str(results), "--csv", str(csv_path), "--markdown", str(markdown_path)]
    assert report(command) == 0

    b0, g2 = csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8")))
    assert (b0["config"], b0["contract"], b0["verdict"]) == ("B0", "C2", "fail")
    assert (g2["config"], g2["contract"], g2["verdict"]) == ("G2", "C2", "pass")
    assert b0["run_id"] == "dev-001-single-po.B0.C2.7"
    assert b0["reason_codes"] == "duplicate_effects; approval_used; stale_input"
    assert g2["reason_codes"] == ""
    assert (b0["duplicates"], b0["success_claimed_with_duplicate"]) == ("1", "1")
    assert (g2["duplicates"], g2["success_claimed_with_duplicate"]) == ("0", "0")
    assert g2["planned_faults"] == (
        "F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order"
        " + F11 at agent.after_tool_call when tool=submit_purchase_order"
        " with restart_delay_ms=10000"
    )

    markdown = markdown_path.read_text(encoding="utf-8")
    assert "| B0 | C2 | 0/1 | 0/1 | 1/1 |" in markdown
    assert "| G2 | C2 | 1/1 | 0/1 | 0/1 |" in markdown
    assert "| B0 | 1/1 | 1/1 | 1/1 | 0/1 | 0/1 |" in markdown
    assert "| G2 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 |" in markdown
    assert "| duplicates | 1 | 0 |" in markdown
    assert "| B0 | C2 | 0/1 | 1/1 |" in markdown, "B0 is live but not safe"
    assert "| G2 | C2 | 1/1 | 1/1 |" in markdown, "G2 is safe and live"

    loaded = load_results(reversed(files))
    assert runs_csv(loaded).encode("utf-8") == csv_path.read_bytes()
    assert markdown_report(loaded).encode("utf-8") == markdown_path.read_bytes()
