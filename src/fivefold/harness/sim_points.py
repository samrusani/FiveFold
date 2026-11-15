"""Faults at the simulator's fault points: F5 (0005, 0006).

The simulator's endpoint (`fivefold.sim.endpoint`) asks a `SimulatorPoints` what fires
at `sim.write.received`, as a write request reaches the destination. F5 holds the write
and commits it `delay_ms` later, through the simulator's two-phase write. In contract,
T < `delay_ms` < D, so the caller has timed out and the commit still meets the deadline.
Beyond the deadline, `delay_ms` > D and the deadline is waived: a contract violation.
The answer goes back when the write commits.

Each fault that fires gets a row in the fault log, naming the request it acted on and
the simulator as the component. Its params hold the values it used: `delay_ms` for F5.
"""

from __future__ import annotations

from collections.abc import Mapping

from fivefold.faults.catalog import F5_BEYOND_DEADLINE, Action
from fivefold.faults.plan import FaultInjector, Fired
from fivefold.harness.log import FaultRecord, HarnessLog
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Envelope
from fivefold.sim.endpoint import WriteDelay

SIMULATOR = "simulator"

WRITE_RECEIVED = "sim.write.received"


class SimulatorPointError(RuntimeError):
    """A fault fired at a simulator point where it has nothing to do: a plan bug."""


class SimulatorPoints:
    """Fires F5 at the simulator's points, and logs it (0006)."""

    def __init__(self, scheduler: Scheduler, faults: FaultInjector, log: HarnessLog) -> None:
        self._scheduler = scheduler
        self._faults = faults
        self._log = log

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
