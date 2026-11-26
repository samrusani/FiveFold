"""The wheel smoke check and image build use the expected inputs."""

import runpy
import ssl
import sys
from http.client import HTTPException, HTTPMessage
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

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


@pytest.mark.parametrize(
    ("read_error", "status", "recovers", "expected_attempts"),
    [
        (TimeoutError, None, True, 2),
        (TimeoutError, None, False, 3),
        (ssl.SSLError, None, True, 2),
        (HTTPException, None, True, 2),
        (None, 429, True, 2),
        (None, 404, False, 1),
    ],
)
def test_temurin_download_retries_transient_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_error: type[Exception] | None,
    status: int | None,
    recovers: bool,
    expected_attempts: int,
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
                assert read_error is not None
                raise read_error("the test interrupted the read")
            return super().read(size)

    def download(requested: str, *, timeout: int) -> BytesIO:
        assert (requested, timeout) == (url, 60)
        attempts.append(requested)
        if recovers and len(attempts) == 2:
            return BytesIO(b"complete")
        if status is not None:
            raise HTTPError(url, status, "fixture response", HTTPMessage(), None)
        return InterruptedRead(b"partial download with trailing data")

    monkeypatch.setattr(sys, "argv", [str(script), url, str(archive)])
    monkeypatch.setattr("urllib.request.urlopen", download)
    monkeypatch.setattr("time.sleep", delays.append)
    if recovers:
        runpy.run_path(str(script))
        assert archive.read_bytes() == b"complete"
    else:
        with pytest.raises(read_error or HTTPError):
            runpy.run_path(str(script))
    assert len(attempts) == expected_attempts
    assert delays == list(range(1, expected_attempts))
