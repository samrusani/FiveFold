"""The CI wheel check runs the installed demo outside the checkout."""

from pathlib import Path

import yaml


def test_ci_builds_and_runs_the_wheel_on_postgres() -> None:
    path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    job = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["packaging"]
    assert job["services"]["postgres"]["image"] == "postgres:16.15"
    assert job["services"]["postgres"]["env"]["POSTGRES_HOST_AUTH_METHOD"] == "trust"
    assert job["env"]["FIVEFOLD_ADMIN_DSN"] == "postgresql://postgres@127.0.0.1:5432/postgres"
    steps = job["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "uv build --out-dir dist" in commands
    assert 'uv venv --python 3.12 "$smoke/venv"' in commands
    assert 'uv pip install --python "$smoke/venv/bin/python" dist/*.whl' in commands
    assert 'cd "$smoke"' in commands
    assert '"$smoke/venv/bin/python" -m fivefold.harness.demo 1' in commands
