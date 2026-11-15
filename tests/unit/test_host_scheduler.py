"""The split topology's clock and scheduler: virtual time follows the host's clock (0009).

These tests wait in wall time, tens of milliseconds at most each. They check lower bounds
and order, never an upper bound on how late the event loop runs a timer.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from fivefold.harness.clock import ClockBehind, HostClock
from fivefold.harness.scheduler import (
    HostScheduler,
    SchedulerStopped,
    SchedulerTimeout,
)


def _scheduler() -> HostScheduler:
    return HostScheduler(HostClock(HostClock.host_ms()))


def test_virtual_time_counts_the_host_clock_from_the_origin() -> None:
    origin = HostClock.host_ms() - 5_000
    clock = HostClock(origin)
    before = time.monotonic_ns() // 1_000_000 - origin
    now = clock.now
    after = time.monotonic_ns() // 1_000_000 - origin
    assert clock.origin_ms == origin
    assert before <= now <= after
    assert now >= 5_000


def test_only_the_host_moves_the_clock() -> None:
    clock = HostClock(HostClock.host_ms())
    with pytest.raises(RuntimeError, match="nothing can move it"):
        clock.advance_to(clock.now + 10)


def test_a_clock_whose_origin_is_ahead_of_this_host_refuses_to_read() -> None:
    clock = HostClock(HostClock.host_ms() + 60_000)
    with pytest.raises(ClockBehind, match="one host"):
        _ = clock.now


def test_timers_fire_in_order_once_their_time_has_come() -> None:
    async def scenario() -> list[tuple[str, int]]:
        scheduler = _scheduler()
        fired: list[tuple[str, int]] = []
        done = asyncio.Event()
        start = scheduler.now

        def late() -> None:
            fired.append(("late", scheduler.now))
            done.set()

        scheduler.call_later(30, late)
        scheduler.call_later(10, lambda: fired.append(("early", scheduler.now)))
        await asyncio.wait_for(done.wait(), timeout=5)
        return [(name, at - start) for name, at in fired]

    fired = asyncio.run(scenario())
    assert [name for name, _ in fired] == ["early", "late"]
    assert fired[0][1] >= 10
    assert fired[1][1] >= 30


def test_an_event_due_in_the_past_fires_at_once() -> None:
    async def scenario() -> bool:
        scheduler = _scheduler()
        done = asyncio.Event()
        scheduler.call_at(0, done.set)
        await asyncio.wait_for(done.wait(), timeout=5)
        return done.is_set()

    assert asyncio.run(scenario())


def test_sleep_and_wait_for_take_at_least_their_virtual_time() -> None:
    async def scenario() -> tuple[int, int, bool]:
        scheduler = _scheduler()
        start = scheduler.now
        await scheduler.sleep(20)
        slept = scheduler.now - start
        start = scheduler.now
        never: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        with pytest.raises(SchedulerTimeout):
            await scheduler.wait_for(never, 15)
        waited = scheduler.now - start
        return slept, waited, never.cancelled()

    slept, waited, cancelled = asyncio.run(scenario())
    assert slept >= 20
    assert waited >= 15
    assert cancelled


def test_a_cancelled_timer_never_fires() -> None:
    async def scenario() -> tuple[list[str], int]:
        scheduler = _scheduler()
        fired: list[str] = []
        timer = scheduler.call_later(5, lambda: fired.append("cancelled"))
        timer.cancel()
        await scheduler.sleep(20)
        return fired, len(scheduler.pending())

    fired, pending = asyncio.run(scenario())
    assert fired == []
    assert pending == 0


def test_stop_cancels_timers_and_tasks_and_refuses_new_timers() -> None:
    async def scenario() -> tuple[list[str], bool, bool]:
        scheduler = _scheduler()
        fired: list[str] = []
        scheduler.call_later(10, lambda: fired.append("timer"))
        task = scheduler.spawn("simulator", asyncio.Event().wait(), name="waits forever")
        scheduler.stop()
        await asyncio.sleep(0.05)
        refused = False
        try:
            scheduler.call_later(1, lambda: None)
        except SchedulerStopped:
            refused = True
        return fired, task.cancelled(), refused and not scheduler.is_up("simulator")

    fired, cancelled, refused = asyncio.run(scenario())
    assert fired == []
    assert cancelled
    assert refused


def test_a_task_or_event_that_raises_is_kept_as_a_failure() -> None:
    async def fails() -> None:
        raise ValueError("a component bug")

    def raises() -> None:
        raise KeyError("an event bug")

    async def scenario() -> list[str]:
        scheduler = _scheduler()
        scheduler.spawn("gateway", fails(), name="fails")
        scheduler.call_later(0, raises, label="raises")
        await scheduler.sleep(10)
        return [type(error).__name__ for _, error in scheduler.failures]

    assert sorted(asyncio.run(scenario())) == ["KeyError", "ValueError"]


def test_the_split_scheduler_has_no_run_loop() -> None:
    with pytest.raises(TypeError, match="no run loop"):
        asyncio.run(_scheduler().run())
