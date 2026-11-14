"""The deterministic scheduler, the virtual clock and the seeded generators (0009).

The plan asks for tests of ordering, timers, crash and restart before anything is built
on the scheduler (plan.md, Risks).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from fivefold.harness import clock
from fivefold.harness.clock import VirtualClock, settle
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import ComponentFailed, RunEnd, Scheduler, SchedulerTimeout


def run[T](coro_fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(coro_fn())


def recorder(fired: list[tuple[int, str]], scheduler: Scheduler, name: str) -> Callable[[], None]:
    return lambda: fired.append((scheduler.now, name))


# The clock ---------------------------------------------------------------------------------


def test_the_virtual_clock_only_moves_forward() -> None:
    c = VirtualClock()
    c.advance_to(10)
    c.advance_to(10)
    assert c.now == 10
    with pytest.raises(ValueError, match="cannot go back"):
        c.advance_to(9)
    with pytest.raises(ValueError):
        VirtualClock(-1)


def test_settle_reads_the_cpython_loop_internals_it_relies_on() -> None:
    async def check() -> None:
        loop = asyncio.get_running_loop()
        assert isinstance(loop._ready, deque)  # type: ignore[attr-defined]
        assert isinstance(loop._scheduled, list)  # type: ignore[attr-defined]
        await settle()

    run(check)


def test_settle_lets_every_ready_task_run_until_it_blocks() -> None:
    steps: list[str] = []

    async def chain(name: str, hops: int, gate: asyncio.Event) -> None:
        for hop in range(hops):
            steps.append(f"{name}{hop}")
            await asyncio.sleep(0)
        await gate.wait()
        steps.append(f"{name} released")

    async def main() -> None:
        gate = asyncio.Event()
        tasks = [asyncio.create_task(chain(n, 3, gate)) for n in "ab"]
        await settle()
        assert sorted(steps) == ["a0", "a1", "a2", "b0", "b1", "b2"]
        gate.set()
        await settle()
        assert steps[-2:] == ["a released", "b released"]
        await asyncio.gather(*tasks)

    run(main)


def test_settle_waits_for_a_wall_clock_sleep_without_moving_virtual_time() -> None:
    """LangGraph's retry backoff sleeps in wall time; the run must wait it out (0009)."""
    done: list[int] = []
    scheduler = Scheduler()

    async def backoff() -> None:
        await asyncio.sleep(0.02)
        done.append(scheduler.now)

    async def main() -> None:
        sleeper = asyncio.create_task(backoff())
        scheduler.call_at(5_000, lambda: done.append(-1))
        await scheduler.run(until=lambda: bool(done))
        await sleeper

    run(main)
    assert done == [0], "virtual time moved while a task slept in wall time"


def test_wall_ms_is_monotonic() -> None:
    first = clock.wall_ms()
    assert clock.wall_ms() >= first


# Seeded generators ---------------------------------------------------------------------------


def test_a_named_generator_repeats_with_the_seed_and_ignores_other_names() -> None:
    one, two = Generators(7), Generators(7)
    first = [one.get("latency").random() for _ in range(5)]
    two.get("faults").random()  # a new consumer must not shift another's numbers
    assert [two.get("latency").random() for _ in range(5)] == first
    assert one.get("latency") is one.get("latency")
    assert [Generators(8).get("latency").random() for _ in range(5)] != first
    with pytest.raises(ValueError):
        one.get("")


# Ordering and timers -----------------------------------------------------------------------


def test_events_fire_in_time_order_then_in_the_order_they_were_queued() -> None:
    scheduler = Scheduler()
    fired: list[tuple[int, str]] = []
    for at, name in [(20, "c"), (10, "a"), (20, "d"), (10, "b"), (0, "start")]:
        scheduler.call_at(at, recorder(fired, scheduler, name))
    assert run(scheduler.run) is RunEnd.IDLE
    assert fired == [(0, "start"), (10, "a"), (10, "b"), (20, "c"), (20, "d")]


def test_a_cancelled_timer_never_fires_and_the_clock_jumps_to_the_next_event() -> None:
    scheduler = Scheduler()
    fired: list[int] = []
    scheduler.call_at(10_000, lambda: fired.append(scheduler.now)).cancel()
    scheduler.call_at(60_000, lambda: fired.append(scheduler.now))
    run(scheduler.run)
    assert fired == [60_000]
    assert scheduler.now == 60_000


def test_timers_cannot_be_set_in_the_past_or_with_negative_delays() -> None:
    scheduler = Scheduler(VirtualClock(100))
    with pytest.raises(ValueError):
        scheduler.call_at(99, lambda: None)
    with pytest.raises(ValueError):
        scheduler.call_later(-1, lambda: None)


def test_sleep_suspends_a_task_in_virtual_time_and_other_events_run_meanwhile() -> None:
    scheduler = Scheduler()
    log: list[str] = []

    async def thinker() -> None:
        log.append(f"think {scheduler.now}")
        await scheduler.sleep(1_000)
        log.append(f"answer {scheduler.now}")

    async def main() -> None:
        scheduler.spawn("agent-1", thinker(), name="think")
        scheduler.call_at(500, lambda: log.append(f"message {scheduler.now}"))
        await scheduler.run()

    run(main)
    assert log == ["think 0", "message 500", "answer 1000"]


def test_wait_for_raises_after_its_virtual_timeout_and_cancels_the_wait() -> None:
    scheduler = Scheduler()
    outcome: list[str] = []

    async def caller() -> None:
        never: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        try:
            await scheduler.wait_for(never, 2_000)
        except SchedulerTimeout:
            outcome.append(f"timeout at {scheduler.now}, cancelled={never.cancelled()}")

    async def main() -> None:
        scheduler.spawn("agent-1", caller(), name="caller")
        await scheduler.run()

    run(main)
    assert outcome == ["timeout at 2000, cancelled=True"]


def test_wait_for_returns_the_result_and_cancels_its_timer() -> None:
    scheduler = Scheduler()
    results: list[int] = []

    async def caller() -> None:
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        scheduler.call_at(300, lambda: future.set_result(42))
        results.append(await scheduler.wait_for(future, 2_000))

    async def main() -> None:
        scheduler.spawn("agent-1", caller(), name="caller")
        await scheduler.run()

    run(main)
    assert results == [42]
    assert scheduler.now == 300, "the cancelled timeout must not move the clock"


def test_a_deadline_stops_the_run_and_keeps_later_events() -> None:
    scheduler = Scheduler()
    fired: list[int] = []
    scheduler.call_at(100, lambda: fired.append(100))
    scheduler.call_at(5_000, lambda: fired.append(5_000))
    assert asyncio.run(scheduler.run(deadline_ms=1_000)) is RunEnd.DEADLINE
    assert fired == [100] and scheduler.now == 1_000
    assert [timer.at for timer in scheduler.pending()] == [5_000]


def test_a_failing_component_task_stops_the_run() -> None:
    scheduler = Scheduler()

    async def broken() -> None:
        raise RuntimeError("boom")

    async def main() -> None:
        scheduler.spawn("gateway", broken(), name="broken")
        await scheduler.run()

    with pytest.raises(ComponentFailed, match="boom"):
        run(main)


# Crash and restart ---------------------------------------------------------------------------


def test_a_crash_cancels_the_components_tasks_and_a_restart_brings_it_back() -> None:
    scheduler = Scheduler()
    log: list[str] = []

    async def worker(name: str) -> None:
        try:
            await scheduler.sleep(10_000)
            log.append(f"{name} finished")
        except asyncio.CancelledError:
            log.append(f"{name} cancelled at {scheduler.now}")
            raise

    def restart() -> None:
        log.append(f"restarted at {scheduler.now}")
        scheduler.spawn("agent-1", worker("second"), name="second")

    async def main() -> None:
        scheduler.spawn("agent-1", worker("first"), name="first")
        scheduler.spawn("gateway", worker("other"), name="other")
        scheduler.call_at(
            1_000, lambda: scheduler.crash("agent-1", restart_after_ms=500, restart=restart)
        )
        scheduler.call_at(1_200, lambda: log.append(f"up={scheduler.is_up('agent-1')}"))
        await scheduler.run()

    run(main)
    assert log == [
        "first cancelled at 1000",
        "up=False",
        "restarted at 1500",
        "other finished",
        "second finished",
    ]
    assert scheduler.incarnation("agent-1") == 1
    assert scheduler.incarnation("gateway") == 0


def test_a_down_component_cannot_start_work_and_listeners_hear_of_crashes() -> None:
    scheduler = Scheduler()
    heard: list[str] = []
    scheduler.on_crash(heard.append)

    async def main() -> None:
        scheduler.crash("gateway")
        idle = asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="down"):
            scheduler.spawn("gateway", idle, name="late")

    run(main)
    assert heard == ["gateway"]
    with pytest.raises(ValueError):
        scheduler.crash("gateway", restart_after_ms=10)


def test_a_crash_cancels_tasks_in_the_order_they_were_spawned() -> None:
    scheduler = Scheduler()
    order: list[int] = []

    async def worker(n: int) -> None:
        try:
            await scheduler.sleep(1_000)
        except asyncio.CancelledError:
            order.append(n)
            raise

    async def main() -> None:
        for n in range(6):
            scheduler.spawn("agent-1", worker(n), name=f"w{n}")
        scheduler.call_at(10, lambda: scheduler.crash("agent-1"))
        await scheduler.run()

    run(main)
    assert order == list(range(6))
