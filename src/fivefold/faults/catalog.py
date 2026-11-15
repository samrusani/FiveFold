"""The fault catalog and the named fault points (0006).

Code calls a fault hook at each named point, and the harness decides whether a fault
fires there. The points and the faults change only by decision record. `BUILT` lists the
faults this build can inject; a plan naming any other fault is refused, so no test can
believe a fault fired when it did not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
    """Stop the component at the point; it restarts after `restart_delay_ms`: the gateway
    through startup recovery (F1), an agent worker from its last checkpoint (F1's B0
    equivalent, F11)."""
    REVOKE = "revoke"
    """Revoke the approval the action or the message cites (F6)."""
    CHANGE_QUOTE = "change_quote"
    """Change the quote the action or the message cites (F7): its price, or only its
    lead time in the `harmless` variant."""


@dataclass(frozen=True)
class FaultKind:
    """One catalog entry: where the fault may fire and what it does there."""

    fault: FaultId
    title: str
    points: frozenset[str]
    action: Action
    params: Mapping[str, int]
    """Parameters and their defaults."""
    variants: Mapping[str, frozenset[str] | None] = field(default_factory=dict)
    """The fault's variants (0006), each with the points it fires at, or None for any of
    the fault's points. A fault with no variants takes none."""
    minimums: Mapping[str, int] = field(default_factory=dict)
    """The smallest value each parameter may take."""


EARLY_POINTS = frozenset({"gw.dispatch.before_check", "net.agent_sim.answer"})
"""F6 and F7 early: at the gateway's check, or as B0's approval answer reaches it."""

LATE_POINTS = frozenset({"net.gw_sim.request", "net.agent_sim.request"})
"""F6 and F7 late: as the write leaves for the simulator."""

AUTHORITY_POINTS = EARLY_POINTS | LATE_POINTS
"""Where F6 and F7 fire (0006): early, at the gateway's check or as B0's approval answer
reaches it; late, as the write request leaves for the simulator."""

BUILT: Mapping[FaultId, FaultKind] = {
    FaultId.F1: FaultKind(
        FaultId.F1,
        "Gateway crash before dispatch",
        frozenset(
            {"gw.dispatch.after_check", "gw.dispatch.after_record", "agent.before_tool_call"}
        ),
        Action.KILL,
        {"restart_delay_ms": 1_000},
        variants={
            "a": frozenset({"gw.dispatch.after_check", "agent.before_tool_call"}),
            "b": frozenset({"gw.dispatch.after_record", "agent.before_tool_call"}),
        },
        minimums={"restart_delay_ms": 0},
    ),
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
        variants={
            "after_tool_call": frozenset({"agent.after_tool_call"}),
            "after_tool_result": frozenset({"agent.after_tool_result"}),
        },
        minimums={"restart_delay_ms": 0},
    ),
    FaultId.F6: FaultKind(
        FaultId.F6,
        "Approval revoked before commit",
        AUTHORITY_POINTS,
        Action.REVOKE,
        {},
        variants={"early": EARLY_POINTS, "late": LATE_POINTS},
    ),
    FaultId.F7: FaultKind(
        FaultId.F7,
        "Quote changes after approval",
        AUTHORITY_POINTS,
        Action.CHANGE_QUOTE,
        {"percent": 5},
        variants={"price_up": None, "harmless": None},
        minimums={"percent": 1},
    ),
    FaultId.F12: FaultKind(
        FaultId.F12,
        "Request lost before it reaches the destination",
        frozenset({"net.agent_gw.request", "net.gw_sim.request", "net.agent_sim.request"}),
        Action.DROP,
        {},
    ),
}
"""The faults this build can inject, with the points 0006 allows for each."""


def point_hop(point: str) -> str | None:
    """The hop of a `net.{hop}.request` or `net.{hop}.answer` point, else None."""
    parts = point.split(".")
    if len(parts) == 3 and parts[0] == "net" and parts[1] in HOPS:
        return parts[1]
    return None
