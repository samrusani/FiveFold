"""What the gateway asks the world: facts, approvals and the destination (0002, 0003, 0005).

The dispatcher reaches the simulator only through this protocol, so its logic can be
tested with a stand-in and the gateway never imports the simulator (0008). The real
implementation sends each request on the `gw_sim` hop.

Every method may raise `Unreachable`: no answer arrived in time, or the answer was a
server error. The dispatcher then tries again later and changes nothing. A write is the
exception: `send` always returns an `Answer`, because a timeout or a server error after
a send is itself evidence, and moves the action to OutcomeUnknown (0003).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from fivefold.gateway.records import Action, Answer, ApprovalStatus, Attempt, ProbeAnswer


class Unreachable(Exception):
    """A check's request got no usable answer in time; nothing was learned."""


@dataclass(frozen=True)
class FactNow:
    """A fact as the simulator holds it now: its key, version and value."""

    key: str
    version: int
    value: Mapping[str, object]


@dataclass(frozen=True)
class ApprovalNow:
    """An approval as the approvals service reports it now (0002)."""

    approval_id: str
    status: ApprovalStatus
    expires_at: int | None
    """When an approved approval expires; None unless it is approved."""


class Destination(Protocol):
    """The gateway's view of the simulator's API."""

    async def facts(self, keys: Sequence[str]) -> Mapping[str, FactNow]:
        """The current version and value of each fact."""

    async def request_approval(self, action: Action) -> ApprovalNow:
        """File the action's approval request, keyed by its action id, and return the
        policy's immediate answer (0002). Filing again returns the same approval."""

    async def approval(self, approval_id: str) -> ApprovalNow:
        """The approval's status now."""

    async def send(self, action: Action, attempt: Attempt) -> Answer:
        """Send one attempt of the write and classify the answer (0003). A timeout, a
        transport error or a server error gives an unknown answer."""

    async def lookup(self, action: Action) -> ProbeAnswer:
        """C1: which effects exist for the action's intent, and as of when (0005)."""
