"""Exact reads and versioned corrections of remembered facts (0023)."""

from collections.abc import Mapping
from typing import Protocol

from fivefold.domain.api import FactRead, Refusal


class Memory(Protocol):
    """A versioned memory backend for exact keys in the FiveFold fact model."""

    def read(self, key: str, *, caller: str, vt: int) -> FactRead | Refusal:
        """Serve the latest version of a memory fact, or a read refusal."""
        ...

    def correct(self, key: str, changes: Mapping[str, str], *, actor: str, vt: int) -> FactRead:
        """Append a new version with the named fields changed and return it."""
        ...
