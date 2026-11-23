"""Tests for the run-shrinking command line."""

import subprocess
import sys


def test_help_names_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "fivefold.shrink", "--help"],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert result.stdout.splitlines()[0].startswith("usage: python -m fivefold.shrink ")
