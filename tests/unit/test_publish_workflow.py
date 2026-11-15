"""The PyPI publish workflow, `.github/workflows/publish.yml`.

It publishes through PyPI's Trusted Publishing, so its safety rests on a few lines: it
runs only when a release is published, only its publish job may ask GitHub for an OIDC
token, that job runs in the `pypi` environment after the build, and the build checks the
tag and runs demo 1 from the installed wheel before anything is published. These tests
read the file as YAML and pin those lines.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "publish.yml"


def workflow() -> dict[Any, Any]:
    found: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return found


def test_it_runs_only_when_a_release_is_published() -> None:
    # PyYAML reads YAML 1.1, in which the bare key `on` is the boolean true.
    assert workflow()[True] == {"release": {"types": ["published"]}}


def test_only_the_publish_job_may_ask_for_an_oidc_token() -> None:
    data = workflow()
    assert data["permissions"] == {"contents": "read"}
    assert data["jobs"]["publish"]["permissions"] == {"id-token": "write"}
    assert "permissions" not in data["jobs"]["build"]


def test_the_publish_job_runs_in_the_pypi_environment_after_the_build() -> None:
    publish = workflow()["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert publish["environment"] == {"name": "pypi", "url": "https://pypi.org/p/fivefold"}
    last = publish["steps"][-1]
    assert last["uses"] == "pypa/gh-action-pypi-publish@release/v1"
    # Trusted Publishing needs no token, and a password input would turn it off.
    assert "with" not in last


def test_the_build_checks_the_tag_and_runs_demo_1_from_the_installed_wheel() -> None:
    steps = workflow()["jobs"]["build"]["steps"]
    runs = "\n".join(step.get("run", "") for step in steps)
    assert 'tag="${GITHUB_REF#refs/tags/}"' in runs
    assert 'if [ "$tag" != "v$version" ]; then' in runs
    assert "uv build --out-dir dist" in runs
    assert 'uv pip install --python "$smoke/venv/bin/python" dist/*.whl' in runs
    assert '"$smoke/venv/bin/python" -m fivefold.harness.demo 1' in runs
    assert steps[-1]["uses"].startswith("actions/upload-artifact@")
