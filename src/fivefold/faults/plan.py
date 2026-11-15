"""Fault plans and the injector that fires them (0006, Plans).

A plan is a fixed list. Each entry names a fault, a point, an optional match on the
event's fields and an occurrence number: the fault fires the Nth time the point is
reached with a matching event in this run. Each entry keeps its own counter, so new
unrelated events never shift a plan, and faults never fire on wall-clock time.

Before a run starts, `check_configuration` and `check_run` refuse a plan that the run's
configuration, contract or timing cannot carry out, and `seed_parameters` draws each
seeded parameter the plan leaves unset, such as F4's `copy_delay_ms`, from the run's
`faults` generator (0009). The run records the plan with the values it drew.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fivefold.domain.contracts import Capability, Contract
from fivefold.domain.scenario import Params
from fivefold.faults.catalog import (
    BUILT,
    F5_BEYOND_DEADLINE,
    F10_STALE_WITHIN_LAG,
    POINTS,
    FaultId,
    FaultKind,
    point_exists,
)


class Trigger(BaseModel):
    """Where and when a fault fires."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    point: str
    match: dict[str, str | int] = Field(default_factory=dict)
    occurrence: int = Field(default=1, ge=1)


class PlannedFault(BaseModel):
    """One entry of a scenario's fault plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fault: FaultId
    variant: str | None = None
    at: Trigger
    params: dict[str, int | str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known(self) -> PlannedFault:
        if self.at.point not in POINTS:
            raise ValueError(f"{self.at.point} is not a fault point (0006)")
        kind = BUILT.get(self.fault)
        if kind is None:
            raise ValueError(f"{self.fault} cannot be injected by this build yet")
        if self.at.point not in kind.points:
            raise ValueError(f"{self.fault} does not fire at {self.at.point} (0006)")
        unknown = sorted(set(self.params) - set(kind.params) - set(kind.seeded))
        if unknown:
            raise ValueError(f"{self.fault} has no parameters {unknown}")
        for name, least in kind.minimums.items():
            value = self.params.get(name, kind.params.get(name))
            if value is None:
                continue
            if not isinstance(value, int) or value < least:
                raise ValueError(f"{self.fault} parameter {name} must be at least {least}")
        if self.variant is None and kind.variant_required:
            raise ValueError(f"{self.fault} needs a variant: {sorted(kind.variants)}")
        if self.variant is not None:
            if self.variant not in kind.variants:
                allowed = sorted(kind.variants) or "none"
                raise ValueError(f"{self.fault} has no variant {self.variant!r}: {allowed}")
            points = kind.variants[self.variant]
            if points is not None and self.at.point not in points:
                raise ValueError(
                    f"{self.fault} {self.variant} fires at {sorted(points)}, not {self.at.point}"
                )
        return self

    @property
    def kind(self) -> FaultKind:
        """The catalog entry of this fault."""
        return BUILT[self.fault]

    def param(self, name: str) -> int:
        """A numeric parameter, or its catalog default.

        A seeded parameter the plan leaves unset has no value until `seed_parameters`
        draws one.
        """
        if name in self.params:
            value = self.params[name]
        elif name in self.kind.seeded:
            raise ValueError(f"{self.fault} parameter {name} is drawn when the run starts")
        else:
            value = self.kind.params[name]
        if not isinstance(value, int):
            raise ValueError(f"{self.fault} parameter {name} must be an integer")
        return value


def parse_plan(entries: Sequence[Mapping[str, Any]]) -> tuple[PlannedFault, ...]:
    """Validate a scenario's `faults` list (0006, Plans)."""
    return tuple(PlannedFault.model_validate(entry) for entry in entries)


def check_configuration(plan: Sequence[PlannedFault], *, gateway: bool) -> None:
    """Refuse a plan with a fault at a point the configuration does not have.

    Such a fault would never fire, or, at a B0-equivalent point in a G mode, would fire
    as the wrong fault. `gateway` is whether the configuration has one (G1, G2).
    """
    wrong = [
        f"{planned.fault} at {planned.at.point}"
        for planned in plan
        if not point_exists(planned.kind, planned.at.point, gateway=gateway)
    ]
    if wrong:
        where = "with a gateway" if gateway else "without a gateway"
        raise ValueError(f"not in a configuration {where}: {', '.join(wrong)} (0006, 0008)")


class Draws(Protocol):
    """A seeded generator, as the run's named generators are (0009)."""

    def randint(self, a: int, b: int) -> int:
        """A whole number from `a` to `b`, both included."""
        ...


def seed_parameters(plan: Sequence[PlannedFault], draws: Draws) -> tuple[PlannedFault, ...]:
    """The plan, with each seeded parameter it leaves unset drawn from `draws` (0009).

    Entries are seeded in plan order, and each entry's parameters in name order, so a
    seed always gives the same values. A value the plan sets is kept.
    """
    seeded: list[PlannedFault] = []
    for planned in plan:
        drawn = {
            name: draws.randint(low, high)
            for name, (low, high) in sorted(planned.kind.seeded.items())
            if name not in planned.params
        }
        if drawn:
            planned = planned.model_copy(update={"params": {**planned.params, **drawn}})
        seeded.append(planned)
    return tuple(seeded)


def check_run(plan: Sequence[PlannedFault], *, params: Params, contract: Contract) -> None:
    """Refuse a plan that the run's timing or contract level cannot carry out (0005, 0006).

    - F5 in contract holds a write past the caller's timeout and commits it before the
      deadline: T < `delay_ms` < D. Beyond the deadline it commits after D: `delay_ms` > D,
      and the plan must set it, since 0006 gives that variant no default.
    - F10 acts on status answers, which only C1 gives. Its `stale_within_lag` variant needs
      a status lag L above 0, or it changes nothing (0006, notes).
    """
    t_ms, d_ms = params.dispatch_timeout_ms, params.commit_deadline_ms
    wrong: list[str] = []
    for planned in plan:
        if planned.fault is FaultId.F5:
            delay = planned.param("delay_ms")
            if planned.variant == F5_BEYOND_DEADLINE:
                if "delay_ms" not in planned.params:
                    wrong.append(f"F5 {planned.variant} needs delay_ms set: 0006 gives no default")
                elif delay <= d_ms:
                    wrong.append(f"F5 {planned.variant} needs delay_ms above D ({d_ms})")
            elif not t_ms < delay < d_ms:
                wrong.append(f"F5 in contract needs delay_ms between T ({t_ms}) and D ({d_ms})")
        elif planned.fault is FaultId.F10:
            if not contract.has(Capability.STATUS):
                wrong.append(f"F10 needs a status lookup, which {contract} does not offer")
            if planned.variant == F10_STALE_WITHIN_LAG and params.status_lag_ms == 0:
                wrong.append(f"F10 {planned.variant} needs status_lag_ms above 0")
    if wrong:
        raise ValueError(f"the plan does not fit the run: {'; '.join(wrong)} (0005, 0006)")


class Fired:
    """A planned fault firing now, at its occurrence."""

    def __init__(self, planned: PlannedFault, occurrence: int) -> None:
        self.planned = planned
        self.occurrence = occurrence

    @property
    def fault(self) -> FaultId:
        """The fault's id."""
        return self.planned.fault


class FaultInjector:
    """Counts matching events at each point and fires planned faults on time."""

    def __init__(self, plan: Sequence[PlannedFault] = ()) -> None:
        self._plan = tuple(plan)
        self._counts = [0] * len(self._plan)

    def at(self, point: str, fields: Mapping[str, object]) -> list[Fired]:
        """Report reaching `point` with an event's `fields`; return the faults that fire.

        A match compares each of its fields with the event's, as text.
        """
        if point not in POINTS:
            raise ValueError(f"{point} is not a fault point (0006)")
        fired: list[Fired] = []
        for index, planned in enumerate(self._plan):
            if planned.at.point != point:
                continue
            if any(str(fields.get(key)) != str(value) for key, value in planned.at.match.items()):
                continue
            self._counts[index] += 1
            if self._counts[index] == planned.at.occurrence:
                fired.append(Fired(planned, self._counts[index]))
        return fired
