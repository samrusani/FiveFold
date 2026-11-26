"""`python -m fivefold.viewer` (0012): what it serves, and what it refuses, with no server."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import psycopg
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.testclient import TestClient

from fivefold.viewer import __main__ as cli
from fivefold.viewer.app import create_app
from fivefold.viewer.source import REGISTRY_ENV, RunSource


def test_it_serves_the_api_over_the_named_server_and_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served: dict[str, object] = {}

    def run(app: Starlette, *, host: str, port: int) -> None:
        served.update(app=app, host=host, port=port)

    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.delenv(REGISTRY_ENV, raising=False)
    argv = ["--port", "8401", "--dsn", "postgresql://postgres@db:5432/postgres"]
    assert cli.main([*argv, "--static", str(tmp_path)]) == 0
    assert (served["host"], served["port"]) == ("127.0.0.1", 8401)
    assert isinstance(served["app"], Starlette)


def test_the_registry_comes_from_the_environment_unless_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(uvicorn, "run", lambda app, *, host, port: None)
    monkeypatch.setenv(REGISTRY_ENV, "fivefold_test_registry")
    cli.main([])
    assert "(registry fivefold_test_registry)" in capsys.readouterr().out
    cli.main(["--registry", "fivefold_other_registry"])
    assert "(registry fivefold_other_registry)" in capsys.readouterr().out


def test_a_registry_name_the_harness_cannot_make_is_refused() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--registry", "Not-A-Database"])


def test_the_viewer_only_serves_a_local_or_explicit_host(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("viewer")
    source = RunSource("")
    client = TestClient(create_app(source, static=tmp_path))
    assert client.get("/", headers={"Host": "testserver"}).status_code == 400
    for host in ("localhost:8300", "127.0.0.1:8300", "[::1]:8300"):
        assert client.get("/", headers={"Host": host}).text == "viewer"


def test_the_viewer_joins_its_run_list_workers_at_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"database": "fivefold_test_run"}
    threads: list[threading.Thread] = []

    def metadata(source: RunSource, listed: dict[str, str]) -> dict[str, str]:
        threads.append(threading.current_thread())
        return listed

    monkeypatch.setattr(RunSource, "_registry_rows", lambda *_: [row])
    monkeypatch.setattr(RunSource, "_listed_run", metadata)
    with TestClient(create_app(RunSource(""), static=None), base_url="http://localhost") as client:
        for _ in range(2):
            assert client.get("/api/runs").json()["runs"] == [row]
        first, second = threads
        assert first is second and first.name.startswith("fivefold-viewer")
    assert not first.is_alive()


def test_allows_each_named_host_but_rejects_wildcards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("viewer")

    def run(app: Starlette, *, host: str, port: int) -> None:
        client = TestClient(app)
        for name in ("viewer.local", "runs.local"):
            assert client.get("/", headers={"Host": name}).text == "viewer"

    monkeypatch.setattr(uvicorn, "run", run)
    assert (
        cli.main(
            [
                "--static",
                str(tmp_path),
                "--allow-host",
                "viewer.local",
                "--allow-host",
                "runs.local",
            ]
        )
        == 0
    )
    with pytest.raises(SystemExit):
        cli.main(["--allow-host", "*"])


@pytest.mark.parametrize("value", ["viewer.local:8300", "::1", "*"])
def test_the_cli_refuses_an_invalid_allow_host(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(["--allow-host", value])
    assert "without ports or wildcards" in capsys.readouterr().err


def test_a_wildcard_listener_prints_a_usable_local_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run(app: Starlette, *, host: str, port: int) -> None:
        assert (host, port) == ("0.0.0.0", 8300)

    monkeypatch.setattr(uvicorn, "run", run)
    assert cli.main(["--host", "0.0.0.0"]) == 0
    assert "http://127.0.0.1:8300/" in capsys.readouterr().out


def test_a_query_timeout_maps_to_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def read(self: RunSource, database: str) -> Iterator[Mock]:
        yield Mock(execute=Mock(side_effect=psycopg.errors.QueryCanceled("statement timeout")))

    monkeypatch.setattr(RunSource, "_read", read)
    with TestClient(create_app(RunSource(""), static=None), base_url="http://localhost") as client:
        response = client.get("/api/runs")
    assert response.status_code == 503
    assert response.json() == {
        "error": "query_timeout",
        "detail": "the registry query exceeded 5 s",
    }
