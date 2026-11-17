"""The commit identity check, `scripts/check_identities.py`, on a scratch repository.

The names and addresses below are made up. The check must refuse a personal address and a
wrong name on the maintainer's address, accept any GitHub noreply address, and never print
what it refused, since CI logs are public.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_identities.py"
CI = ROOT / ".github" / "workflows" / "ci.yml"

MAINTAINER = ("samrusani", "samrusani@users.noreply.github.com")
PERSONAL = ("Example Person", "person@example-laptop")


def commit(repo: Path, author: tuple[str, str], committer: tuple[str, str]) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": committer[0],
        "GIT_COMMITTER_EMAIL": committer[1],
    }
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-m", "change"],
        cwd=repo,
        env=env,
        check=True,
    )


def check(repo: Path, revisions: str = "HEAD~1..HEAD") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), revisions], cwd=repo, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    commit(tmp_path, MAINTAINER, MAINTAINER)
    return tmp_path


def test_the_maintainers_noreply_identity_passes(repo: Path) -> None:
    commit(repo, MAINTAINER, MAINTAINER)
    assert check(repo).returncode == 0


def test_other_contributors_and_githubs_own_commits_pass(repo: Path) -> None:
    commit(
        repo,
        ("someone", "12345+someone@users.noreply.github.com"),
        ("GitHub", "noreply@github.com"),
    )
    assert check(repo).returncode == 0


def test_a_personal_committer_fails_and_is_never_printed(repo: Path) -> None:
    commit(repo, MAINTAINER, PERSONAL)
    result = check(repo)
    assert result.returncode == 1
    assert "committer email is not a GitHub noreply address" in result.stdout
    assert PERSONAL[0] not in result.stdout + result.stderr
    assert PERSONAL[1] not in result.stdout + result.stderr


def test_a_personal_author_fails(repo: Path) -> None:
    commit(repo, PERSONAL, MAINTAINER)
    result = check(repo)
    assert result.returncode == 1
    assert "author email is not a GitHub noreply address" in result.stdout


def test_the_maintainers_address_needs_the_maintainers_name(repo: Path) -> None:
    commit(repo, (PERSONAL[0], MAINTAINER[1]), MAINTAINER)
    result = check(repo)
    assert result.returncode == 1
    assert "author name is not samrusani" in result.stdout
    assert PERSONAL[0] not in result.stdout


def test_every_commit_in_the_range_is_checked(repo: Path) -> None:
    commit(repo, MAINTAINER, PERSONAL)
    commit(repo, MAINTAINER, MAINTAINER)
    assert check(repo, "HEAD~1..HEAD").returncode == 0
    assert check(repo, "HEAD~2..HEAD").returncode == 1


def test_ci_checks_every_pull_requests_commits() -> None:
    jobs: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    job = jobs["identities"]
    assert job["if"] == "github.event_name == 'pull_request'"
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0
    run = job["steps"][-1]["run"]
    assert "scripts/check_identities.py" in run
    assert "github.event.pull_request.base.sha" in run
    assert "github.event.pull_request.head.sha" in run
