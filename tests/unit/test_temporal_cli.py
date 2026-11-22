import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "temporal_cli.py"


@pytest.mark.parametrize("version", [None, "1.9.0", "1.10.1"])
def test_temporal_check_requires_the_pinned_cli(tmp_path: Path, version: str | None) -> None:
    binary = tmp_path / "temporal"
    if version is not None:
        binary.write_text(f"#!/bin/sh\necho 'temporal version {version}'\n", encoding="utf-8")
        binary.chmod(0o755)
    result = subprocess.run(
        [sys.executable, SCRIPT, "check"],
        env={**os.environ, "FIVEFOLD_TEMPORAL_CLI": str(binary)},
        capture_output=True,
        text=True,
        check=False,
    )
    if version == "1.10.1":
        assert result.returncode == 0
    else:
        assert result.returncode == 1
        assert "Run make temporal-setup" in result.stderr
