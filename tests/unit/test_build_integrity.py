"""Invalid check inputs fail and revision arguments stay separate from Git options."""

import runpy
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def test_three_repeats_use_both_pause_treatments(
    baseline_repeats: Callable[[str, str, str], list[str] | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIVEFOLD_B1_REPEATS", "3")
    assert baseline_repeats("FIVEFOLD_B1_REPEATS", "short", "long") == ["normal", "short", "long"]


@pytest.mark.parametrize("value", ["0", "abc", ""])
def test_invalid_baseline_repeat_counts_are_refused(
    baseline_repeats: Callable[[str, str, str], list[str] | None],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("FIVEFOLD_B1_REPEATS", value)
    with pytest.raises(pytest.UsageError, match="FIVEFOLD_B1_REPEATS must be a positive integer"):
        baseline_repeats("FIVEFOLD_B1_REPEATS", "short", "long")


def test_identity_check_ends_git_options_before_the_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = runpy.run_path(str(SCRIPTS / "check_identities.py"))["check"]

    def git(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["git", "log"]
        assert argv[-2:] == ["--end-of-options", "HEAD~1..HEAD"]
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", git)
    assert check("HEAD~1..HEAD") == []


def test_pypi_readme_check_requires_an_artifact(capsys: pytest.CaptureFixture[str]) -> None:
    main = runpy.run_path(str(SCRIPTS / "check_pypi_readme.py"))["main"]
    assert main([]) == 1
    assert "Provide a wheel or source distribution" in capsys.readouterr().err
