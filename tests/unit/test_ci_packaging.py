"""The wheel smoke check and image build use the expected inputs."""

import runpy
import sys
from io import BytesIO
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_ci_builds_and_runs_the_wheel_on_postgres() -> None:
    path = ROOT / ".github" / "workflows" / "ci.yml"
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


@pytest.mark.parametrize("recovers", [True, False])
def test_temurin_download_retries_a_read_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovers: bool
) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    script = tmp_path / "download.py"
    script.write_text(dockerfile.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0], encoding="utf-8")
    archive = tmp_path / "java.tar.gz"
    url = "https://download.example/temurin.tar.gz"
    attempts: list[str] = []
    delays: list[int] = []

    class InterruptedRead(BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            if self.tell():
                raise TimeoutError("the read operation timed out")
            return super().read(size)

    def download(requested: str, *, timeout: int) -> BytesIO:
        assert (requested, timeout) == (url, 60)
        attempts.append(requested)
        if recovers and len(attempts) == 2:
            return BytesIO(b"complete")
        return InterruptedRead(b"partial download with trailing data")

    monkeypatch.setattr(sys, "argv", [str(script), url, str(archive)])
    monkeypatch.setattr("urllib.request.urlopen", download)
    monkeypatch.setattr("time.sleep", delays.append)
    if recovers:
        runpy.run_path(str(script))
        assert archive.read_bytes() == b"complete"
        assert len(attempts) == 2
        assert delays == [1]
    else:
        with pytest.raises(TimeoutError, match="the read operation timed out"):
            runpy.run_path(str(script))
        assert len(attempts) == 3
        assert delays == [1, 2]
