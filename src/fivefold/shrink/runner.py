"""Load, rerun and shrink a scored run with fresh, owned databases (0022)."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import yaml

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, scenario_digest
from fivefold.evaluator.result import RunResult, Verdict, comparable_json, result_json
from fivefold.faults.plan import parse_plan
from fivefold.harness.clock import wall_ms
from fivefold.harness.migrations import bootstrap_server, create_run_database, registry_database
from fivefold.harness.run import InprocRun, RunSpec, run_plan
from fivefold.harness.study import as_config, drop_own_database
from fivefold.paths import DB_DIR
from fivefold.recorder.cassette import canonical_json, sha256
from fivefold.recorder.playback import records
from fivefold.shrink.search import Observation, Plan, Signature, minimize, plan_data
from fivefold.viewer.source import RunSource

DEFAULT_MAX_CANDIDATES = 100


def registered_result(admin_dsn: str, database: str) -> RunResult:
    """Use the viewer's registry check and read-only snapshot as ff_viewer."""
    source = RunSource(admin_dsn, registry_database())
    with source.run(database) as conn:
        row = conn.execute(
            "SELECT r.result FROM eval.run_result r JOIN harness.run h USING (run_id)"
        ).fetchone()
    if row is None:
        raise ValueError("the registered run has no scored result")
    return RunResult.model_validate(row["result"])


def source_spec(source: RunResult, scenario: Scenario) -> tuple[RunSpec, Plan]:
    """Require the scored run's original input and a fully materialized fault plan."""
    run = source.run
    if run.topology != "inproc":
        raise ValueError("split runs need separate candidate determinism and cleanup verification")
    cassette_hint = (
        "a changed fault plan can change requests. Use ordinary playback for the cassette"
    )
    if run.parent_run_id is not None or run.branch_point is not None:
        raise ValueError(f"cannot shrink branched runs because {cassette_hint}")
    if run.model_adapter == "replay":
        raise ValueError(f"cannot shrink replayed runs because {cassette_hint}")
    if run.model_adapter != "scripted" or run.model != "scripted":
        raise ValueError(f"cannot shrink live runs because {cassette_hint}")
    if run.config not in ("B0", "G1", "G2"):
        raise ValueError(
            f"{run.config} needs separate candidate determinism and cleanup verification"
        )
    if run.scenario_id != scenario.id or run.scenario_digest != scenario_digest(scenario):
        raise ValueError("the supplied scenario does not match the source run's digest")
    properties = (1, 2, 3, 4, 5)
    if (
        tuple(sorted(source.evaluated)) != properties
        or tuple(sorted(prop.property for prop in source.properties)) != properties
    ):
        raise ValueError("the source must include all five evaluated properties")
    if source.verdict is not Verdict.FAIL or not Signature.of(source).properties:
        raise ValueError("the source must be a scored run with verdict fail")
    if not isinstance(run.params, dict):
        raise ValueError("the source lacks its materialized fault plan")
    entries, workers = run.params.get("plan"), run.params.get("workers")
    if not isinstance(entries, list):
        raise ValueError("the source lacks its materialized fault plan")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("the source lacks its initial worker count")
    if not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("the source fault plan contains an entry that is not an object")
    plan = parse_plan([entry for entry in entries if isinstance(entry, dict)])
    if any(entry.kind.seeded.keys() - entry.params.keys() for entry in plan):
        raise ValueError("the source plan lacks materialized parameters drawn from the seed")
    if run.params.get("params") != scenario.params.model_dump(mode="json"):
        raise ValueError("the source run's recorded timing parameters do not match the scenario")
    spec = RunSpec(
        scenario,
        as_config(run.config),
        Contract(run.contract),
        run.seed,
        plan=plan_data(plan),
        workers=workers,
    )
    if spec.run_id != run.run_id:
        raise ValueError("the source run ID does not match its recorded inputs")
    return spec, run_plan(spec)


@dataclass(frozen=True)
class ScoredTrial:
    result: RunResult
    evidence: dict[str, list[str]]

    def observation(self) -> Observation:
        return Observation(
            Signature.of(self.result),
            {name: sha256("\n".join(rows)) for name, rows in self.evidence.items()},
        )


@contextmanager
def _trial(
    admin_dsn: str, name: str, spec: RunSpec, plan: Plan, *, keep: bool = False
) -> Iterator[ScoredTrial]:
    """Keep a final database only if executing, verifying and writing its output succeeded."""
    created = create_run_database(admin_dsn, name, DB_DIR / "migrations")
    retained = False
    try:
        try:
            episode = InprocRun(replace(spec, plan=plan_data(plan)), created.dsn)
            result = asyncio.run(episode.execute()).evaluation
        except Exception as error:
            raise RuntimeError(f"trial {name} stopped before scoring: {error}") from error
        if result is None:
            raise RuntimeError(f"trial {name} stopped before scoring")
        yield ScoredTrial(result, records(created.dsn))
        retained = keep
    finally:
        if not retained:
            drop_own_database(admin_dsn, created)


def _write_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def shrink(
    source: RunResult,
    scenario: Scenario,
    out: Path,
    *,
    admin_dsn: str,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    wall_time_s: float | None = None,
    keep_database: bool = False,
    source_database: str | None = None,
) -> dict[str, object]:
    """Run record 0022's search and write the verified plan, score and evidence."""
    if max_candidates < 0:
        raise ValueError("the candidate budget must be at least zero")
    if wall_time_s is not None and (not math.isfinite(wall_time_s) or wall_time_s < 0):
        raise ValueError("the wall-time limit must be a finite, nonnegative number of seconds")
    started = wall_ms()
    spec, original = source_spec(source, scenario)
    prefix = "fivefold_shrink_" + sha256(str(out.resolve()))[:16]
    written: list[Path] = []

    def write(name: str, content: str) -> None:
        path = out / name
        with path.open("x", encoding="utf-8") as stream:
            written.append(path)
            stream.write(content)

    out.mkdir()
    try:
        bootstrap_server(admin_dsn, DB_DIR)
        with _trial(admin_dsn, f"{prefix}_original", spec, original) as baseline:
            observed = baseline.observation()
        if observed.signature != Signature.of(source):
            raise ValueError(
                "rerunning the original plan did not reproduce the source failure signature"
            )

        candidate_number = 0

        def trial(plan: Plan) -> Observation:
            nonlocal candidate_number
            candidate_number += 1
            with _trial(admin_dsn, f"{prefix}_{candidate_number}", spec, plan) as scored:
                return scored.observation()

        def expired() -> bool:
            return wall_time_s is not None and (wall_ms() - started) / 1000 >= wall_time_s

        found = minimize(
            spec, original, observed, trial, max_candidates=max_candidates, expired=expired
        )
        final_database = f"{prefix}_final"
        with _trial(admin_dsn, final_database, spec, found.plan, keep=keep_database) as final:
            observation = final.observation()
            if observation != found.observation:
                raise RuntimeError(
                    "the selected plan reran with a different signature or record digest"
                )
            original_data, final_data = plan_data(original), plan_data(found.plan)
            report: dict[str, object] = {
                "source_run_id": source.run.run_id,
                "source_database": source_database,
                "source_result_sha256": sha256(comparable_json(source)),
                "source_plan_sha256": sha256(canonical_json(original_data)),
                "source_signature": asdict(observed.signature),
                "final_plan_sha256": sha256(canonical_json(final_data)),
                "final_signature": asdict(observation.signature),
                "final_record_digests": observation.digests,
                "final_database": final_database if keep_database else None,
                "candidate_count": len(found.candidates),
                "max_candidates": max_candidates,
                "wall_time_limit_s": wall_time_s,
                "budget_exhausted": found.budget_exhausted,
                "next_candidate": (
                    plan_data(found.next_candidate) if found.next_candidate is not None else None
                ),
                "candidates": [
                    {
                        "plan": plan_data(candidate.plan),
                        "signature": asdict(candidate.observation.signature),
                        "reproduces": candidate.reproduces,
                    }
                    for candidate in found.candidates
                ],
            }
            write("faults.yaml", yaml.safe_dump(final_data, sort_keys=False))
            write("result.json", result_json(final.result))
            write(
                "records.json",
                _write_json(
                    {
                        name: [json.loads(row) for row in rows]
                        for name, rows in final.evidence.items()
                    }
                ),
            )
            report["elapsed_wall_ms"] = round(wall_ms() - started)
            write("shrink.json", _write_json(report))
        return report
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        with suppress(OSError):
            out.rmdir()
        raise
