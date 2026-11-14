"""Fixtures for tests that need PostgreSQL (marker `pg`).

FIVEFOLD_ADMIN_DSN must point at a PostgreSQL 16 server, connecting as a superuser. The
default is the local Docker Compose database (`make db-up`).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from fivefold.harness.migrations import (
    DEFAULT_ADMIN_DSN,
    bootstrap,
    drop_database,
    migrate,
    recreate_database,
)

DB_DIR = Path(__file__).resolve().parents[2] / "db"
TEST_DATABASE = "fivefold_test"


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
    dsn = recreate_database(admin_dsn, TEST_DATABASE)
    yield dsn
    drop_database(admin_dsn, TEST_DATABASE)


@pytest.fixture
def migrated_database(empty_database: str) -> str:
    """A fresh database with every migration applied. Returns its DSN."""
    with psycopg.connect(empty_database, autocommit=True) as conn:
        migrate(conn, DB_DIR / "migrations")
    return empty_database
