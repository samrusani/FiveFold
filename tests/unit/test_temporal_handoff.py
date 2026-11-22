"""Handoff deadlines allow Temporal's retry interval without exposing Activity payloads."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness import temporal_worker
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import InprocRun, RunSpec


@pytest.mark.parametrize("arrives", [True, False], ids=["after-old-bound", "no-handoff"])
def test_retry_can_wait_past_the_ordinary_bound(
    monkeypatch: pytest.MonkeyPatch, arrives: bool
) -> None:
    monkeypatch.setattr(temporal_worker, "HOST_WAIT_S", 0.03)
    monkeypatch.setattr(temporal_worker, "ACTIVITY_RETRY_BACKOFF_CAP_S", 0.18)
    host = Mock(spec=temporal_worker.TemporalHost)
    host.next_episode.return_value = "episode"
    previous = temporal_worker.Request("tool", "tool-8", 6, b"token", {"private": "payload"})
    following = temporal_worker.Request("tool", "tool-8", 7, b"new-token", {})

    async def run() -> None:
        workers = temporal_worker.TemporalWorkers(Mock(spec=InprocRun), host)
        worker = "agent-1"
        queue: asyncio.Queue[temporal_worker.Delivery] = asyncio.Queue()
        workers.queues[worker] = queue
        workers.active[worker] = previous
        reply = (previous, None, "private error")
        # Simulate delivery after 80 ms, past the old scaled 30 ms deadline.
        if arrives:
            entered = asyncio.Event()

            async def acknowledge(*args: object) -> None:
                entered.set()

            host.answer.side_effect = acknowledge
            retry = asyncio.create_task(workers._wait_for_handoff(worker, reply, start=False))
            await entered.wait()
            other_queue: asyncio.Queue[temporal_worker.Delivery] = asyncio.Queue()
            workers.queues["agent-2"] = other_queue
            done = temporal_worker.Done(["finished", ""])
            other_queue.put_nowait(done)
            asyncio.get_running_loop().call_later(0.08, queue.put_nowait, following)
            # This worker can only start its ordinary deadline after the retry hands off.
            assert await workers._wait_for_handoff("agent-2", None, start=False) is done
            assert await retry is following
        else:
            with pytest.raises(
                temporal_worker.TemporalInfrastructureError,
                match=r"^Temporal handoff timed out waiting for Activity tool-8 attempt 7$",
            ):
                await workers._wait_for_handoff(worker, reply, start=False)

        assert isinstance(host.answer, AsyncMock)
        host.answer.assert_awaited_once_with(previous, None, "private error")

    asyncio.run(run())


def test_run_surfaces_the_handoff_failure_before_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(temporal_worker, "HOST_WAIT_S", 0.03)
    host = Mock(spec=temporal_worker.TemporalHost)
    host.next_episode.return_value = "episode"
    spec = RunSpec(load_scenario_file(DEV_001), "B1", Contract.C2, 7, plan=[])
    run = InprocRun(spec, "", temporal_host=host)

    async def stall_first_handoff() -> None:
        workers = run._temporal_workers
        assert workers is not None
        workers.queues["agent-1"] = asyncio.Queue()
        run.scheduler.spawn(
            "agent-1", workers._wait_for_handoff("agent-1", None, start=True), name="worker"
        )
        await run.scheduler.run()

    monkeypatch.setattr(run, "_run", stall_first_handoff)
    with pytest.raises(
        temporal_worker.TemporalInfrastructureError,
        match=r"^Temporal handoff timed out waiting for Activity model-1 attempt 1$",
    ):
        asyncio.run(run.execute())
    assert run.result.evaluation is None
