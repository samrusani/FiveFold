"""The split topology's networks in the Compose file (records 0008 and 0009).

Record 0008: in the split topology, the agent's container is only on a network it shares
with the gateway, and it holds no simulator credential. These tests read
`docker-compose.yml` as YAML (merge keys included) and check which networks and volumes
the agent's service and the others join. CI also validates the file with
`docker compose config`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
AGENT_NETWORK = "agent"


def services() -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    found: dict[str, dict[str, Any]] = data["services"]
    return found


def networks(service: dict[str, Any]) -> set[str]:
    """The networks a service joins: Compose puts a service that names none on
    `default`, and takes the names as a list or as the keys of a mapping."""
    named = service.get("networks", ["default"])
    return set(named)


def test_the_agent_is_only_on_the_agents_network() -> None:
    assert networks(services()["agent"]) == {AGENT_NETWORK}


def test_only_the_gateway_shares_the_agents_network() -> None:
    on_it = {name for name, service in services().items() if AGENT_NETWORK in networks(service)}
    assert on_it == {"agent", "gateway"}


def test_the_agent_mounts_no_run_directory_and_knows_no_simulator() -> None:
    """The run directory holds the gateway's and the harness's tokens, so the agent never
    mounts it; and nothing tells the agent where the simulator or PostgreSQL is."""
    agent = services()["agent"]
    assert "volumes" not in agent
    # Compose takes `environment` as a mapping or as a list of NAME=value strings.
    named = set(agent.get("environment", {}))
    assert not any("SIMULATOR" in name or "DSN" in name for name in named)


def test_the_simulator_and_postgres_are_on_the_backend_only_with_their_peers() -> None:
    found = services()
    assert AGENT_NETWORK not in networks(found["simulator"])
    assert AGENT_NETWORK not in networks(found["postgres"])
    assert "backend" in networks(found["simulator"])
    assert "backend" in networks(found["gateway"])
