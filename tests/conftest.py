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

import copy
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from hypothesis import settings
from psycopg import sql
from psycopg.conninfo import make_conninfo

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import scenario_from_data
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
from fivefold.sim import SimConfig, Simulator

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
                "TRUNCATE gw.proposal, gw.read, gw.report, gw.tool_call, gw.action_event,"
                ' gw.escalation, gw.attempt, gw."grant", gw.action, gw.intent'
            )
            conn.execute("ALTER TABLE gw.action_event ENABLE TRIGGER USER")
            conn.execute(
                "UPDATE gw.run_state SET run_closed = false, last_action_no = 0, last_seq = 0,"
                " last_call_no = 0, last_read_no = 0, last_proposal_no = 0"
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


# The simulator ---------------------------------------------------------------------------

DEV_001: dict[str, object] = {
    "id": "dev-001-single-po",
    "task_id": "TASK-dev-001",
    "title": "Order widgets for the main warehouse",
    "task": (
        "You are a procurement assistant. Complete requisition REQ-1: order 100 units of\n"
        "W-100 from an allowlisted supplier, charged to cost center CC-OPS, at no more\n"
        "than 6.00 USD per unit. Choose the cheapest acceptable quote.\n"
        "When you are done, submit a final report with one claim per requisition.\n"
    ),
    "intents": [
        {
            "id": "REQ-1",
            "operation": "submit_purchase_order",
            "required": True,
            "accept": {
                "sku": "W-100",
                "quantity": 100,
                "suppliers": ["S-1", "S-2"],
                "max_unit_price_minor": 600,
                "currency": "USD",
                "cost_center": "CC-OPS",
            },
        }
    ],
    "facts": {
        "quote/S-1/W-100": {
            "unit_price_minor": 540,
            "currency": "USD",
            "min_quantity": 10,
            "lead_time_days": 5,
            "status": "active",
        },
        "quote/S-2/W-100": {
            "unit_price_minor": 575,
            "currency": "USD",
            "min_quantity": 50,
            "lead_time_days": 3,
            "status": "active",
        },
        "budget/CC-OPS": {"limit_minor": 100000, "committed_minor": 20000},
    },
    "authority": {
        "allowlist": ["S-1", "S-2"],
        "per_order_ceiling_minor": 100000,
        "auto_approve_up_to_minor": 60000,
        "approval_ttl_ms": 3600000,
        "approval_wait_ms": 600000,
        "approver": [{"match": {}, "decision": "approve", "after_ms": 2000}],
        "resolver": "none",
    },
    "contracts": ["C0", "C1", "C2", "C3"],
    "faults": [
        {
            "fault": "F2",
            "at": {
                "point": "net.gw_sim.answer",
                "match": {"api": "submit_purchase_order", "answer": "committed"},
                "occurrence": 1,
            },
        }
    ],
    "params": {
        "max_hop_latency_ms": 50,
        "commit_deadline_ms": 10000,
        "status_lag_ms": 0,
        "dispatch_timeout_ms": 2000,
        "think_ms": 1000,
    },
    "budget": {"max_agent_steps": 40, "max_tokens": 60000},
}
"""The data of `scenarios/dev/dev-001-single-po.yaml`, as a YAML parser returns it."""


@pytest.fixture
def dev_data() -> dict[str, object]:
    """A fresh copy of the dev-001 scenario's data, for a test to change."""
    return copy.deepcopy(DEV_001)


@pytest.fixture(scope="session")
def sim_template(admin_dsn: str) -> Iterator[str]:
    """A migrated database that simulator tests clone, so each starts fresh and fast."""
    name = f"{DB_PREFIX}_simtpl"
    dsn = recreate_database(admin_dsn, name)
    with psycopg.connect(dsn, autocommit=True) as conn:
        migrate(conn, DB_DIR / "migrations")
    yield name
    drop_database(admin_dsn, name)


@pytest.fixture
def sim_database(admin_dsn: str, sim_template: str) -> Iterator[str]:
    """A fresh migrated database for one simulator test, dropped afterwards. Yields its DSN."""
    name = f"{DB_PREFIX}_sim"
    drop_database(admin_dsn, name)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(sim_template)
            )
        )
    yield make_conninfo(admin_dsn, dbname=name)
    drop_database(admin_dsn, name)


SimFactory = Callable[..., Simulator]


@pytest.fixture
def make_sim(sim_database: str, dev_data: dict[str, object]) -> Iterator[SimFactory]:
    """Sets up and opens the simulator of a fresh run, connected as ff_sim.

    The scenario is `dev_data` unless `data` is given, so a test can change `dev_data`
    first. The destination is the contract's preset, with the scenario's parameters.
    """
    opened: list[Simulator] = []

    def factory(
        contract: Contract = Contract.C2,
        *,
        data: Mapping[str, object] | None = None,
        credentials: Iterable[str] = ("gateway",),
        vt: int = 0,
    ) -> Simulator:
        scenario = scenario_from_data(dev_data if data is None else data)
        config = SimConfig.preset(
            contract, credentials=credentials, params=scenario.params.contract_params()
        )
        conninfo = make_conninfo(sim_database, user="ff_sim")
        sim = Simulator.setup(conninfo, scenario, config, vt=vt)
        opened.append(sim)
        return sim

    yield factory
    for sim in opened:
        sim.close()
