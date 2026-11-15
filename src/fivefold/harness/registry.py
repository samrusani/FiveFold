"""The run registry (0009, 0012): one row per run database, so the viewer can list runs.

The registry is a database of its own on the server that holds the run databases:
`fivefold_registry`, unless FIVEFOLD_REGISTRY_DB names another (`fivefold.harness.
migrations.registry_database`). `bootstrap_server` in that module creates it and applies
its migrations, `db/registry/`. A row holds the run database's name, the run's id, its
scenario, configuration, contract level and seed, and when the row was written, in wall
time, for display only (0009).

The harness writes a run's row when the run records itself in its run database
(`InprocRun.execute`), so every run gets one, whoever made its database: a demo, a study,
playback or a test. A database made again under an old name, as each demo's is, gets its
row replaced. `forget` removes the row of a run database that is dropped.

The harness writes as role `ff_registry`, which has no privilege on any run database's
schemas; the viewer reads as `ff_viewer` (`db/registry/0001_registry.sql`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import TupleRow

from fivefold.harness.migrations import check_database_name, registry_database

REGISTRY_ROLE: Final = "ff_registry"
"""The role the harness writes the registry as."""


class RegistryError(Exception):
    """The run registry cannot be reached, or a run's database cannot be named."""


@dataclass(frozen=True)
class RegistryEntry:
    """One run database's row: what the registry lists for the viewer (0012)."""

    database: str
    run_id: str
    scenario_id: str
    config: str
    contract: str
    seed: int


def database_of(dsn: str) -> str:
    """The name of the database `dsn` connects to. It must name one."""
    name = conninfo_to_dict(dsn).get("dbname")
    if not isinstance(name, str) or not name:
        raise RegistryError("the run's connection string names no database")
    check_database_name(name)
    return name


def register(dsn: str, entry: RegistryEntry, *, registry: str | None = None) -> None:
    """Write `entry` to the registry on the server `dsn` names, replacing the row its
    database had, as `ff_registry`.

    `registry` defaults to `registry_database()`. Raises `RegistryError` when the
    registry cannot be reached, for example because it was never created.
    """
    name = registry_database() if registry is None else registry
    check_database_name(entry.database)
    with _connect(dsn, name) as conn:
        conn.execute(
            "INSERT INTO registry.run AS r"
            " (database_name, run_id, scenario_id, config, contract, seed)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (database_name) DO UPDATE SET run_id = EXCLUDED.run_id,"
            " scenario_id = EXCLUDED.scenario_id, config = EXCLUDED.config,"
            " contract = EXCLUDED.contract, seed = EXCLUDED.seed,"
            " created_wall_at = EXCLUDED.created_wall_at",
            (
                entry.database,
                entry.run_id,
                entry.scenario_id,
                entry.config,
                entry.contract,
                entry.seed,
            ),
        )


def forget(dsn: str, database: str, *, registry: str | None = None) -> None:
    """Remove the row of `database` from the registry on the server `dsn` names, if it
    has one. For a run database that was dropped."""
    name = registry_database() if registry is None else registry
    with _connect(dsn, name) as conn:
        conn.execute("DELETE FROM registry.run WHERE database_name = %s", (database,))


def _connect(dsn: str, registry: str) -> psycopg.Connection[TupleRow]:
    try:
        return psycopg.connect(
            make_conninfo(dsn, dbname=registry, user=REGISTRY_ROLE), autocommit=True
        )
    except psycopg.OperationalError as error:
        raise RegistryError(
            f"the run registry {registry} cannot be reached ({error}); create it with"
            " `python -m fivefold.harness.migrations bootstrap`"
        ) from error
