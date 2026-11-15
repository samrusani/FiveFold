"""The run's seeded generators (0009).

The run seed feeds independent named generators (latency, fault plans, data generation,
scripted variation), so adding a new consumer does not shift another's numbers. No other
module may use the global `random` module, `secrets`, `uuid4` or `os.urandom` (0007).

One thing here is not seeded: `credential`, the secret of a split run's per-run credential
(0008). A secret that the run's seed could reproduce would be no secret, so it comes from
the system's source. It decides nothing in the run: it only lets a caller prove who it is.
"""

from __future__ import annotations

import hashlib
import random
import secrets

Generator = random.Random
"""A seeded generator. Other modules name the type through this alias, never `random`."""


class Generators:
    """Independent random generators, each derived from the run seed and its name."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._made: dict[str, Generator] = {}

    @property
    def seed(self) -> int:
        """The run seed."""
        return self._seed

    def get(self, name: str) -> Generator:
        """The generator called `name`, the same one on every call in this run.

        Its seed is the first 8 bytes of SHA-256 over the run seed and the name, so two
        names never share a stream and a new name never shifts an existing one.
        """
        if not name:
            raise ValueError("a generator needs a name")
        made = self._made.get(name)
        if made is None:
            digest = hashlib.sha256(f"{self._seed}\x00{name}".encode()).digest()
            made = random.Random(int.from_bytes(digest[:8], "big"))
            self._made[name] = made
        return made


CREDENTIAL_BYTES = 32
"""The random bytes in each credential's secret: 256 bits."""


def credential() -> str:
    """A new secret for a split run's credential (0008), as URL-safe text, from the system's
    random source. Never seeded: see the module's docstring."""
    return secrets.token_urlsafe(CREDENTIAL_BYTES)
