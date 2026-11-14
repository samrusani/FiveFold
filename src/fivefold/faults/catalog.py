"""The fault catalog and the named fault points (0006).

Code calls a fault hook at each named point, and the harness decides whether a fault
fires there. The points and the faults change only by decision record. `BUILT` lists the
faults this build can inject; a plan naming any other fault is refused, so no test can
believe a fault fired when it did not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

HOPS = ("agent_gw", "gw_sim", "agent_sim")
"""The transport's hops: agent to gateway and gateway to simulator in G modes, agent to
simulator in baselines (0006)."""

POINTS = frozenset(
    {
        "gw.dispatch.before_check",
        "gw.dispatch.after_check",
        "gw.dispatch.after_record",
        "gw.dispatch.after_answer",
        *(f"net.{hop}.request" for hop in HOPS),
        *(f"net.{hop}.answer" for hop in HOPS),
        "sim.write.received",
        "sim.status.answer",
        "agent.before_tool_call",
        "agent.after_tool_call",
        "agent.after_tool_result",
        "agent.step",
    }
)
"""Every named fault point (0006, Fault points)."""


class FaultId(StrEnum):
    """The faults in the catalog (0006). F8 is deferred to v0.2."""

    F1 = "F1"
    F2 = "F2"
    F3 = "F3"
    F4 = "F4"
    F5 = "F5"
    F6 = "F6"
    F7 = "F7"
    F8 = "F8"
    F9 = "F9"
    F10 = "F10"
    F11 = "F11"
    F12 = "F12"


class Action(StrEnum):
    """What a fault does where it fires."""

    DROP = "drop"
    """Drop the message at the point (F2 drops an answer, F12 a request)."""
    KILL = "kill"
    """Stop the component at the point; it restarts after `restart_delay_ms` (F11)."""


@dataclass(frozen=True)
class FaultKind:
    """One catalog entry: where the fault may fire and what it does there."""

    fault: FaultId
    title: str
    points: frozenset[str]
    action: Action
    params: Mapping[str, int]
    """Parameters and their defaults."""


BUILT: Mapping[FaultId, FaultKind] = {
    FaultId.F2: FaultKind(
        FaultId.F2,
        "Reply lost after commit",
        frozenset({"net.gw_sim.answer", "net.agent_sim.answer"}),
        Action.DROP,
        {},
    ),
    FaultId.F11: FaultKind(
        FaultId.F11,
        "Agent killed and resumed from checkpoint",
        frozenset({"agent.after_tool_call", "agent.after_tool_result"}),
        Action.KILL,
        {"restart_delay_ms": 1_000},
    ),
}
"""The faults this build can inject, with the points 0006 allows for each."""


def point_hop(point: str) -> str | None:
    """The hop of a `net.{hop}.request` or `net.{hop}.answer` point, else None."""
    parts = point.split(".")
    if len(parts) == 3 and parts[0] == "net" and parts[1] in HOPS:
        return parts[1]
    return None
