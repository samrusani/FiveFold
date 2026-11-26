"""An episode that exits early releases its work on a shared Temporal host."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from fivefold.harness import temporal_worker


def test_an_early_exit_terminates_the_episode_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    async def result() -> None:
        await asyncio.Event().wait()

    handle = Mock(result=AsyncMock(side_effect=result), terminate=AsyncMock())
    client = Mock(
        start_workflow=AsyncMock(return_value=handle), get_workflow_handle=Mock(return_value=handle)
    )

    async def poll(host: temporal_worker.TemporalHost) -> None:
        monkeypatch.setattr(host, "client", client, raising=False)
        host.ready.set_result(None)
        await host.stop.wait()

    monkeypatch.setattr(temporal_worker, "temporal_cli_path", lambda: Path("unused"))
    monkeypatch.setattr(temporal_worker.TemporalHost, "_poll", poll)
    with temporal_worker.TemporalHost() as host:
        execution = host.next_episode()
        host.start(execution, {}, Mock())
        asyncio.run(host.finish((execution,)))
        assert not host.receivers and not host.workflows
    client.get_workflow_handle.assert_called_once_with(execution)
    handle.terminate.assert_awaited_once()
