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
(pyproject.toml), and `test_clock.py` fails loudly if either internal disappears.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Sequence
from typing import cast


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


async def settle() -> None:
    """Return once every other task waits on something the scheduler controls.

    Ready callbacks run first. A task that sleeps on a wall-clock timer, such as
    LangGraph's retry backoff (0008), holds the run until its timer fires, because
    virtual time must not move while it sleeps: the backoff changes wall-clock metrics
    only (0009). The run has no other wall-clock timers and no network I/O.
    """
    loop = asyncio.get_running_loop()
    ready = _ready_queue(loop)
    while True:
        await yield_to_loop()
        if ready:
            continue
        pending = [timer.when() for timer in _timer_heap(loop) if not timer.cancelled()]
        if not pending:
            return
        await asyncio.sleep(max(0.0, min(pending) - loop.time()))
