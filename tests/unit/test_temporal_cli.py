"""The setup check verifies an executable before invoking it."""

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from fivefold.harness import temporal_worker

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/temporal_cli.py"


def test_the_cli_digest_is_checked_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "temporal"
    binary.write_bytes(b"fixture executable bytes")

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("the unverified file must not run")

    check = runpy.run_path(str(SCRIPT))["check"]
    monkeypatch.setattr("subprocess.run", unexpected)
    with pytest.raises(ValueError, match="executable SHA-256 mismatch"):
        check(binary)


def test_a_missing_cli_reports_the_setup_command(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, SCRIPT, "check"],
        env={**os.environ, "FIVEFOLD_TEMPORAL_CLI": str(tmp_path / "temporal")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Run make temporal-setup" in result.stderr


@pytest.mark.parametrize("entry", ["preflight", "host"])
def test_runtime_refuses_a_cli_with_the_right_version_but_wrong_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    binary = tmp_path / "temporal"
    binary.write_bytes(b"fixture executable bytes")
    monkeypatch.setenv("FIVEFOLD_TEMPORAL_CLI", str(binary))
    invocations: list[object] = []

    def correct_version(
        args: list[Path | str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        invocations.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="temporal version 1.10.1")

    monkeypatch.setattr("subprocess.run", correct_version)
    with pytest.raises(temporal_worker.TemporalInfrastructureError, match="SHA-256 mismatch"):
        if entry == "preflight":
            temporal_worker.check_temporal_cli()
        else:
            temporal_worker.TemporalHost().loop.close()
    assert invocations == []
