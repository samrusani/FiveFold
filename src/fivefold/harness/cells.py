"""The expected-outcome cells of record 0006, and the fault plan for each (0006).

`faults/expected.yaml` lists every cell built so far: a fault variant, a contract level and
a configuration, with the verdict the run must get. `cell_plan` turns a cell into the plan
the run uses. A fault that acts on a message targets the purchase order's call, on the hop
the configuration uses: `gw_sim` in G modes, `agent_sim` in B0. G1 and G2 have the same
gateway points, so a G1 cell fires its fault where the G2 cell does (0006, 0008).

The variants of a cell:

- F2 has none.
- F11 names the agent's point: `after_tool_call` or `after_tool_result`.
- F6 is `early` or `late`. Early fires at the gateway's dispatch check in G modes, and in
  B0 as the approval answer reaches the agent; late fires as the order leaves for the
  simulator (0006).
- F7 names its timing and its change, as F6's timing then `price_up` or `harmless`:
  `early_price_up`, `late_price_up`, `early_harmless` or `late_harmless`.
- F8 names its timing and whether the memory correction is relevant or harmless (0023).
- F12 names the hop it drops the order on: `gw_sim` or `agent_gw` in G modes, `agent_sim`
  in B0, its B0 equivalent (0006).
- F1 is `a` or `b`: in G modes the gateway crashes after the order's dispatch check (`a`)
  or after its attempt is recorded (`b`); in B0, both kill the agent just before it sends
  the order, F1's B0 equivalent. The crashed component restarts 1,000 ms later.
- F9 is `same_args` or `different_args`: a second worker starts at the first worker's
  first step (0006).
- F3 has none: in G modes the gateway crashes after the order's answer arrives and before
  it is recorded; in B0 the agent is killed as the order's result reaches it, F3's B0
  equivalent. The crashed component restarts 1,000 ms later.
- F4 names the hop it delivers the order twice on, as F12 does. The copy follows the
  first delivery after `copy_delay_ms`, which the run draws from its seed.
- F5 is `in_contract` (the order commits 5,000 ms after it arrives: T < delay < D) or
  `beyond_deadline` (20,000 ms: twice dev-001's D, a contract violation that still lands
  inside the drain window, so the late commit happens before the run ends).
- F10 is `delayed`, `stale_within_lag` or `stale_beyond_lag`, at C1. It needs a status
  probe, so its cells pair it with F2 on the order's committed answer (0006, notes).
  `delayed` and `stale_within_lag` fire at the first status answer. The `delayed` cells
  set `count` to 5, below `max_probes` (0013), and the `stale_within_lag` cells set
  `status_lag_ms` to 3,000 (`cell_scenario`). `stale_beyond_lag` fires as the order
  reaches the simulator, and the status answers after it list the state from just before
  the order arrived (0013). B0 has no F10 cells: its scripted agent never calls the status
  tool (0008), so no status answer is produced for F10 to change. G1 has none either: its
  gateway never probes, and its agent has no status tool (0008), so nothing asks for a
  status answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario
from fivefold.paths import EXPECTED

ORDER = "submit_purchase_order"
APPROVAL = "request_approval"

TIMINGS: dict[str, dict[str, tuple[str, dict[str, str]]]] = {
    "early": {
        "G1": ("gw.dispatch.before_check", {}),
        "G2": ("gw.dispatch.before_check", {}),
        "B0": ("net.agent_sim.answer", {"api": APPROVAL}),
    },
    "late": {
        "G1": ("net.gw_sim.request", {"api": ORDER}),
        "G2": ("net.gw_sim.request", {"api": ORDER}),
        "B0": ("net.agent_sim.request", {"api": ORDER}),
    },
}
"""Where F6 and F7 fire, early and late, by configuration (0006, catalog)."""

F7_CHANGES = ("price_up", "harmless")
"""F7's changes: the price up within the intent's maximum, or the lead time only."""

F1_POINTS: dict[str, str] = {"a": "gw.dispatch.after_check", "b": "gw.dispatch.after_record"}
"""Where F1 crashes the gateway, by variant (0006, catalog)."""

F12_HOPS: dict[str, frozenset[str]] = {
    "G1": frozenset({"gw_sim", "agent_gw"}),
    "G2": frozenset({"gw_sim", "agent_gw"}),
    "B0": frozenset({"agent_sim"}),
}
"""The hops F12 can drop the order on, by configuration. F4 delivers it twice on the same."""

ORDER_STATUS = "get_order_status"

F5_DELAYS: dict[str, dict[str, int]] = {"in_contract": {}, "beyond_deadline": {"delay_ms": 20_000}}
"""F5's parameters by variant: the catalog's 5,000 ms in contract; twice dev-001's commit
deadline D beyond it, since 0006 gives that variant no default."""

DELAYED_COUNT = 5
"""The `count` of the `delayed` cells (0013): below C1's `max_probes` of 20, so the first
five probes go unanswered and the sixth is answered in time. The catalog's default stays
20, which `stale_beyond_lag` needs to last past the settle time."""

F10_PLANS: dict[str, tuple[str, str, dict[str, int]]] = {
    "delayed": ("sim.status.answer", ORDER_STATUS, {"count": DELAYED_COUNT}),
    "stale_within_lag": ("sim.status.answer", ORDER_STATUS, {}),
    "stale_beyond_lag": ("sim.write.received", ORDER, {}),
}
"""Where each F10 cell fires, on which call, and the parameters it sets (0006, 0013):
the first status answer, or, for `stale_beyond_lag`, the order's arrival at the
simulator."""

STALE_WITHIN_LAG_MS = 3_000
"""The status lag L of the `stale_within_lag` cells (0006, notes): dev-001's L is 0."""


class Cell(BaseModel):
    """One cell and its expected outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fault: str
    variant: str | None
    contract: Contract
    config: str
    verdict: str
    failing: tuple[int, ...]

    @property
    def name(self) -> str:
        """A short id such as `F11-after_tool_call-C2-B0`."""
        parts = [self.fault, self.variant, str(self.contract), self.config]
        return "-".join(part for part in parts if part)


class Expected(BaseModel):
    """`faults/expected.yaml`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: str
    seed: int
    evaluated: tuple[int, ...]
    cells: tuple[Cell, ...]


def load_expected(path: Path = EXPECTED) -> Expected:
    """The expected outcomes, as committed."""
    return Expected.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def cell_plan(cell: Cell) -> list[dict[str, Any]]:
    """The fault plan that puts a run in this cell."""
    # The durable baselines use B0's direct fault points (0018, 0020).
    config = "B0" if cell.config in ("B1", "B2") else cell.config
    hop = "agent_sim" if config == "B0" else "gw_sim"
    if cell.fault == "F2":
        return [
            {
                "fault": "F2",
                "at": {
                    "point": f"net.{hop}.answer",
                    "match": {"api": ORDER, "answer": "committed"},
                    "occurrence": 1,
                },
            }
        ]
    if cell.fault == "F11":
        return [
            {
                "fault": "F11",
                "variant": cell.variant,
                "at": {
                    "point": f"agent.{cell.variant}",
                    "match": {"tool": ORDER},
                    "occurrence": 1,
                },
            }
        ]
    if cell.fault == "F6":
        point, match = _timing(cell, config, str(cell.variant))
        return [
            {
                "fault": "F6",
                "variant": cell.variant,
                "at": {"point": point, "match": match, "occurrence": 1},
            }
        ]
    if cell.fault == "F7":
        timing, _, change = str(cell.variant).partition("_")
        if change not in F7_CHANGES:
            raise ValueError(f"F7 has no variant {cell.variant!r}")
        point, match = _timing(cell, config, timing)
        return [
            {
                "fault": "F7",
                "variant": change,
                "at": {"point": point, "match": match, "occurrence": 1},
            }
        ]
    if cell.fault == "F8":
        timing, _, _ = str(cell.variant).partition("_")
        point, match = _timing(cell, config, timing)
        return [
            {
                "fault": "F8",
                "variant": cell.variant,
                "at": {"point": point, "match": match, "occurrence": 1},
            }
        ]
    if cell.fault == "F12":
        if cell.variant not in F12_HOPS.get(config, frozenset()):
            raise ValueError(f"F12 cannot drop the order on {cell.variant!r} in {cell.config}")
        return [
            {
                "fault": "F12",
                "at": {
                    "point": f"net.{cell.variant}.request",
                    "match": {"api": ORDER},
                    "occurrence": 1,
                },
            }
        ]
    if cell.fault == "F1":
        if cell.variant not in F1_POINTS:
            raise ValueError(f"F1 has no variant {cell.variant!r}")
        if config == "B0":
            point, match = "agent.before_tool_call", {"tool": ORDER}
        else:
            point, match = F1_POINTS[cell.variant], {"api": ORDER}
        return [
            {
                "fault": "F1",
                "variant": cell.variant,
                "at": {"point": point, "match": match, "occurrence": 1},
            }
        ]
    if cell.fault == "F3":
        if config == "B0":
            point, match = "agent.after_tool_result", {"tool": ORDER}
        else:
            point, match = "gw.dispatch.after_answer", {"api": ORDER}
        return [{"fault": "F3", "at": {"point": point, "match": match, "occurrence": 1}}]
    if cell.fault == "F9":
        if cell.variant not in ("same_args", "different_args"):
            raise ValueError(f"F9 has no variant {cell.variant!r}")
        return [
            {
                "fault": "F9",
                "variant": cell.variant,
                "at": {"point": "agent.step", "match": {"worker": "agent-1"}, "occurrence": 1},
            }
        ]
    if cell.fault == "F4":
        if cell.variant not in F12_HOPS.get(config, frozenset()):
            raise ValueError(f"F4 cannot copy the order on {cell.variant!r} in {cell.config}")
        return [
            {
                "fault": "F4",
                "at": {
                    "point": f"net.{cell.variant}.request",
                    "match": {"api": ORDER},
                    "occurrence": 1,
                },
            }
        ]
    if cell.fault == "F5":
        params = F5_DELAYS.get(str(cell.variant))
        if params is None:
            raise ValueError(f"F5 has no variant {cell.variant!r}")
        return [
            {
                "fault": "F5",
                "variant": cell.variant,
                "at": {"point": "sim.write.received", "match": {"api": ORDER}, "occurrence": 1},
                "params": dict(params),
            }
        ]
    if cell.fault == "F10":
        plan = F10_PLANS.get(str(cell.variant))
        if plan is None:
            raise ValueError(f"F10 has no variant {cell.variant!r}")
        if config == "B0":
            raise ValueError(
                "B0's scripted agent never calls the status tool, so F10 has no answer to change"
            )
        if config == "G1":
            raise ValueError(
                "G1 never asks for the destination's status (0008), so F10 has no answer to change"
            )
        point, api, params = plan
        lost_reply = cell_plan(cell.model_copy(update={"fault": "F2", "variant": None}))
        status: dict[str, Any] = {
            "fault": "F10",
            "variant": cell.variant,
            "at": {"point": point, "match": {"api": api}, "occurrence": 1},
        }
        if params:
            status["params"] = dict(params)
        return [*lost_reply, status]
    raise ValueError(f"no plan for {cell.fault} yet")


def cell_scenario(cell: Cell, scenario: Scenario) -> Scenario:
    """The scenario a cell runs: `scenario` itself, except where 0006 sets a parameter.

    The `stale_within_lag` cells of F10 set `status_lag_ms` to 3,000 (0006, notes).
    """
    if cell.fault == "F10" and cell.variant == "stale_within_lag":
        params = scenario.params.model_copy(update={"status_lag_ms": STALE_WITHIN_LAG_MS})
        return scenario.model_copy(update={"params": params})
    return scenario


def _timing(cell: Cell, config: str, timing: str) -> tuple[str, dict[str, str]]:
    """Where an F6, F7 or F8 cell fires: its point and match."""
    points = TIMINGS.get(timing)
    if points is None or config not in points:
        raise ValueError(f"{cell.fault} cannot fire {timing!r} in {cell.config}")
    point, match = points[config]
    return point, dict(match)
