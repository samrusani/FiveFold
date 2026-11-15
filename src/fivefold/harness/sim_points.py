"""Faults at the simulator's fault points: F5 and F10 (0005, 0006, 0013).

The simulator's endpoint (`fivefold.sim.endpoint`) asks a `SimulatorPoints` what fires
at its two points:

- `sim.write.received`, as a write request reaches the destination.
  - F5 holds the write and commits it `delay_ms` later, through the simulator's two-phase
    write. In contract, T < `delay_ms` < D, so the caller has timed out and the commit
    still meets the deadline. Beyond the deadline, `delay_ms` > D and the deadline is
    waived: a contract violation. The answer goes back when the write commits.
  - F10 `stale_beyond_lag` freezes the state as it was just before this write arrived
    (0013). Each of the next `count` status answers lists the effects committed before
    the virtual millisecond in which the write arrived, the same in every answer, but
    claims to be accurate to the time it is produced: a contract violation. So the write
    is in none of them, even when it commits in that millisecond. The status lookup
    counts time in whole milliseconds, so an effect committed earlier in that millisecond
    is left out too.
- `sim.status.answer`, as a status answer is produced. F10 `delayed` and
  `stale_within_lag` fire here, and shape this answer and the next ones, `count` in all:
  - `delayed`: the answer is held for T (`dispatch_timeout_ms`) before it is sent, so it
    arrives after the caller's timeout, and the probe counts as unanswered;
  - `stale_within_lag`: the answer is accurate to L (`status_lag_ms`) before it is
    produced, as stale as the destination's promise allows, and says so.

Every status answer counts toward each F10 that has fired, wherever it fired. When two
F10 entries overlap, every one counts the answer, and the one that fired first shapes it.

Each fault that fires gets a row in the fault log, naming the request it acted on, the
request's intent, and the simulator as the component. Its params hold the values it
used: `delay_ms` for F5, `count` for F10. F10 also gets one row for each status answer it
changes, whichever variant and wherever it fired, once that answer is sent
(`answer_sent`): the answer is the row's target, its intent the row's intent, and
`answer_no` counts the answers F10 changed, from 1 (0014).
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
from fivefold.harness.transport import Envelope, Stage
from fivefold.sim.endpoint import StatusShape, WriteDelay

SIMULATOR = "simulator"

WRITE_RECEIVED = "sim.write.received"
STATUS_ANSWER = "sim.status.answer"

ACTIONS: Mapping[str, frozenset[Action]] = {
    WRITE_RECEIVED: frozenset({Action.DELAY_COMMIT, Action.DEGRADE_STATUS}),
    STATUS_ANSWER: frozenset({Action.DEGRADE_STATUS}),
}
"""What a fault may do at each simulator point: F5 holds a write, F10 shapes status."""


class SimulatorPointError(RuntimeError):
    """A fault fired at a simulator point where it has nothing to do: a plan bug."""


@dataclass
class _Shaping:
    """An F10 that fired, with the answers it still shapes."""

    fired: Fired
    left: int
    fired_vt: int
    changed: int = 0
    """The status answers it has changed so far."""


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
        self._changed: dict[int, tuple[_Shaping, int]] = {}
        """The status requests whose answers F10 changed and has not logged yet: the F10
        that shapes each answer, and the answer's number among those it changed."""

    def write_received(self, request: Envelope) -> WriteDelay | None:
        """`sim.write.received`: F5 holds the write and commits it `delay_ms` later, and
        F10 `stale_beyond_lag` freezes the state the next `count` status answers list.

        A write is held once. Two F5 entries that fire at the same write are a plan bug:
        the second would be recorded and take no effect, so the run stops instead.
        """
        fired = self._faults.at(WRITE_RECEIVED, _fields(request))
        for one in fired:
            _check(one, WRITE_RECEIVED)
        holds = [one for one in fired if one.planned.kind.action is Action.DELAY_COMMIT]
        if len(holds) > 1:
            raise SimulatorPointError(
                f"{len(holds)} F5 entries fired at one write (message {request.seq});"
                " a write is held once"
            )
        delay: WriteDelay | None = None
        for one in fired:
            if one.planned.kind.action is Action.DEGRADE_STATUS:
                self._start_shaping(one, WRITE_RECEIVED, request)
                continue
            delay_ms = one.planned.param("delay_ms")
            self._record(one, WRITE_RECEIVED, request, {"delay_ms": delay_ms})
            delay = WriteDelay(delay_ms, waive_deadline=one.planned.variant == F5_BEYOND_DEADLINE)
        return delay

    def status_answer(self, request: Envelope) -> StatusShape | None:
        """`sim.status.answer`: F10 `delayed` and `stale_within_lag` fire here. Every F10
        that has fired shapes this answer and the next, `count` in all."""
        now = self._scheduler.now
        fired = self._faults.at(STATUS_ANSWER, _fields(request))
        for one in fired:
            _check(one, STATUS_ANSWER)
        for one in fired:
            self._start_shaping(one, STATUS_ANSWER, request)
        if not self._shaping:
            return None
        first = self._shaping[0]
        first.changed += 1
        self._changed[request.seq] = (first, first.changed)
        for shaping in self._shaping:
            shaping.left -= 1
        self._shaping = [shaping for shaping in self._shaping if shaping.left > 0]
        variant = first.fired.planned.variant
        if variant == F10_DELAYED:
            return StatusShape(hold_ms=self._t_ms)
        if variant == F10_STALE_WITHIN_LAG:
            return StatusShape(as_of_vt=max(0, now - self._lag_ms))
        if variant == F10_STALE_BEYOND_LAG:
            # The simulator lists effects committed at or before `reflect_vt`. The write F10
            # fired at commits in the millisecond it arrived in at the earliest, so freezing
            # at the millisecond before leaves it out of every shaped answer.
            frozen_vt = max(0, first.fired_vt - 1)
            return StatusShape(reflect_vt=frozen_vt, contract_violation=True)
        raise SimulatorPointError(f"F10 has no variant {variant!r}")

    def answer_sent(self, envelope: Envelope, stage: Stage) -> None:
        """A transport observer: once a status answer F10 changed is sent, log one more
        row for that F10, naming the answer and its intent (0014). F10 acts on the next
        `count` status answers, which can concern several intents, so each answer it
        changes touches the answer's intent."""
        if stage != "sent" or envelope.kind != "answer" or envelope.correlation is None:
            return
        changed = self._changed.pop(envelope.correlation, None)
        if changed is None:
            return
        shaping, number = changed
        count = shaping.fired.planned.param("count")
        self._record(shaping.fired, STATUS_ANSWER, envelope, {"count": count, "answer_no": number})

    def _start_shaping(self, fired: Fired, point: str, request: Envelope) -> None:
        """Log an F10 that fired, and shape the next `count` status answers with it."""
        count = fired.planned.param("count")
        self._record(fired, point, request, {"count": count})
        self._shaping.append(_Shaping(fired, count, self._scheduler.now))

    def _record(
        self, fired: Fired, point: str, message: Envelope, used: Mapping[str, object]
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
                target_seq=message.seq,
                component=SIMULATOR,
                params={**fired.planned.params, **used},
                intent_id=message.intent_id,
            )
        )


def _check(fired: Fired, point: str) -> None:
    """Refuse a fault with nothing to do at `point`, before anything is logged.

    The catalog already refuses such a plan. Without this check, the fault would be
    logged as fired and change nothing, or, for an F10 variant away from its own point,
    shape answers its variant does not describe (0013).
    """
    planned = fired.planned
    points = None if planned.variant is None else planned.kind.variants.get(planned.variant)
    if planned.kind.action not in ACTIONS[point] or (points is not None and point not in points):
        name = " ".join(part for part in (str(fired.fault), planned.variant) if part)
        raise SimulatorPointError(f"{name} cannot act at {point}")


def _fields(request: Envelope) -> dict[str, object]:
    """What a plan's match can name at a simulator point: the call and its intent."""
    return {"api": request.api, "tool": request.api, "intent_id": request.intent_id}
