"""Fault plans and the injector that fires them (0006, Plans).

A plan is a fixed list. Each entry names a fault, a point, an optional match on the
event's fields and an occurrence number: the fault fires the Nth time the point is
reached with a matching event in this run. Each entry keeps its own counter, so new
unrelated events never shift a plan, and faults never fire on wall-clock time.

Before a run starts, `check_configuration` and `check_run` refuse a plan that the run's
configuration, contract or timing cannot carry out, and `seed_parameters` draws each
seeded parameter the plan leaves unset, such as F4's `copy_delay_ms`, from the run's
`faults` generator (0009). The run records the plan with the values it drew.

`parse_plan` also refuses two crash faults with the very same trigger: the same point,
match and occurrence. They would fire at the same event on the same component, and one
crash already stops it. Two crashes whose triggers differ can still meet at one event;
then only the first fires, since a component it stopped reaches no further fault point
(`fivefold.harness.run`).

Random plans
------------

A plan can be random instead: `{random: {from: [F2, F4, F5, F12], max: 3}}` (0006,
Plans). `draw_plan` turns it into a fixed list before the run starts, drawing every choice
from the run's `faults` generator (0009), so one seed always gives one plan. The run then
checks, fires and records that list as it would a fixed one. Record 0006 names the faults
and the most to draw, and leaves the rest open. This module chooses simply, and only what
a fixed plan could also say:

- How many: from 1 to `max`, and at most the number of faults named, each count equally
  likely.
- Which: that many different faults of `from`, so each fault fires at most once. A plan
  then never holds two F5 entries for one write, or two F6 entries for one approval.
- Variant and trigger: equally likely among every combination the run allows. A
  combination is a variant (one is always named when the fault has variants), one of
  its points that exists in the run's configuration (`point_exists`), and a target. The
  target is the write of one task intent, as the cells of `faults/expected.yaml` target
  the purchase order: the match names the intent and its call at the point, which is the
  write itself, its committed answer for F2, its status call at `sim.status.answer`, or,
  for B0's early F6 and F7, its approval request. F7 targets only intents that order
  from a quote. F9 matches the first worker's steps. B0's early F6 and F7 fire as the
  answer to the approval request reaches the agent, so they target only intents whose
  every acceptable write policy approves at once, as dev-001's; where a human decides,
  that answer is still pending.
- Occurrence: 1, the first matching event, as in every cell. Each entry counts its own
  events, so faults drawn together still reach retries and resends: after F12 drops the
  gateway's first request, F2 drops the committed answer to its retry. A crash fault
  whose trigger an earlier crash in the plan holds takes the next occurrence instead,
  since `parse_plan` refuses two crashes with one trigger: F3's B0 equivalent and F11
  both kill the agent at `agent.after_tool_result`.
- Parameters: each drawn evenly from a range. `restart_delay_ms` from 0 to 10,000 ms
  for an agent worker's crash, demo 1's longest, and from 0 to 1,000 ms, the catalog's
  default, for the gateway's. A worker waits at most T for each new connection to the
  gateway, so a longer outage can end it with an error, and the run's faults with it,
  as an outage of 5,290 ms did in a trial run. F5's `delay_ms`
  from T + 1 to D - 1 in contract, and from D + 1 to 2 x D beyond the deadline, where
  the cells use 2 x D. F7's `percent` from 1 to 10 for `price_up`. F10's `count` from 1
  to its default, 20. F4's `copy_delay_ms` is drawn by `seed_parameters`, as for any
  plan.
- Every combination passes `check_configuration` and `check_run`, and those that fail
  are left out: F10 below C1, for example, or `stale_within_lag` with no status lag.
  F10 is left out in G1 too, where nothing ever asks for a status answer (0008).

A fault this build cannot inject (F8), or one that no combination lets fire in the run,
is refused in one line, before the run starts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fivefold.domain.authority import PolicyDecision
from fivefold.domain.contracts import Capability, Contract
from fivefold.domain.rules import Operation
from fivefold.domain.scenario import Authority, Intent, Params, PurchaseOrderAccept, Scenario
from fivefold.faults.catalog import (
    BUILT,
    F5_BEYOND_DEADLINE,
    F10_STALE_WITHIN_LAG,
    POINTS,
    Action,
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


_TriggerKey = tuple[str, tuple[tuple[str, str], ...], int]


def _trigger_key(planned: PlannedFault) -> _TriggerKey:
    """The entry's trigger as the injector compares it: the point, the match with its
    values as text, and the occurrence."""
    at = planned.at
    return (at.point, tuple(sorted((k, str(v)) for k, v in at.match.items())), at.occurrence)


def parse_plan(entries: Sequence[Mapping[str, Any]]) -> tuple[PlannedFault, ...]:
    """Validate a scenario's `faults` list (0006, Plans).

    Raises `ValueError` for an entry that is not a planned fault, and for two crash
    faults with the same trigger.
    """
    plan = tuple(PlannedFault.model_validate(entry) for entry in entries)
    crashes: dict[_TriggerKey, PlannedFault] = {}
    for planned in plan:
        if planned.kind.action is not Action.KILL:
            continue
        first = crashes.setdefault(_trigger_key(planned), planned)
        if first is not planned:
            at = planned.at
            raise ValueError(
                f"{first.fault} and {planned.fault} crash the same component at the same event"
                f" ({at.point}, occurrence {at.occurrence}); one crash already stops it, so"
                " give one of them another trigger (0006)"
            )
    return plan


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
    - F10 acts on status answers, which only C1 gives, wherever it fires: at a status
      answer, or where a write arrives for `stale_beyond_lag` (0013). Its
      `stale_within_lag` variant needs a status lag L above 0, or it changes nothing
      (0006, notes).

    Which point each F10 variant fires at is the catalog's (`PlannedFault`).
    """
    wrong = _misfits(plan, params=params, contract=contract)
    if wrong:
        raise ValueError(f"the plan does not fit the run: {'; '.join(wrong)} (0005, 0006)")


def _misfits(plan: Sequence[PlannedFault], *, params: Params, contract: Contract) -> list[str]:
    """What `check_run` refuses in `plan`, one line for each rule an entry breaks."""
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
    return wrong


# Random plans (see the module's docstring) ---------------------------------------------

CONFIGS = ("B0", "G1", "G2")
"""The configurations a run can have (0008)."""

AGENT_RESTART_MAX_MS = 10_000
"""The longest `restart_delay_ms` a random plan draws for an agent worker's crash: demo
1's (0006, Demos)."""

GATEWAY_RESTART_MAX_MS = 1_000
"""The longest `restart_delay_ms` a random plan draws for the gateway's crash: the
catalog's default, short enough for an agent worker's new connection, which waits at most
T, to find the gateway up."""

PERCENT_MAX = 10
"""The largest `percent` a random plan draws for F7 `price_up`: twice the default."""

FIRST_WORKER = "agent-1"
"""The worker whose steps a drawn F9 counts, as the cells' F9 does."""

APPROVAL_REQUEST = "request_approval"
"""B0's call whose answer holds an approval that policy grants at once (0008)."""

STATUS_CALLS: Mapping[Operation, str] = {
    Operation.SUBMIT_PURCHASE_ORDER: "get_order_status",
    Operation.RESERVE_INVENTORY: "get_reservation_status",
}
"""The destination's status call for each write operation (0005)."""


class RandomPlan(BaseModel):
    """A random plan's settings (0006, Plans): `from`, the faults it may draw, each named
    once, and `max`, the most it draws."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    faults: tuple[FaultId, ...] = Field(alias="from", min_length=1)
    most: int = Field(alias="max", ge=1)

    @model_validator(mode="after")
    def _each_once(self) -> RandomPlan:
        if len(set(self.faults)) != len(self.faults):
            names = ", ".join(map(str, self.faults))
            raise ValueError(f"a random plan names each fault once, not {names}")
        return self


class _Random(BaseModel):
    """A plan that is random: `{random: {from: [...], max: N}}`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    random: RandomPlan


def parse_random_plan(faults: Mapping[str, Any]) -> RandomPlan:
    """Validate a random plan, `{random: {from: [F2, F4], max: 2}}` (0006, Plans).

    Raises `ValueError` for any other shape. Which faults it can draw in a run is
    `draw_plan`'s to check.
    """
    return _Random.model_validate(faults).random


def drawable_faults(*, scenario: Scenario, config: str, contract: Contract) -> tuple[FaultId, ...]:
    """The faults a random plan can draw in a run of `scenario` in `config` at `contract`,
    in catalog order."""
    return tuple(
        fault
        for fault in FaultId
        if fault in BUILT
        and _choices(BUILT[fault], scenario=scenario, config=config, contract=contract)[0]
    )


def draw_plan(
    random: RandomPlan, draws: Draws, *, scenario: Scenario, config: str, contract: Contract
) -> tuple[PlannedFault, ...]:
    """The fixed plan `random` draws from `draws` for a run of `scenario` in `config` at
    `contract`, before its seeded parameters are drawn (`seed_parameters`). How it draws is
    in the module's docstring.

    Raises `ValueError`, in one line, when `random` names a fault this build cannot inject
    or one that cannot fire in the run.
    """
    choices: dict[FaultId, list[PlannedFault]] = {}
    refused: list[str] = []
    for fault in random.faults:
        kind = BUILT.get(fault)
        if kind is None:
            refused.append(f"{fault} cannot be injected by this build yet")
            continue
        found, why = _choices(kind, scenario=scenario, config=config, contract=contract)
        if not found:
            refused.append(f"{fault} cannot fire in {config} at {contract}: {why}")
        choices[fault] = found
    if refused:
        raise ValueError(f"the random plan cannot be drawn: {'; '.join(refused)} (0006)")
    left = list(random.faults)
    plan: list[PlannedFault] = []
    for _ in range(draws.randint(1, min(random.most, len(left)))):
        fault = left.pop(draws.randint(0, len(left) - 1))
        chosen = choices[fault][draws.randint(0, len(choices[fault]) - 1)]
        if chosen.kind.action is Action.KILL:
            taken = {_trigger_key(entry) for entry in plan if entry.kind.action is Action.KILL}
            while _trigger_key(chosen) in taken:
                later = chosen.at.model_copy(update={"occurrence": chosen.at.occurrence + 1})
                chosen = chosen.model_copy(update={"at": later})
        ranges = _ranges(chosen, scenario.params)
        drawn: dict[str, int | str] = {
            name: draws.randint(low, high) for name, (low, high) in sorted(ranges.items())
        }
        plan.append(chosen.model_copy(update={"params": drawn}))
    # Every entry was built to pass these; checking again keeps a mistake here from running.
    checked = parse_plan([entry.model_dump(mode="json") for entry in plan])
    check_configuration(checked, gateway=config != "B0")
    check_run(checked, params=scenario.params, contract=contract)
    return checked


def _choices(
    kind: FaultKind, *, scenario: Scenario, config: str, contract: Contract
) -> tuple[list[PlannedFault], str]:
    """Every variant and trigger of `kind` that can fire in the run, at the first matching
    event, each with its drawn parameters at the least of their ranges; or none, and
    why."""
    if config not in CONFIGS:
        raise ValueError(f"a run's configuration is one of {', '.join(CONFIGS)}, not {config}")
    if kind.fault is FaultId.F10 and config == "G1":
        return [], "it changes status answers, and nothing in G1 asks for one (0008)"
    if kind.action is Action.CHANGE_QUOTE and not any(
        intent.operation is Operation.SUBMIT_PURCHASE_ORDER for intent in scenario.intents
    ):
        return [], "it changes the quote an order cites, and no task intent orders from a quote"
    found: list[PlannedFault] = []
    why: list[str] = []
    variants: tuple[str | None, ...] = tuple(sorted(kind.variants)) or (None,)
    for variant in variants:
        points = kind.variants[variant] if variant is not None else None
        for point in sorted(points if points is not None else kind.points):
            if not point_exists(kind, point, gateway=config != "B0"):
                continue
            for match in _targets(kind, point, scenario):
                at = Trigger(point=point, match=match)
                choice = PlannedFault(fault=kind.fault, variant=variant, at=at)
                least = {name: low for name, (low, _) in _ranges(choice, scenario.params).items()}
                choice = choice.model_copy(update={"params": least})
                wrong = _misfits([choice], params=scenario.params, contract=contract)
                if wrong:
                    why.extend(wrong)
                else:
                    found.append(choice)
    return found, "; ".join(dict.fromkeys(why)) or "no trigger of it fits the run"


def _targets(kind: FaultKind, point: str, scenario: Scenario) -> list[dict[str, str | int]]:
    """The matches `kind` can take at `point`: one for each task intent it can act on,
    naming the intent and its call at the point; for F9, the first worker's steps."""
    if point == "agent.step":
        return [{"worker": FIRST_WORKER}]
    matches: list[dict[str, str | int]] = []
    for intent in scenario.intents:
        operation = intent.operation
        call: dict[str, str | int]
        if kind.action is Action.CHANGE_QUOTE and operation is not Operation.SUBMIT_PURCHASE_ORDER:
            continue
        if point.startswith("agent."):
            call = {"tool": str(operation)}
        elif point == "sim.status.answer":
            call = {"api": STATUS_CALLS[operation]}
        elif point == "net.agent_sim.answer" and kind.action in (
            Action.REVOKE,
            Action.CHANGE_QUOTE,
        ):
            # B0's early F6 and F7, as the approval reaches the agent.
            if not _approved_at_once(intent, scenario.authority):
                continue
            call = {"api": APPROVAL_REQUEST}
        elif point.endswith(".answer"):
            call = {"api": str(operation), "answer": "committed"}
        else:
            call = {"api": str(operation)}
        matches.append({**call, "intent_id": intent.id})
    return matches


def _approved_at_once(intent: Intent, authority: Authority) -> bool:
    """Whether policy approves every acceptable write of `intent` as its request arrives
    (0002, rule 6): a reservation, or a purchase order from any of its suppliers at its
    highest acceptable price. B0 then finds the approval in the answer to its request."""
    accept = intent.accept
    if not isinstance(accept, PurchaseOrderAccept):
        return True
    amount = accept.quantity * accept.max_unit_price_minor
    return all(
        authority.policy_decision(
            intent.operation, {"supplier_id": supplier_id, "max_amount_minor": amount}
        )
        is PolicyDecision.APPROVE
        for supplier_id in accept.suppliers
    )


def _ranges(choice: PlannedFault, params: Params) -> dict[str, tuple[int, int]]:
    """The lowest and highest value a random plan draws for each parameter of `choice`,
    from the run's timing for F5. F4's `copy_delay_ms` is `seed_parameters`'s to draw, as
    for any plan."""
    kind, variant = choice.kind, choice.variant
    if kind.action is Action.KILL:
        gateway = choice.at.point.startswith("gw.")
        longest = GATEWAY_RESTART_MAX_MS if gateway else AGENT_RESTART_MAX_MS
        return {"restart_delay_ms": (kind.minimums["restart_delay_ms"], longest)}
    if kind.fault is FaultId.F5:
        t_ms, d_ms = params.dispatch_timeout_ms, params.commit_deadline_ms
        if variant == F5_BEYOND_DEADLINE:
            return {"delay_ms": (d_ms + 1, 2 * d_ms)}
        return {"delay_ms": (t_ms + 1, d_ms - 1)}
    if kind.fault is FaultId.F7 and variant == "price_up":
        return {"percent": (kind.minimums["percent"], PERCENT_MAX)}
    if kind.fault is FaultId.F10:
        return {"count": (kind.minimums["count"], kind.params["count"])}
    return {}


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
