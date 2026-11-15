"""The deterministic scheduler for the inproc topology (0009).

One process, one thread, one asyncio event loop. The scheduler owns the run's virtual
clock and a queue of timed events, ordered by (time, enqueue sequence). It repeats one
step: let every task run until each waits on the scheduler (`settle`), then take the
next event, move the clock to it and fire it. Message deliveries, timeouts, probes,
polls, approver delays and restart delays are all such events, so nothing depends on
wall-clock time, and a run full of long timeouts takes milliseconds.

Components (the agent workers, the gateway, the simulator and the harness's scripted
humans) run their asynchronous work as tasks spawned through the scheduler, under the
component's name. Libraries that hand synchronous work to the event loop's default
executor, as LangChain and LangGraph do for synchronous callables, run it at once in the
loop's thread instead (`InlineExecutor`), so a run never has a second thread.

Crashing a component cancels its tasks and discards their memory; it stays down until
its restart event fires (0006). Each crash starts a new incarnation, so the transport can
drop answers to requests sent before the crash (0010, change 3). A restart belongs to the
incarnation its crash started. When the component crashes again before that restart
fires, the earlier restart does nothing, so a component never runs as two copies at
once.

A component that waits on the real world, as the live model adapter waits on its
provider, awaits it through `hold`. Until the wait ends, the scheduler fires no event: it
waits in wall time, with virtual time held still. So every timer fires at the virtual time
it would have without the wait, and a model call takes exactly its `think_ms` (0009).
"""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

from fivefold.harness.clock import RealWaits, VirtualClock, settle

T = TypeVar("T")


class SchedulerTimeout(Exception):
    """A wait ended because its virtual-time limit passed."""


class ComponentFailed(RuntimeError):
    """A component task raised an exception that nothing handled."""


class RunEnd(StrEnum):
    """Why `Scheduler.run` returned."""

    UNTIL = "until"
    IDLE = "idle"
    DEADLINE = "deadline"


@dataclass(order=True)
class _Event:
    at: int
    seq: int
    callback: Callable[[], None] = field(compare=False)
    label: str = field(compare=False, default="")
    cancelled: bool = field(compare=False, default=False)


class Timer:
    """A scheduled event that can be cancelled before it fires."""

    def __init__(self, event: _Event) -> None:
        self._event = event

    @property
    def at(self) -> int:
        """The virtual time it fires at."""
        return self._event.at

    @property
    def label(self) -> str:
        """What the event is for, for debugging."""
        return self._event.label

    @property
    def cancelled(self) -> bool:
        """Whether it was cancelled."""
        return self._event.cancelled

    def cancel(self) -> None:
        """Keep the event from firing. Cancelling twice, or after it fired, does nothing."""
        self._event.cancelled = True


class InlineExecutor(ThreadPoolExecutor):
    """Runs submitted work at once, in the calling thread, and never starts a thread.

    asyncio's `loop.run_in_executor(None, ...)` uses the loop's default executor, which
    must be a `ThreadPoolExecutor` (CPython 3.12, `set_default_executor`). LangGraph runs
    synchronous callables through it in asynchronous runs (langchain-core 1.8.0,
    `run_in_executor`; langgraph 1.2.22, `pregel/_call.py`). A thread would finish at a
    wall-clock moment the scheduler cannot see, so this runs the work inline instead.
    """

    def submit(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Future[T]:
        future: Future[T] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as error:  # handed to the awaiting task, as a thread would
            future.set_exception(error)
        return future


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class Scheduler:
    """Runs a run's components one step at a time on a virtual clock."""

    def __init__(self, clock: VirtualClock | None = None) -> None:
        self.clock = clock if clock is not None else VirtualClock()
        self._events: list[_Event] = []
        self._next_seq = 0
        self._next_task = 0
        self._tasks: dict[str, dict[asyncio.Task[Any], int]] = {}
        self._incarnations: dict[str, int] = {}
        self._down: set[str] = set()
        self._failures: list[tuple[str, BaseException]] = []
        self._crash_listeners: list[Callable[[str], None]] = []
        self._real = RealWaits()

    @property
    def now(self) -> int:
        """The run's virtual time, in milliseconds."""
        return self.clock.now

    # Timed events --------------------------------------------------------------------------

    def call_at(self, at: int, callback: Callable[[], None], *, label: str = "") -> Timer:
        """Fire `callback` at virtual time `at`, after earlier and same-time events."""
        if at < self.now:
            raise ValueError(f"cannot schedule {label!r} at {at}, before now ({self.now})")
        event = _Event(at, self._next_seq, callback, label)
        self._next_seq += 1
        heapq.heappush(self._events, event)
        return Timer(event)

    def call_later(self, delay: int, callback: Callable[[], None], *, label: str = "") -> Timer:
        """Fire `callback` `delay` virtual milliseconds from now."""
        if delay < 0:
            raise ValueError(f"a delay cannot be negative: {delay}")
        return self.call_at(self.now + delay, callback, label=label)

    async def sleep(self, ms: int) -> None:
        """Suspend the calling task for `ms` virtual milliseconds."""
        if ms < 0:
            raise ValueError(f"cannot sleep for a negative time: {ms}")
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        timer = self.call_later(ms, lambda: _resolve(future), label="sleep")
        try:
            await future
        finally:
            timer.cancel()

    async def wait_for(self, awaitable: Awaitable[T], timeout_ms: int) -> T:
        """Await `awaitable`; after `timeout_ms` virtual ms, cancel it and raise.

        Raises `SchedulerTimeout` when the limit passes first.
        """
        if timeout_ms < 0:
            raise ValueError(f"a timeout cannot be negative: {timeout_ms}")
        inner = asyncio.ensure_future(awaitable)
        expired = False

        def expire() -> None:
            nonlocal expired
            if not inner.done():
                expired = True
                inner.cancel()

        timer = self.call_later(timeout_ms, expire, label="timeout")
        try:
            return await inner
        except asyncio.CancelledError:
            if expired:
                raise SchedulerTimeout(f"no result within {timeout_ms} virtual ms") from None
            raise
        finally:
            timer.cancel()

    async def hold(self, awaitable: Awaitable[T]) -> T:
        """Await `awaitable`, a wait on the real world such as a live model call, with
        virtual time held still.

        Until it ends, `run` fires no event and waits for it in wall time, so the wait
        takes no virtual time. `awaitable` must not wait on the scheduler: nothing the
        scheduler controls can happen until it ends.
        """
        return await self._real.hold(awaitable)

    # Components ----------------------------------------------------------------------------

    def spawn(self, component: str, coro: Coroutine[Any, Any, T], *, name: str) -> asyncio.Task[T]:
        """Run `coro` as a task of `component`. A crash of the component cancels it."""
        if component in self._down:
            coro.close()
            raise RuntimeError(f"{component} is down")
        self._next_task += 1
        task = asyncio.get_running_loop().create_task(
            coro, name=f"{component} {self._next_task}: {name}"
        )
        self._tasks.setdefault(component, {})[task] = self._next_task
        task.add_done_callback(lambda done: self._task_finished(component, done))
        return task

    def _task_finished(self, component: str, task: asyncio.Task[Any]) -> None:
        self._tasks.get(component, {}).pop(task, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._failures.append((task.get_name(), error))

    def is_up(self, component: str) -> bool:
        """Whether `component` is running (not crashed and waiting for its restart)."""
        return component not in self._down

    def incarnation(self, component: str) -> int:
        """How many times `component` has crashed."""
        return self._incarnations.get(component, 0)

    def on_crash(self, listener: Callable[[str], None]) -> None:
        """Call `listener(component)` whenever a component crashes."""
        self._crash_listeners.append(listener)

    def crash(
        self,
        component: str,
        *,
        restart_after_ms: int | None = None,
        restart: Callable[[], None] | None = None,
    ) -> None:
        """Stop `component` now, discarding its memory, and maybe restart it later (0006).

        Its tasks are cancelled in the order they were spawned. `restart`, called when the
        restart event fires, rebuilds the component, for example through startup
        recovery (0003) or from the agent's last checkpoint. It is called only if this
        crash is still the component's latest when the event fires.
        """
        if (restart_after_ms is None) != (restart is None):
            raise ValueError("give both restart_after_ms and restart, or neither")
        self._down.add(component)
        incarnation = self.incarnation(component) + 1
        self._incarnations[component] = incarnation
        spawned = self._tasks.pop(component, {})
        for task in sorted(spawned, key=spawned.__getitem__):
            task.cancel()
        for listener in self._crash_listeners:
            listener(component)
        if restart_after_ms is not None and restart is not None:
            self.call_later(
                restart_after_ms,
                lambda: self._restart(component, incarnation, restart),
                label=f"restart {component}",
            )

    def _restart(self, component: str, incarnation: int, restart: Callable[[], None]) -> None:
        """Bring `component` back as `incarnation`, unless a later crash has started a
        newer one: then that crash's own restart brings it back, and this one does
        nothing."""
        if self.incarnation(component) != incarnation:
            return
        self._down.discard(component)
        restart()

    # Running -------------------------------------------------------------------------------

    async def run(
        self, *, until: Callable[[], bool] | None = None, deadline_ms: int | None = None
    ) -> RunEnd:
        """Fire events in order until `until()` holds, nothing is left, or the deadline.

        Raises `ComponentFailed` if a component task raised an exception that nothing
        handled. At the deadline, the clock moves to it and later events stay queued.
        """
        loop = asyncio.get_running_loop()
        if not isinstance(getattr(loop, "_default_executor", None), InlineExecutor):
            loop.set_default_executor(InlineExecutor())
        while True:
            await settle(self._real)
            self._raise_failures()
            if until is not None and until():
                return RunEnd.UNTIL
            event = self._next_event()
            if event is None:
                return RunEnd.IDLE
            if deadline_ms is not None and event.at > deadline_ms:
                heapq.heappush(self._events, event)
                self.clock.advance_to(max(self.now, deadline_ms))
                return RunEnd.DEADLINE
            self.clock.advance_to(event.at)
            event.callback()

    def pending(self) -> list[Timer]:
        """The events still queued, in firing order."""
        return [Timer(event) for event in sorted(self._events) if not event.cancelled]

    def _next_event(self) -> _Event | None:
        while self._events:
            event = heapq.heappop(self._events)
            if not event.cancelled:
                return event
        return None

    def _raise_failures(self) -> None:
        if self._failures:
            name, error = self._failures[0]
            raise ComponentFailed(f"task {name} failed: {error!r}") from error
