"""The expected-outcome cells of record 0006, and the fault plan for each (0006).

`faults/expected.yaml` lists every cell built so far: a fault variant, a contract level and
a configuration, with the verdict the run must get. `cell_plan` turns a cell into the plan
the run uses. A fault that acts on a message targets the purchase order's call, on the hop
the configuration uses: `gw_sim` in G modes, `agent_sim` in B0.

The variants of a cell:

- F2 has none.
- F11 names the agent's point: `after_tool_call` or `after_tool_result`.
- F6 is `early` or `late`. Early fires at the gateway's dispatch check in G modes, and in
  B0 as the approval answer reaches the agent; late fires as the order leaves for the
  simulator (0006).
- F7 names its timing and its change, as F6's timing then `price_up` or `harmless`:
  `early_price_up`, `late_price_up`, `early_harmless` or `late_harmless`.
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
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from fivefold.domain.contracts import Contract

ROOT = Path(__file__).resolve().parents[3]
EXPECTED = ROOT / "faults" / "expected.yaml"

ORDER = "submit_purchase_order"
APPROVAL = "request_approval"

TIMINGS: dict[str, dict[str, tuple[str, dict[str, str]]]] = {
    "early": {
        "G2": ("gw.dispatch.before_check", {}),
        "B0": ("net.agent_sim.answer", {"api": APPROVAL}),
    },
    "late": {
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
    "G2": frozenset({"gw_sim", "agent_gw"}),
    "B0": frozenset({"agent_sim"}),
}
"""The hops F12 can drop the order on, by configuration."""


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
    hop = "agent_sim" if cell.config == "B0" else "gw_sim"
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
        point, match = _timing(cell, str(cell.variant))
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
        point, match = _timing(cell, timing)
        return [
            {
                "fault": "F7",
                "variant": change,
                "at": {"point": point, "match": match, "occurrence": 1},
            }
        ]
    if cell.fault == "F12":
        if cell.variant not in F12_HOPS.get(cell.config, frozenset()):
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
        if cell.config == "B0":
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
        if cell.config == "B0":
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
    raise ValueError(f"no plan for {cell.fault} yet")


def _timing(cell: Cell, timing: str) -> tuple[str, dict[str, str]]:
    """Where an F6 or F7 cell fires: its point and match."""
    points = TIMINGS.get(timing)
    if points is None or cell.config not in points:
        raise ValueError(f"{cell.fault} cannot fire {timing!r} in {cell.config}")
    point, match = points[cell.config]
    return point, dict(match)
