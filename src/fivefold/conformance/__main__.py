"""Run the MCP lost-reply cases and print a report."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError

from fivefold.conformance.fixture import Fixture
from fivefold.conformance.runner import Report, run


def display(report: Report) -> str:
    lines = [
        f"FiveFold {report.fivefold_version}, MCP SDK {report.sdk_version}",
        f"Fixture {report.fixture_version}, {report.transport}, tool {report.tool}",
        f"Seed {report.seed}, retry identity {report.retry_identity}",
    ]
    for row in report.cases:
        evidence = row.evidence
        lines.append(
            f"{row.case} [{row.protocol_version or 'no protocol'}] {row.verdict}. {row.reason}"
        )
        lines.append(
            f"  effects initial={evidence.initial_count} before_retry={evidence.count_before_retry}"
            f" settled={evidence.settled_count}, writes={evidence.writes_sent}"
            f", dropped={evidence.replies_dropped}, wait_ms={evidence.caller_wait_ms}"
        )
        if evidence.events:
            lines.append(f"  {' > '.join(evidence.events)}")
    lines.append(f"Exit code {report.exit_code}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m fivefold.conformance", description=__doc__)
    parser.add_argument(
        "--fixture", type=Path, required=True, help="local fixture declaration JSON"
    )
    parser.add_argument("--seed", type=int, required=True, help="seed for inputs and case order")
    parser.add_argument("--json", action="store_true", help="print the machine-readable report")
    args = parser.parse_args()
    try:
        fixture = Fixture.model_validate_json(args.fixture.read_bytes())
    except (OSError, ValidationError):
        if args.json:
            print(json.dumps({"error": "invalid fixture declaration", "exit_code": 2}))
        else:
            print("Invalid fixture declaration.", file=sys.stderr)
        return 2
    report = asyncio.run(run(fixture, args.seed))
    if args.json:
        print(json.dumps({**asdict(report), "exit_code": report.exit_code}, indent=2))
    else:
        print(display(report))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
