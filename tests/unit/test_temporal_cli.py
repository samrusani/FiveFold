"""The setup check verifies an executable before invoking it."""

import runpy
from pathlib import Path

import pytest

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
