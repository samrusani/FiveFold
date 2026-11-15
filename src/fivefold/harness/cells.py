"""The expected-outcome cells of record 0006, and the fault plan for each (0006).

`faults/expected.yaml` lists every cell built so far: a fault variant, a contract level and
a configuration, with the verdict the run must get. `cell_plan` turns a cell into the plan
the run uses. A fault that acts on a message targets the purchase order's call, on the hop
the configuration uses: `gw_sim` in G modes, `agent_sim` in B0.
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
    raise ValueError(f"no plan for {cell.fault} yet")
