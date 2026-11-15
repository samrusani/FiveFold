"""Fault plans and the injector that fires them (0006, Plans).

A plan is a fixed list. Each entry names a fault, a point, an optional match on the
event's fields and an occurrence number: the fault fires the Nth time the point is
reached with a matching event in this run. Each entry keeps its own counter, so new
unrelated events never shift a plan, and faults never fire on wall-clock time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fivefold.faults.catalog import BUILT, POINTS, FaultId, FaultKind, point_exists


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
        unknown = sorted(set(self.params) - set(kind.params))
        if unknown:
            raise ValueError(f"{self.fault} has no parameters {unknown}")
        for name, least in kind.minimums.items():
            value = self.params.get(name, kind.params[name])
            if not isinstance(value, int) or value < least:
                raise ValueError(f"{self.fault} parameter {name} must be at least {least}")
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
        """A numeric parameter, or its catalog default."""
        value = self.params.get(name, self.kind.params[name])
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
