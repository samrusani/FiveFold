"""`python -m fivefold.viewer` (0012): what it serves, and what it refuses, with no server."""

from __future__ import annotations

from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette

from fivefold.viewer import __main__ as cli
from fivefold.viewer.source import REGISTRY_ENV


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
