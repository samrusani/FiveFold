"""The run's virtual clock, and the only wall-clock access in the harness (0009).

Every component reads time only from the run's `VirtualClock`: grant expiry, timeouts,
`action_event.vt`, `attempt.sent_vt` and `effect.committed_vt` are virtual milliseconds.
This module and `rng.py` are the only places allowed to use wall-clock time or system
randomness; the lint configuration in pyproject.toml enforces it (0007). Wall-clock time
is read here for metrics only, never for decisions.

`settle` lets every other task of the event loop run until each one waits on something
the scheduler controls. It reads two internals of CPython 3.12's asyncio event loop, the
ready queue (`_ready`) and the timer heap (`_scheduled`), checked in
`Lib/asyncio/base_events.py` of Python 3.12.3. The project pins Python 3.12
(pyproject.toml), and `test_scheduler.py` fails loudly if either internal disappears.

A task that waits on the real world, as a live model call waits on its provider, does so
inside a real wait (`RealWaits.hold`). Network I/O registers nothing that `settle` can
see: an HTTP client with no timeout leaves no timer in the heap. So while a real wait is
in flight, `settle` waits for it to end, in wall time, and does not return. Virtual time
stands still meanwhile, and the call takes no virtual time of its own (0009).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Sequence
from typing import TypeVar, cast

T = TypeVar("T")


class VirtualClock:
    """Virtual milliseconds since the run started. Only the scheduler moves it."""

    def __init__(self, start_ms: int = 0) -> None:
        if start_ms < 0:
            raise ValueError("virtual time starts at or after 0")
        self._now = start_ms

    @property
    def now(self) -> int:
        """The current virtual time, in milliseconds."""
        return self._now

    def advance_to(self, at_ms: int) -> None:
        """Move the clock forward to `at_ms`. Virtual time never runs backwards."""
        if at_ms < self._now:
            raise ValueError(f"virtual time cannot go back from {self._now} to {at_ms}")
        self._now = at_ms


def wall_ms() -> float:
    """Wall-clock milliseconds from a monotonic clock, for metrics only (0009)."""
    return time.monotonic() * 1000.0


class LoopInternalsMissing(RuntimeError):
    """The running event loop lacks the CPython internals `settle` reads."""


def _ready_queue(loop: asyncio.AbstractEventLoop) -> deque[asyncio.Handle]:
    ready = getattr(loop, "_ready", None)
    if not isinstance(ready, deque):
        raise LoopInternalsMissing("the event loop has no _ready deque (CPython 3.12 asyncio)")
    return cast(deque[asyncio.Handle], ready)


def _timer_heap(loop: asyncio.AbstractEventLoop) -> Sequence[asyncio.TimerHandle]:
    scheduled = getattr(loop, "_scheduled", None)
    if not isinstance(scheduled, list):
        raise LoopInternalsMissing("the event loop has no _scheduled list (CPython 3.12 asyncio)")
    return cast(list[asyncio.TimerHandle], scheduled)


async def yield_to_loop() -> None:
    """Let every callback that is ready now run once, then continue."""
    await asyncio.sleep(0)


class RealWaits:
    """The real waits in flight, such as live model calls awaiting their provider (0009).

    While one is in flight, virtual time stands still: `settle` does not return, so the
    scheduler fires no event, and the run waits for it in wall time. What a real wait
    awaits must never wait on the scheduler, which cannot move until the wait ends.
    """

    def __init__(self) -> None:
        self._count = 0
        self._waiters: list[asyncio.Future[None]] = []

    @property
    def active(self) -> bool:
        """Whether a real wait is in flight."""
        return self._count > 0

    async def hold(self, awaitable: Awaitable[T]) -> T:
        """Await `awaitable`, a real wait, with virtual time held still until it ends."""
        self._count += 1
        try:
            return await awaitable
        finally:
            self._count -= 1
            if self._count == 0:
                waiters, self._waiters = self._waiters, []
                for waiter in waiters:
                    if not waiter.done():
                        waiter.set_result(None)

    async def ended(self) -> None:
        """Return once no real wait is in flight."""
        if self._count == 0:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        await waiter


async def settle(real: RealWaits | None = None) -> None:
    """Return once every other task waits on something the scheduler controls.

    Ready callbacks run first. A task in one of `real`'s real waits, such as a live model
    call awaiting its provider, holds the run until the wait ends. A task that sleeps on a
    wall-clock timer, such as LangGraph's retry backoff (0008), holds it until its timer
    fires. Virtual time must not move during either: they change wall-clock metrics only
    (0009). While a real wait is in flight, `settle` waits for that wait alone, so a timer
    the real wait itself set, such as an HTTP client's timeout, never keeps the run
    asleep after the answer arrives.
    """
    loop = asyncio.get_running_loop()
    ready = _ready_queue(loop)
    while True:
        await yield_to_loop()
        if ready:
            continue
        if real is not None and real.active:
            await real.ended()
            continue
        pending = [timer.when() for timer in _timer_heap(loop) if not timer.cancelled()]
        if not pending:
            return
        await asyncio.sleep(max(0.0, min(pending) - loop.time()))
