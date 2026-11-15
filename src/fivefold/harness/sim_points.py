"""Faults at the simulator's fault points: F5 and F10 (0005, 0006).

The simulator's endpoint (`fivefold.sim.endpoint`) asks a `SimulatorPoints` what fires
at its two points:

- `sim.write.received`, as a write request reaches the destination. F5 holds the write
  and commits it `delay_ms` later, through the simulator's two-phase write. In contract,
  T < `delay_ms` < D, so the caller has timed out and the commit still meets the
  deadline. Beyond the deadline, `delay_ms` > D and the deadline is waived: a contract
  violation. The answer goes back when the write commits.
- `sim.status.answer`, as a status answer is produced. F10 shapes this answer and the
  next ones, `count` in all:
  - `delayed`: the answer is held for T (`dispatch_timeout_ms`) before it is sent, so it
    arrives after the caller's timeout, and the probe counts as unanswered;
  - `stale_within_lag`: the answer is accurate to L (`status_lag_ms`) before it is
    produced, as stale as the destination's promise allows, and says so;
  - `stale_beyond_lag`: the answer lists the effects committed before the virtual
    millisecond in which F10 fired, the same in every answer F10 shapes, but claims to
    be accurate to the time it is produced: a contract violation. A write that commits
    later in that millisecond is in none of them.
  When two F10 entries overlap, every one counts the answer, and the one that fired
  first shapes it.

Each fault that fires gets a row in the fault log, naming the request it acted on and
the simulator as the component. Its params hold the values it used: `delay_ms` for F5,
`count` for F10.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fivefold.faults.catalog import (
    F5_BEYOND_DEADLINE,
    F10_DELAYED,
    F10_STALE_BEYOND_LAG,
    F10_STALE_WITHIN_LAG,
    Action,
)
from fivefold.faults.plan import FaultInjector, Fired
from fivefold.harness.log import FaultRecord, HarnessLog
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Envelope
from fivefold.sim.endpoint import StatusShape, WriteDelay

SIMULATOR = "simulator"

WRITE_RECEIVED = "sim.write.received"
STATUS_ANSWER = "sim.status.answer"


class SimulatorPointError(RuntimeError):
    """A fault fired at a simulator point where it has nothing to do: a plan bug."""


@dataclass
class _Shaping:
    """An F10 that fired, with the answers it still shapes."""

    fired: Fired
    left: int
    fired_vt: int


class SimulatorPoints:
    """Fires F5 and F10 at the simulator's points, and logs them (0006)."""

    def __init__(
        self,
        scheduler: Scheduler,
        faults: FaultInjector,
        log: HarnessLog,
        *,
        dispatch_timeout_ms: int,
        status_lag_ms: int,
    ) -> None:
        self._scheduler = scheduler
        self._faults = faults
        self._log = log
        self._t_ms = dispatch_timeout_ms
        self._lag_ms = status_lag_ms
        self._shaping: list[_Shaping] = []

    def write_received(self, request: Envelope) -> WriteDelay | None:
        """`sim.write.received`: F5 holds the write and commits it `delay_ms` later.

        A write is held once. Two F5 entries that fire at the same write are a plan bug:
        the second would be recorded and take no effect, so the run stops instead.
        """
        fired = self._faults.at(WRITE_RECEIVED, _fields(request))
        for one in fired:
            if one.planned.kind.action is not Action.DELAY_COMMIT:
                raise SimulatorPointError(f"{one.fault} cannot act at {WRITE_RECEIVED}")
        if not fired:
            return None
        if len(fired) > 1:
            raise SimulatorPointError(
                f"{len(fired)} F5 entries fired at one write (message {request.seq});"
                " a write is held once"
            )
        (hold,) = fired
        delay_ms = hold.planned.param("delay_ms")
        self._record(hold, WRITE_RECEIVED, request, {"delay_ms": delay_ms})
        return WriteDelay(delay_ms, waive_deadline=hold.planned.variant == F5_BEYOND_DEADLINE)

    def status_answer(self, request: Envelope) -> StatusShape | None:
        """`sim.status.answer`: F10 shapes this answer and the next, `count` in all."""
        now = self._scheduler.now
        for fired in self._faults.at(STATUS_ANSWER, _fields(request)):
            if fired.planned.kind.action is not Action.DEGRADE_STATUS:
                raise SimulatorPointError(f"{fired.fault} cannot act at {STATUS_ANSWER}")
            count = fired.planned.param("count")
            self._record(fired, STATUS_ANSWER, request, {"count": count})
            self._shaping.append(_Shaping(fired, count, now))
        if not self._shaping:
            return None
        first = self._shaping[0]
        for shaping in self._shaping:
            shaping.left -= 1
        self._shaping = [shaping for shaping in self._shaping if shaping.left > 0]
        variant = first.fired.planned.variant
        if variant == F10_DELAYED:
            return StatusShape(hold_ms=self._t_ms)
        if variant == F10_STALE_WITHIN_LAG:
            return StatusShape(as_of_vt=max(0, now - self._lag_ms))
        if variant == F10_STALE_BEYOND_LAG:
            # The simulator lists effects committed at or before `reflect_vt`. Freezing at
            # the millisecond before F10 fired keeps every shaped answer alike, even when a
            # write commits later in the millisecond F10 fired in.
            frozen_vt = max(0, first.fired_vt - 1)
            return StatusShape(reflect_vt=frozen_vt, contract_violation=True)
        raise SimulatorPointError(f"F10 has no variant {variant!r}")

    def _record(
        self, fired: Fired, point: str, request: Envelope, used: Mapping[str, object]
    ) -> None:
        self._log.fault_fired(
            FaultRecord(
                seq=self._log.next_seq(),
                fault=str(fired.fault),
                variant=fired.planned.variant,
                point=point,
                occurrence=fired.occurrence,
                action=str(fired.planned.kind.action),
                vt=self._scheduler.now,
                target_seq=request.seq,
                component=SIMULATOR,
                params={**fired.planned.params, **used},
            )
        )


def _fields(request: Envelope) -> dict[str, object]:
    """What a plan's match can name at a simulator point: the call and its intent."""
    return {"api": request.api, "tool": request.api, "intent_id": request.intent_id}
