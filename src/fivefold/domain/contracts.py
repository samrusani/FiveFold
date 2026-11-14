"""Tool contract levels and the promises a destination makes (0005).

A contract level is a preset of three independent capabilities. The levels are not
cumulative: C2 has idempotency keys but no status lookup. Run configs may combine
capabilities for experiments, but the gateway's recovery is defined only for the four
presets, so only the presets exist here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    """What a destination can do for a caller after a lost reply (0005)."""

    STATUS = "status"
    """Answers which effects exist for an intent, accurate to a stated `as_of_vt`."""

    IDEMPOTENCY_KEY = "idempotency_key"
    """Stores the key with each effect; a repeated key gets the stored result."""

    CONDITIONAL_WRITE = "conditional_write"
    """Checks the request's basis and approval inside the commit transaction."""


class Contract(StrEnum):
    """The four contract presets of the published matrix (0005)."""

    C0 = "C0"
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"

    @property
    def capabilities(self) -> frozenset[Capability]:
        """The capabilities this preset offers."""
        return _CAPABILITIES[self]

    def has(self, capability: Capability) -> bool:
        """Whether this preset offers `capability`."""
        return capability in _CAPABILITIES[self]


_CAPABILITIES: dict[Contract, frozenset[Capability]] = {
    Contract.C0: frozenset(),
    Contract.C1: frozenset({Capability.STATUS}),
    Contract.C2: frozenset({Capability.IDEMPOTENCY_KEY}),
    Contract.C3: frozenset({Capability.IDEMPOTENCY_KEY, Capability.CONDITIONAL_WRITE}),
}


@dataclass(frozen=True)
class ContractParams:
    """The timing promises every destination publishes, in virtual milliseconds (0005).

    The simulator keeps these promises unless a fault breaks one. Scenarios may override
    the defaults in their `params` block (0009).
    """

    max_hop_latency_ms: int = 50
    """Every message arrives within this time on one hop, or it counts as dropped."""

    commit_deadline_ms: int = 10_000
    """A request not committed within this time of its arrival never commits (D)."""

    status_lag_ms: int = 0
    """A status answer is accurate to at most this long before it is produced (L)."""

    def __post_init__(self) -> None:
        if self.max_hop_latency_ms <= 0 or self.commit_deadline_ms <= 0:
            raise ValueError("max_hop_latency_ms and commit_deadline_ms must be positive")
        if self.status_lag_ms < 0:
            raise ValueError("status_lag_ms must not be negative")

    @property
    def settle_ms(self) -> int:
        """S: a request sent at `s` has committed or never will by `s + S` (0004, 0005)."""
        return self.max_hop_latency_ms + self.commit_deadline_ms
