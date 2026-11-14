"""Shared test configuration: Hypothesis profiles and PostgreSQL fixtures (0007).

Select a profile with HYPOTHESIS_PROFILE. CI uses `ci`, which derandomizes Hypothesis so
results are reproducible. The nightly job uses `nightly`, which explores far more examples.

Tests that need PostgreSQL carry the marker `pg`. FIVEFOLD_ADMIN_DSN must point at a
PostgreSQL 16 server, connecting as a superuser. The default is the local Docker Compose
database (`make db-up`).

Every database the tests create starts with FIVEFOLD_TEST_DB_PREFIX (default
`fivefold_test`). Test runs that share one server at the same time need different
prefixes, or they drop each other's databases.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from hypothesis import settings
from psycopg.conninfo import make_conninfo

from fivefold.domain.contracts import Contract
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.pg_store import PgStore
from fivefold.gateway.state_machine import GatewaySettings
from fivefold.gateway.twin import TwinStore
from fivefold.harness.migrations import (
    DEFAULT_ADMIN_DSN,
    bootstrap,
    drop_database,
    migrate,
    recreate_database,
)

settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
settings.register_profile("nightly", max_examples=2_000, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

DB_DIR = Path(__file__).resolve().parents[1] / "db"
DB_PREFIX = os.environ.get("FIVEFOLD_TEST_DB_PREFIX", "fivefold_test")
if not re.fullmatch(r"[a-z_][a-z0-9_]{0,39}", DB_PREFIX):
    raise ValueError("FIVEFOLD_TEST_DB_PREFIX must be at most 40 lowercase letters, digits or _")


@pytest.fixture(scope="session")
def db_prefix() -> str:
    """The prefix of every database this test run creates."""
    return DB_PREFIX


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    """The admin connection string, after the component roles are in place."""
    dsn = os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_ADMIN_DSN)
    with psycopg.connect(dsn, autocommit=True) as conn:
        bootstrap(conn, DB_DIR / "bootstrap.sql")
    return dsn


@pytest.fixture
def empty_database(admin_dsn: str) -> Iterator[str]:
    """A fresh, empty database for one test, dropped afterwards. Yields its DSN."""
    dsn = recreate_database(admin_dsn, DB_PREFIX)
    yield dsn
    drop_database(admin_dsn, DB_PREFIX)


@pytest.fixture
def migrated_database(empty_database: str) -> str:
    """A fresh database with every migration applied. Returns its DSN."""
    with psycopg.connect(empty_database, autocommit=True) as conn:
        migrate(conn, DB_DIR / "migrations")
    return empty_database


@dataclass(frozen=True)
class GatewayDatabase:
    """A migrated database for gateway stores, shared by the session and reset per use."""

    admin: str
    gateway: str

    def reset(self) -> None:
        """Empty every gw table, as a fresh run database is. Needs the admin connection,
        because the log refuses TRUNCATE until its triggers are switched off."""
        with psycopg.connect(self.admin, autocommit=True) as conn, conn.transaction():
            conn.execute("ALTER TABLE gw.action_event DISABLE TRIGGER USER")
            conn.execute("ALTER TABLE gw.run_state DISABLE TRIGGER USER")
            conn.execute(
                "TRUNCATE gw.report, gw.tool_call, gw.action_event, gw.escalation, gw.attempt,"
                ' gw."grant", gw.action, gw.intent'
            )
            conn.execute("ALTER TABLE gw.action_event ENABLE TRIGGER USER")
            conn.execute(
                "UPDATE gw.run_state SET run_closed = false, last_action_no = 0, last_seq = 0,"
                " last_call_no = 0"
            )
            conn.execute("ALTER TABLE gw.run_state ENABLE TRIGGER USER")


@pytest.fixture(scope="session")
def gateway_database(admin_dsn: str) -> Iterator[GatewayDatabase]:
    """One migrated run database for the session's gateway stores, dropped at the end."""
    name = f"{DB_PREFIX}_gateway"
    dsn = recreate_database(admin_dsn, name)
    with psycopg.connect(dsn, autocommit=True) as conn:
        migrate(conn, DB_DIR / "migrations")
    yield GatewayDatabase(admin=dsn, gateway=make_conninfo(dsn, user="ff_gw"))
    drop_database(admin_dsn, name)


StoreFactory = Callable[..., MemoryStore | TwinStore]


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.pg)])
def make_store(request: pytest.FixtureRequest) -> Iterator[StoreFactory]:
    """Builds the store a test runs against: in memory, or Postgres checked against it.

    With "postgres", every operation runs on both stores through `TwinStore`, which
    requires equal results and equal records (0003, testing plan, item 3). One store per
    test: the database is reset each time one is built.
    """
    database: GatewayDatabase | None = None
    if request.param == "postgres":
        database = request.getfixturevalue("gateway_database")
    opened: list[PgStore] = []

    def factory(
        *,
        task_id: str,
        intents: Mapping[str, str],
        contract: Contract,
        settings: GatewaySettings,
        identity: bool = True,
    ) -> MemoryStore | TwinStore:
        oracle = MemoryStore(
            task_id=task_id,
            intents=intents,
            contract=contract,
            settings=settings,
            identity=identity,
        )
        if database is None:
            return oracle
        database.reset()
        pg = PgStore(
            database.gateway,
            task_id=task_id,
            intents=intents,
            contract=contract,
            settings=settings,
            identity=identity,
        )
        opened.append(pg)
        return TwinStore(oracle, pg)

    yield factory
    for store in opened:
        store.close()
