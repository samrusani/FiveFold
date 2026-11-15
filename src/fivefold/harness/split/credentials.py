"""A split run's credentials, made for that run alone (0008).

The run's start makes a secret for each caller the services must tell apart: the gateway,
the agent worker `agent-1` and the harness, which ends the run. A caller proves who it is
by sending its secret as a bearer token. The services keep only each secret's SHA-256
digest (`Callers`), and compare digests in constant time.

Who may use the simulator's API is the simulator's own rule: the run's credentials name
the gateway in G1 and G2 and the agent worker in B0 (`sim.run.credentials`, 0008). So the
agent's identity exists in every configuration, and in G1 and G2 the simulator refuses it,
reads and writes alike. The agent's secret is then kept in the run's directory and never
given to the agent: in G modes the agent holds no simulator credential (0008).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass

from fivefold.harness.rng import credential

GATEWAY = "gateway"
AGENT = "agent-1"
HARNESS = "harness"

HOLDERS = (GATEWAY, AGENT, HARNESS)
"""Every caller of a split run that has a secret."""


def issue() -> dict[str, str]:
    """A new secret for each holder, from the system's random source."""
    return {holder: credential() for holder in HOLDERS}


def digest(secret: str) -> str:
    """A secret's SHA-256, in hex: what the services keep instead of the secret."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def bearer(authorization: str | None) -> str | None:
    """The token of an `Authorization: Bearer <token>` header, or None."""
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


@dataclass(frozen=True)
class Callers:
    """The run's holders, by the digest of each one's secret."""

    digests: Mapping[str, str]
    """Holder name to the SHA-256 of its secret, in hex."""

    @classmethod
    def of(cls, secrets: Mapping[str, str]) -> Callers:
        """The callers whose secrets are `secrets`, holder name to secret."""
        return cls({holder: digest(secret) for holder, secret in secrets.items()})

    def identify(self, authorization: str | None) -> str | None:
        """The holder whose secret an `Authorization` header carries, or None."""
        token = bearer(authorization)
        if token is None:
            return None
        presented = digest(token)
        found: str | None = None
        for holder, known in sorted(self.digests.items()):
            if hmac.compare_digest(presented, known):
                found = holder
        return found
