"""Which faults the split topology can run (0006, 0009).

In the split topology the agent is the user's own, and the gateway and the simulator are
separate services. Faults still fire at their points, on the plan's counters (0009), but
only where a service of the split topology can act:

| Point | Fires in | As | Faults |
| --- | --- | --- | --- |
| `net.agent_gw.request` | the gateway | the agent's MCP message arrives | F4, F12 |
| `net.gw_sim.request` | the simulator | the gateway's request arrives | F4, F6/F7 late, F12 |
| `net.gw_sim.answer` | the gateway | the simulator's answer arrives | F2 |
| `net.agent_sim.request` | the simulator | the agent's request arrives | F4, F6/F7 late, F12 |
| `net.agent_sim.answer` | the simulator | it answers the agent | F2, F6/F7 early |
| `sim.write.received` | the simulator | a write reaches the destination | F5 |
| `sim.status.answer` | the simulator | a status answer is produced | F10 |

The rest cannot run here, and a plan that names one is refused before the run starts:

- the agent's points (`agent.*`: F1's and F3's B0 equivalents, F9 and F11), because the
  harness has no hold on the user's agent;
- the gateway's dispatch points (`gw.dispatch.*`): F1 and F3 crash the gateway, which here
  would mean its process exits and restarts, and F6 and F7 early would need the
  simulator's control interface inside the gateway's process.

Each point fires in one service only, so each plan entry counts its events in one place.
A split run also has no scripted resolver: a human resolves an escalation through the
gateway's admin API.

A split run takes a fixed plan only, and refuses a random one (0006, Plans) before it
starts. A random plan draws faults at every point of the configuration, F1, F3, F9, F11
and, in G1 and G2, F6 and F7 early among them, which the split topology cannot fire. It
serves the Hypothesis suites, whose runs only the inproc topology reproduces from a seed.
"""

from __future__ import annotations

from collections.abc import Sequence

from fivefold.domain.scenario import Scenario
from fivefold.faults.plan import PlannedFault

SPLIT_POINTS = (
    "net.agent_gw.request",
    "net.gw_sim.request",
    "net.gw_sim.answer",
    "net.agent_sim.request",
    "net.agent_sim.answer",
    "sim.write.received",
    "sim.status.answer",
)
"""The points where the split topology fires faults (see the module's table)."""


class SplitRefused(ValueError):
    """The split topology cannot run this plan or scenario."""


def check_split(plan: Sequence[PlannedFault], scenario: Scenario) -> None:
    """Refuse, in one line, a plan with a fault the split topology cannot run, or a
    scenario with a scripted resolver."""
    wrong = [
        f"{planned.fault} at {planned.at.point}"
        for planned in plan
        if planned.at.point not in SPLIT_POINTS
    ]
    if wrong:
        raise SplitRefused(
            f"the split topology cannot run {', '.join(wrong)}: it fires faults on the"
            " network and in the simulator, never inside the gateway or your agent (0009)"
        )
    if scenario.authority.resolver != "none":
        raise SplitRefused(
            "the split topology has no scripted resolver: set the scenario's resolver to"
            " none, and resolve escalations through the gateway's admin API"
        )
