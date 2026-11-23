"""Shrink a scored, in-process scripted failure (record 0022)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import psycopg

from fivefold.domain.scenario import load_scenario_file
from fivefold.evaluator.result import RunResult
from fivefold.harness.migrations import DEFAULT_ADMIN_DSN, MigrationError
from fivefold.harness.registry import RegistryError
from fivefold.shrink.runner import DEFAULT_MAX_CANDIDATES, registered_result, shrink
from fivefold.viewer.source import ViewerError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fivefold.shrink", description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--result", type=Path, help="a saved evaluator result JSON file")
    inputs.add_argument("--database", help="the source run's registered database name")
    parser.add_argument(
        "--scenario", required=True, type=Path, help="the original scenario YAML file"
    )
    parser.add_argument("--out", required=True, type=Path, help="a new output directory")
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_MAX_CANDIDATES,
        help="distinct search candidates (default: %(default)s)",
    )
    parser.add_argument("--wall-time-s", type=float, help="optional wall-time budget in seconds")
    parser.add_argument(
        "--keep-database", action="store_true", help="keep the final run for the viewer"
    )
    args = parser.parse_args(argv)
    admin_dsn = os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_ADMIN_DSN)
    try:
        scenario = load_scenario_file(args.scenario)
        source = (
            RunResult.model_validate_json(args.result.read_bytes())
            if args.result is not None
            else registered_result(admin_dsn, args.database)
        )
        report = shrink(
            source,
            scenario,
            args.out,
            admin_dsn=admin_dsn,
            max_candidates=args.max_candidates,
            wall_time_s=args.wall_time_s,
            keep_database=args.keep_database,
            source_database=args.database,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        MigrationError,
        RegistryError,
        ViewerError,
        psycopg.Error,
    ) as error:
        print(f"shrink failed: {error}", file=sys.stderr)
        return 1
    print(f"Verified the shrunk plan after {report['candidate_count']} candidates.")
    if report["budget_exhausted"]:
        print("Search budget exhausted. The output contains the best verified plan.")
    if report["final_database"]:
        print(f"Kept {report['final_database']} for the viewer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
