"""Database bootstrap and migrations (0007, 0008).

`db/bootstrap.sql` creates the component roles. Roles belong to the whole PostgreSQL
server, so the script runs once per server, as a superuser, and is safe to run again.

`db/migrations/NNNN_name.sql` holds the schema changes. The runner applies them in number
order, each file in its own transaction, and records each file's name and SHA-256 in
`public.schema_migration`. It refuses to continue if a file that was already applied has
changed or disappeared. Migrations only go forward: run databases are disposable (0009).

Run every migration as the same role. The default privileges in `0001_schemas.sql` apply
to objects created later by the role that ran it.

Command line, from the repository root:

    python -m fivefold.harness.migrations bootstrap
    python -m fivefold.harness.migrations migrate --database fivefold_dev
    python -m fivefold.harness.migrations reset --database fivefold_dev
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow

DEFAULT_ADMIN_DSN = "postgresql://postgres@127.0.0.1:5432/postgres"
"""Used when FIVEFOLD_ADMIN_DSN is unset: the local Docker Compose database."""

MIGRATION_FILE = re.compile(r"^(?P<number>\d{4})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")
"""A migration file name: four digits, an underscore, then lowercase words joined by underscores."""

ADVISORY_LOCK_KEY = 5_000_005
"""Session advisory lock held while migrating, so two runners never interleave."""

Connection = psycopg.Connection[TupleRow]


class MigrationError(Exception):
    """The migration files or the database are in a state the runner refuses to touch."""


@dataclass(frozen=True)
class Migration:
    """One migration file."""

    number: int
    name: str
    path: Path
    sha256: str


def discover(directory: Path) -> list[Migration]:
    """Return the migrations in `directory` in number order.

    Every `.sql` file must be named `NNNN_name.sql`, and the numbers must run 1, 2, 3 and
    so on, with no gap and no duplicate.
    """
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_FILE.match(path.name)
        if match is None:
            raise MigrationError(f"bad migration file name: {path.name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        migrations.append(Migration(int(match["number"]), path.name, path, digest))
    migrations.sort(key=lambda migration: migration.number)
    numbers = [migration.number for migration in migrations]
    if numbers != list(range(1, len(numbers) + 1)):
        raise MigrationError(f"migration numbers must run 1, 2, 3 with no gaps: {numbers}")
    return migrations


def bootstrap(conn: Connection, script: Path) -> None:
    """Run the cluster-level bootstrap script in one transaction."""
    _require_autocommit(conn)
    with conn.transaction():
        conn.execute(script.read_bytes())


def migrate(conn: Connection, directory: Path) -> list[str]:
    """Apply every pending migration in `directory`, and return the names applied.

    The database must use UTF-8; otherwise text comes back as raw bytes.
    """
    _require_autocommit(conn)
    row = conn.execute("SHOW server_encoding").fetchone()
    encoding = row[0] if row is not None else None
    if isinstance(encoding, bytes):
        encoding = encoding.decode("ascii", errors="replace")
    if encoding != "UTF8":
        raise MigrationError(f"the database must use UTF8 encoding, not {encoding}")
    migrations = discover(directory)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_migration ("
        " filename text PRIMARY KEY,"
        " sha256 text NOT NULL,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    )
    conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    try:
        rows = conn.execute("SELECT filename, sha256 FROM public.schema_migration").fetchall()
        applied = {str(row[0]): str(row[1]) for row in rows}
        missing = sorted(set(applied) - {migration.name for migration in migrations})
        if missing:
            raise MigrationError(f"applied migrations are missing from {directory}: {missing}")
        newly_applied: list[str] = []
        for migration in migrations:
            recorded = applied.get(migration.name)
            if recorded is not None:
                if recorded != migration.sha256:
                    raise MigrationError(f"{migration.name} changed after it was applied")
                continue
            with conn.transaction():
                conn.execute(migration.path.read_bytes())
                conn.execute(
                    "INSERT INTO public.schema_migration (filename, sha256) VALUES (%s, %s)",
                    (migration.name, migration.sha256),
                )
            newly_applied.append(migration.name)
        return newly_applied
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def recreate_database(admin_dsn: str, name: str) -> str:
    """Drop the database `name` if it exists, create it empty, and return its DSN.

    The database always uses UTF-8 with C collation, whatever the server's defaults. C
    collation orders text by byte value, so query results do not depend on the host's
    locale (0009).
    """
    drop_database(admin_dsn, name)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "CREATE DATABASE {} ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0"
            ).format(sql.Identifier(name))
        )
    return make_conninfo(admin_dsn, dbname=name)


def drop_database(admin_dsn: str, name: str) -> None:
    """Drop the database `name` if it exists, closing any open connections to it."""
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def _require_autocommit(conn: Connection) -> None:
    if not conn.autocommit:
        raise MigrationError("use an autocommit connection; the runner manages its transactions")


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point. Returns the process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m fivefold.harness.migrations",
        description="Create the component roles and apply database migrations.",
    )
    parser.add_argument(
        "command",
        choices=["bootstrap", "migrate", "reset"],
        help="bootstrap: create roles. migrate: apply pending migrations. "
        "reset: recreate the database, then bootstrap and migrate.",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_ADMIN_DSN),
        help="admin connection string (default: FIVEFOLD_ADMIN_DSN, or the local Compose database)",
    )
    parser.add_argument("--database", default="fivefold_dev", help="database to migrate or reset")
    parser.add_argument(
        "--db-dir",
        type=Path,
        default=Path("db"),
        help="directory holding bootstrap.sql and migrations/ (default: db)",
    )
    args = parser.parse_args(argv)
    admin_dsn: str = args.dsn
    database: str = args.database
    db_dir: Path = args.db_dir

    if args.command == "bootstrap":
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            bootstrap(conn, db_dir / "bootstrap.sql")
        print("bootstrap: roles are in place")
        return 0

    if args.command == "reset":
        target = recreate_database(admin_dsn, database)
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            bootstrap(conn, db_dir / "bootstrap.sql")
    else:
        target = make_conninfo(admin_dsn, dbname=database)
    with psycopg.connect(target, autocommit=True) as conn:
        applied = migrate(conn, db_dir / "migrations")
    print(f"{args.command}: applied {len(applied)} migration(s) to {database}: {applied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
