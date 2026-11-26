"""An episode that exits early releases its work on a shared Temporal host."""

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio.service import RPCError, RPCStatusCode

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness import temporal_worker
from fivefold.harness.run import InprocRun, RunResult, RunSpec
from fivefold.paths import DEV_001


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


def test_a_failed_termination_keeps_tracking_until_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def result() -> None:
        await asyncio.Event().wait()

    handle = Mock(
        result=AsyncMock(side_effect=result),
        terminate=AsyncMock(
            side_effect=[RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""), None]
        ),
    )
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
        with pytest.raises(RPCError, match="unavailable"):
            asyncio.run(host.finish((execution,)))
        assert set(host.workflows) == {execution}
        asyncio.run(host.finish((execution,)))
        assert not host.workflows and not host.receivers
    assert handle.terminate.await_count == 2


def test_a_closed_poller_refuses_new_work_and_closes_coroutines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(temporal_worker, "temporal_cli_path", lambda: Path("unused"))
    host = temporal_worker.TemporalHost()
    host.loop.close()
    with pytest.raises(temporal_worker.TemporalInfrastructureError, match="poller is closed"):
        host.start("fixture", {}, Mock())
    operation = host._finish_workflows(())
    with pytest.raises(temporal_worker.TemporalInfrastructureError, match="poller is unavailable"):
        asyncio.run(host._submit(operation))
    assert inspect.iscoroutine(operation) and operation.cr_frame is None


@pytest.mark.parametrize("score_succeeded", [True, False])
def test_inproc_run_stops_temporal_workers_and_preserves_the_score_or_primary_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, score_succeeded: bool
) -> None:
    host = Mock(spec=temporal_worker.TemporalHost)
    stopped = AsyncMock(side_effect=OSError("termination unavailable"))
    scored = RunResult("fixture")
    score = AsyncMock(
        return_value=scored, side_effect=None if score_succeeded else ValueError("primary failure")
    )
    monkeypatch.setattr(temporal_worker.TemporalWorkers, "stop", stopped)
    monkeypatch.setattr(InprocRun, "_run_and_score", score)
    run = InprocRun(
        RunSpec(load_scenario_file(DEV_001), "B1", Contract.C2, 7, plan=[]),
        "postgresql://localhost",
        temporal_host=host,
    )
    if score_succeeded:
        assert asyncio.run(run.execute()) is scored
    else:
        with pytest.raises(ValueError, match="primary failure"):
            asyncio.run(run.execute())
    stopped.assert_awaited_once()
    assert "baseline cleanup failed: termination unavailable" in caplog.text
