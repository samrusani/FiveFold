"""Record 0022 through the CLI, with actual evaluator and PostgreSQL records."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.evaluator.result import ReasonCode, RunResult, Verdict, result_json
from fivefold.harness.cells import cell_plan, load_expected
from fivefold.harness.migrations import (
    CreatedDatabase,
    DatabaseExists,
    create_database,
    registry_database,
)
from fivefold.harness.registry import database_of
from fivefold.harness.run import InprocRun, RunSpec
from fivefold.harness.run import RunResult as Execution
from fivefold.harness.study import as_config, drop_own_database
from fivefold.paths import DEV_001
from fivefold.recorder.cassette import sha256
from fivefold.recorder.playback import records
from fivefold.shrink import runner
from fivefold.shrink.__main__ import main
from fivefold.shrink.search import Signature
from fivefold.viewer.source import RunSource, ViewerError

pytestmark = pytest.mark.pg
SCENARIO = load_scenario_file(DEV_001)
CELLS = {cell.name: cell for cell in load_expected().cells}


def _scored(dsn: str, spec: RunSpec) -> RunResult:
    result = asyncio.run(InprocRun(spec, dsn).execute()).evaluation
    assert result is not None and result.verdict is Verdict.FAIL
    return result


@pytest.fixture
def original(run_database: Callable[[str], str]) -> tuple[str, RunResult]:
    dsn = run_database("shrink_source")
    plan = cell_plan(CELLS["F2-C2-B0"]) + cell_plan(CELLS["F7-late_harmless-C2-B0"])
    return dsn, _scored(dsn, RunSpec(SCENARIO, "B0", Contract.C2, 7, plan=plan))


def _json(out: Path) -> dict[str, Any]:
    return json.loads((out / "shrink.json").read_bytes())  # type: ignore[no-any-return]


def _faults(out: Path) -> list[dict[str, Any]]:
    return yaml.safe_load((out / "faults.yaml").read_text())  # type: ignore[no-any-return]


def _prefix(out: Path) -> str:
    return "fivefold_shrink_" + sha256(str(out.resolve()))[:16]


def _databases(admin_dsn: str, out: Path) -> list[str]:
    with psycopg.connect(admin_dsn) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT datname FROM pg_database WHERE starts_with(datname, %s) ORDER BY datname",
                (_prefix(out),),
            )
        ]


def test_cli_shrinks_a_saved_result_and_a_registered_run(
    original: tuple[str, RunResult], admin_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn, result = original
    name = database_of(dsn)
    viewer = RunSource(admin_dsn, registry_database())
    before = records(dsn)
    registered = viewer.registered(name)
    source = tmp_path / "source.json"
    source.write_text(result_json(result))
    monkeypatch.setenv("FIVEFOLD_ADMIN_DSN", admin_dsn)
    one, two = tmp_path / "file", tmp_path / "database"
    assert main(["--result", str(source), "--scenario", str(DEV_001), "--out", str(one)]) == 0
    assert (
        main(["--database", name, "--scenario", str(DEV_001), "--out", str(two), "--keep-database"])
        == 0
    )
    first, second = _json(one), _json(two)
    kept = second["final_database"]
    try:
        assert [entry["fault"] for entry in _faults(one)] == ["F2"]
        assert _faults(one) == _faults(two)
        assert first["candidate_count"] == second["candidate_count"] == 3
        assert first["candidates"] == second["candidates"]
        assert first["final_record_digests"] == second["final_record_digests"]
        assert first["max_candidates"] == 100 and first["wall_time_limit_s"] is None
        assert (one / "records.json").read_bytes() == (two / "records.json").read_bytes()
        assert _databases(admin_dsn, one) == [] and _databases(admin_dsn, two) == [kept]
        assert viewer.registered(kept)["run_id"] == result.run.run_id
        assert viewer.registered(name) == registered
        assert records(dsn) == before
        assert source.read_text() == result_json(result)
    finally:
        with psycopg.connect(admin_dsn) as conn:
            row = conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (kept,)).fetchone()
        if row is not None:
            drop_own_database(admin_dsn, CreatedDatabase(kept, int(row[0]), ""))


@pytest.mark.parametrize("config", ["G1", "G2"])
def test_a_minimal_single_fault_remains_in_each_gateway_configuration(
    config: str, run_database: Callable[[str], str], admin_dsn: str, tmp_path: Path
) -> None:
    cell = CELLS[f"F7-late_price_up-C2-{config}"]
    plan = cell_plan(cell)
    plan[0]["params"] = {"percent": 1}
    result = _scored(
        run_database("shrink_gateway"),
        RunSpec(SCENARIO, as_config(config), Contract.C2, 7, plan=plan),
    )
    out = tmp_path / "gateway"
    report = runner.shrink(result, SCENARIO, out, admin_dsn=admin_dsn)
    assert _faults(out)[0]["fault"] == "F7" and _faults(out)[0]["params"] == {"percent": 1}
    assert report["source_plan_sha256"] == report["final_plan_sha256"]
    assert report["candidate_count"] == 1


def test_timed_stop_repeats_by_count_and_keeps_a_verified_plan(
    original: tuple[str, RunResult], admin_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, result = original
    timed, repeated, zero = tmp_path / "timed", tmp_path / "repeated", tmp_path / "zero"
    elapsed = iter((0.0, 0.0, 0.0, 1001.0, 1001.0))
    with monkeypatch.context() as patch:
        patch.setattr(runner, "wall_ms", lambda: next(elapsed))
        report = runner.shrink(result, SCENARIO, timed, admin_dsn=admin_dsn, wall_time_s=1)
    assert report["budget_exhausted"] is True and report["candidate_count"] == 2
    rerun = runner.shrink(result, SCENARIO, repeated, admin_dsn=admin_dsn, max_candidates=2)
    assert rerun["budget_exhausted"] is True and report["candidates"] == rerun["candidates"]
    assert report["next_candidate"] == rerun["next_candidate"] == []
    assert _faults(timed) == _faults(repeated)
    assert [entry["fault"] for entry in _faults(timed)] == ["F2"]
    assert (timed / "records.json").read_bytes() == (repeated / "records.json").read_bytes()
    exhausted = runner.shrink(result, SCENARIO, zero, admin_dsn=admin_dsn, max_candidates=0)
    assert exhausted["budget_exhausted"] and exhausted["candidate_count"] == 0
    assert exhausted["final_plan_sha256"] == exhausted["source_plan_sha256"]


def test_a_seeded_random_plan_uses_its_recorded_entries(
    run_database: Callable[[str], str], admin_dsn: str, tmp_path: Path
) -> None:
    spec = RunSpec(SCENARIO, "B0", Contract.C2, 7, plan={"random": {"from": ["F2"], "max": 1}})
    result = _scored(run_database("shrink_random"), spec)
    out = tmp_path / "random"
    report = runner.shrink(result, SCENARIO, out, admin_dsn=admin_dsn)
    assert [entry["fault"] for entry in _faults(out)] == ["F2"]
    assert report["source_plan_sha256"] == report["final_plan_sha256"]


def test_shrinks_a_failing_f5_delay_to_the_legal_minimum(
    run_database: Callable[[str], str], admin_dsn: str, tmp_path: Path
) -> None:
    spec = RunSpec(
        SCENARIO, "B0", Contract.C2, 7, plan=cell_plan(CELLS["F5-beyond_deadline-C2-B0"])
    )
    result = _scored(run_database("shrink_delay"), spec)
    out = tmp_path / "delay"
    report = runner.shrink(result, SCENARIO, out, admin_dsn=admin_dsn)
    assert _faults(out)[0]["params"] == {"delay_ms": SCENARIO.params.commit_deadline_ms + 1}
    assert report["candidate_count"] == 2


def test_taken_database_survives_and_failed_candidate_is_removed(
    original: tuple[str, RunResult], admin_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, result = original
    taken = tmp_path / "taken"
    sentinel = create_database(admin_dsn, _prefix(taken) + "_original")
    try:
        with psycopg.connect(sentinel.dsn) as conn:
            conn.execute("CREATE TABLE sentinel (n integer)")
            conn.execute("INSERT INTO sentinel VALUES (42)")
        with pytest.raises(DatabaseExists, match="already exists"):
            runner.shrink(result, SCENARIO, taken, admin_dsn=admin_dsn)
        assert not taken.exists()
        with psycopg.connect(sentinel.dsn) as conn:
            assert conn.execute("SELECT n FROM sentinel").fetchone() == (42,)
    finally:
        drop_own_database(admin_dsn, sentinel)

    execute = InprocRun.execute

    async def fail_candidate(self: InprocRun) -> Execution:
        assert isinstance(self.spec.plan, list)
        outcome = await execute(self)
        if len(self.spec.plan) == 1:
            raise RuntimeError("candidate fixture failed")
        return outcome

    monkeypatch.setattr(InprocRun, "execute", fail_candidate)
    failed = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="stopped before scoring: candidate fixture failed"):
        runner.shrink(result, SCENARIO, failed, admin_dsn=admin_dsn)
    assert not failed.exists() and _databases(admin_dsn, failed) == []
    with pytest.raises(ViewerError, match="lists no database"):
        RunSource(admin_dsn, registry_database()).registered(_prefix(failed) + "_1")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"config": "B1"}, "B1 needs"),
        ({"config": "B2"}, "B2 needs"),
        ({"topology": "split"}, "split runs"),
        ({"model_adapter": "live"}, "live runs"),
        ({"model_adapter": "replay"}, "replayed runs"),
        ({"parent_run_id": "parent", "branch_point": 0}, "branched runs"),
        ({"scenario_digest": "wrong"}, "does not match"),
        ({"params": {}}, "materialized fault plan"),
    ],
)
def test_unsupported_input_is_refused_before_provisioning(
    original: tuple[str, RunResult],
    admin_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
    message: str,
) -> None:
    _, result = original
    bad = result.model_copy(update={"run": result.run.model_copy(update=changes)})
    monkeypatch.setattr(
        runner, "bootstrap_server", lambda *_: pytest.fail("provisioned a database")
    )
    out = tmp_path / "refused"
    with pytest.raises(ValueError, match=message):
        runner.shrink(bad, SCENARIO, out, admin_dsn=admin_dsn)
    assert not out.exists()


@pytest.mark.parametrize("field", ["code", "intent_id"])
def test_a_different_reason_or_intent_does_not_reproduce(
    original: tuple[str, RunResult], admin_dsn: str, tmp_path: Path, field: str
) -> None:
    _, result = original
    prop = next(p for p in result.properties if p.failures)
    first = prop.failures[0]
    value = next(code for code in ReasonCode if code != first.code) if field == "code" else "other"
    altered = prop.model_copy(
        update={"failures": (first.model_copy(update={field: value}), *prop.failures[1:])}
    )
    bad = result.model_copy(
        update={"properties": tuple(altered if p is prop else p for p in result.properties)}
    )
    assert Signature.of(bad).properties == Signature.of(result).properties
    out = tmp_path / "mismatch"
    with pytest.raises(ValueError, match="did not reproduce the source failure signature"):
        runner.shrink(bad, SCENARIO, out, admin_dsn=admin_dsn)
    assert not out.exists() and _databases(admin_dsn, out) == []
