"""How one run's destination behaves (0005, 0008).

A destination has three independent capabilities. The presets C0 to C3 of record 0005
set them, and a run config may combine them for experiments. The destination also
publishes timing promises (`ContractParams`), and gives a credential to the callers that
may use its API: the gateway in G modes, the agent workers in baselines (0008).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from fivefold.domain.contracts import Capability, Contract, ContractParams


@dataclass(frozen=True)
class SimConfig:
    """The capabilities, credentials and timing promises of one run's destination."""

    capabilities: frozenset[Capability]
    credentials: frozenset[str]
    """Callers that hold a credential for this run. Every other caller is refused."""
    params: ContractParams = field(default_factory=ContractParams)

    def __post_init__(self) -> None:
        for capability in self.capabilities:
            Capability(capability)
        if not self.credentials:
            raise ValueError("a run gives at least one caller a credential")
        for caller in self.credentials:
            if not isinstance(caller, str) or not caller or "\x00" in caller:
                raise ValueError(f"a caller is named by non-empty text: {caller!r}")

    @classmethod
    def preset(
        cls,
        contract: Contract,
        *,
        credentials: Iterable[str],
        params: ContractParams | None = None,
    ) -> SimConfig:
        """The configuration of a contract preset, C0 to C3 (0005)."""
        return cls(
            capabilities=contract.capabilities,
            credentials=frozenset(credentials),
            params=ContractParams() if params is None else params,
        )

    def has(self, capability: Capability) -> bool:
        """Whether the destination offers `capability`."""
        return capability in self.capabilities
