"""The evaluator's command line: `python -m fivefold.evaluator`.

    python -m fivefold.evaluator report results/ --csv runs.csv --markdown report.md

`report` reads saved run results, each a JSON file as `result_json` writes it: the files
given, and every `.json` file directly in each directory given. It writes the CSV across
runs, the Markdown report, or both (record 0004, Metrics; `fivefold.evaluator.report`).
The same results give the same bytes whatever order they are given in.

A refused input, such as a file that is not a valid run result, ends the command with one
line on standard error and exit status 1, and nothing is written. So does an output that
names a result file the command reads, or the other output, through any path or link.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from fivefold.evaluator.report import (
    ReportError,
    load_results,
    markdown_report,
    result_files,
    runs_csv,
)


def same_file(path: Path, other: Path) -> bool:
    """Whether `path` and `other` name one file: the same path once resolved, or one file
    through a hard link when both exist."""
    if path.resolve() == other.resolve():
        return True
    return path.exists() and other.exists() and path.samefile(other)


def check_outputs(outputs: Sequence[Path], inputs: Sequence[Path]) -> None:
    """Refuse an output that names one of the result files `inputs`, or another output, so
    that writing a report never replaces a saved result or the other output."""
    for number, output in enumerate(outputs):
        for other in outputs[number + 1 :]:
            if same_file(output, other):
                raise ReportError(f"{output} and {other} name the same file")
        for source in inputs:
            if same_file(output, source):
                raise ReportError(f"{output} would replace the result file {source}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command. Returns the process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m fivefold.evaluator",
        description="Build the evaluator's outputs across runs from saved run results.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser(
        "report",
        help="write a CSV with one row per run and a Markdown report",
        description="Write a CSV with one row per run and a Markdown report from saved run"
        " results.",
    )
    report.add_argument(
        "results",
        nargs="+",
        type=Path,
        metavar="RESULTS",
        help="run result files, or directories holding them as .json files",
    )
    report.add_argument("--csv", type=Path, metavar="PATH", help="where to write the CSV")
    report.add_argument(
        "--markdown", type=Path, metavar="PATH", help="where to write the Markdown report"
    )
    args = parser.parse_args(argv)
    csv_path: Path | None = args.csv
    markdown_path: Path | None = args.markdown
    if csv_path is None and markdown_path is None:
        report.error("give --csv, --markdown or both")
    if csv_path is not None and markdown_path is not None and csv_path == markdown_path:
        report.error("--csv and --markdown name the same file")
    try:
        targets = [path for path in (csv_path, markdown_path) if path is not None]
        check_outputs(targets, result_files(args.results))
        results = load_results(args.results)
        outputs: list[tuple[Path, str]] = []
        if csv_path is not None:
            outputs.append((csv_path, runs_csv(results)))
        if markdown_path is not None:
            outputs.append((markdown_path, markdown_report(results)))
        for path, text in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
    except (OSError, ReportError) as error:
        print(f"report failed: {error}", file=sys.stderr)
        return 1
    written = " and ".join(str(path) for path, _ in outputs)
    print(f"Reported {len(results)} run{'' if len(results) == 1 else 's'} in {written}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
