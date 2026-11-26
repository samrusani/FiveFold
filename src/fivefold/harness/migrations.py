"""Database bootstrap and migrations (0007, 0008, 0012).

`db/bootstrap.sql` creates the component roles. Roles belong to the whole PostgreSQL
server, so the script runs once per server, as a superuser, and is safe to run again.

`db/migrations/NNNN_name.sql` holds the schema changes. The runner applies them in number
order, each file in its own transaction, and records each file's name and SHA-256 in
`public.schema_migration`. It refuses to continue if a file that was already applied has
changed or disappeared. Migrations only go forward: run databases are disposable (0009).

Run every migration as the same role. The default privileges in `0001_schemas.sql` apply
to objects created later by the role that ran it.

The run registry (0009, 0012) is one more database on the server, `fivefold_registry`
unless FIVEFOLD_REGISTRY_DB names another. It lists the run databases for the viewer, and
has migrations of its own, in `db/registry/`, applied by the same runner.
`bootstrap_server` creates the roles and the registry, and leaves an existing registry's
rows alone. `fivefold.harness.registry` writes the rows.

Every run database this module creates or drops changes under one advisory lock
(`database_lock`), so another fivefold process on the server never sees a name between its
check and its change. `DROP DATABASE` cannot run in a transaction, so the check and the
drop are two statements, and the lock is what joins them.

Run databases are copies of one migrated template database (0007, 0009),
`fivefold_template` unless FIVEFOLD_TEMPLATE_DB names another; the tests name their own.
`create_run_database` makes the template if it is missing, migrates it forward if it is
behind, and copies it with `CREATE DATABASE ... TEMPLATE`, all under the database lock. A
copy keeps the template's rows in `public.schema_migration`, so `migrate` on it applies
nothing.

Command line, from the repository root:

    python -m fivefold.harness.migrations bootstrap
    python -m fivefold.harness.migrations migrate --database fivefold_dev
    python -m fivefold.harness.migrations reset --database fivefold_dev
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow

from fivefold.paths import DB_DIR

DEFAULT_ADMIN_DSN = "postgresql://postgres@127.0.0.1:5432/postgres"
"""Used when FIVEFOLD_ADMIN_DSN is unset: the local Docker Compose database."""

MIGRATION_FILE = re.compile(r"^(?P<number>\d{4})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")
"""A migration file name: four digits, an underscore, then lowercase words joined by underscores."""

ADVISORY_LOCK_KEY = 5_000_005
"""Session advisory lock held while migrating, so two runners never interleave."""

DATABASE_LOCK_KEY = 5_000_007
"""Session advisory lock held, in the admin database, while this module creates or drops a
run database (`database_lock`). The gateway's lock is 5_000_006."""

DATABASE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
"""Database names this runner creates or drops: lowercase ASCII, at most 63 bytes.

PostgreSQL truncates longer identifiers to 63 bytes (NAMEDATALEN - 1) without an error,
so dropping an overlong name would drop whichever database has its first 63 bytes.
"""

REGISTRY_DATABASE = "fivefold_registry"
"""The run registry's database (0012), unless FIVEFOLD_REGISTRY_DB names another."""

REGISTRY_ENV = "FIVEFOLD_REGISTRY_DB"
"""The environment variable that names another registry database. Tests set it, so a test
run never writes to the registry a person's runs are in."""

TEMPLATE_DATABASE = "fivefold_template"
"""The migrated template that run databases are copied from (0009), unless
FIVEFOLD_TEMPLATE_DB names another."""

TEMPLATE_ENV = "FIVEFOLD_TEMPLATE_DB"
"""The environment variable that names another template database. Tests set it, so a test
run never migrates, copies or drops the template a person's runs are made from."""

RUN_DATABASE_COMMENT = "FiveFold run database"
"""Shared catalog marker set on each database this module creates for a run."""

DEVELOPMENT_DATABASE = "fivefold_dev"
DEVELOPMENT_DATABASE_COMMENT = "FiveFold development database"
TEMPLATE_DATABASE_COMMENT = "FiveFold template database"
REGISTRY_DATABASE_COMMENT = "FiveFold registry database"
MANAGED_COMMENTS = frozenset(
    {
        DEVELOPMENT_DATABASE_COMMENT,
        TEMPLATE_DATABASE_COMMENT,
        REGISTRY_DATABASE_COMMENT,
        RUN_DATABASE_COMMENT,
    }
)

UNMARKED_RELEASE_LAST_MIGRATION = "0014_tool_call_args.sql"
"""The last migration in 0.1.0, the final release before run databases had a marker."""

Connection = psycopg.Connection[TupleRow]


class MigrationError(Exception):
    """The migration files or the database are in a state the runner refuses to touch."""


class DatabaseExists(MigrationError):
    """`create_database` found the name taken, and left that database as it was."""


@dataclass(frozen=True)
class CreatedDatabase:
    """A database `create_database` made: its name, its OID and its DSN.

    The OID tells it apart from any database made under the same name later, so cleanup
    can drop only the database it made (`drop_database`).
    """

    name: str
    oid: int
    dsn: str


@dataclass(frozen=True)
class Migration:
    """One migration file, read once.

    `sql` holds the bytes that were hashed, and the runner executes exactly those bytes,
    so the recorded checksum always describes what ran, even if the file changes while
    the runner waits for its lock.
    """

    number: int
    name: str
    path: Path
    sha256: str
    sql: bytes = field(repr=False)


def discover(directory: Path) -> list[Migration]:
    """Return the migrations in `directory` in number order.

    Every `.sql` file must be named `NNNN_name.sql`, and the numbers must run 1, 2, 3 and
    so on, with no gap and no duplicate. A missing directory is an error, not an empty
    list: otherwise a mistyped path would look like a database with nothing to apply.
    """
    if not directory.is_dir():
        raise MigrationError(f"migration directory not found: {directory}")
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_FILE.match(path.name)
        if match is None:
            raise MigrationError(f"bad migration file name: {path.name}")
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        migrations.append(Migration(int(match["number"]), path.name, path, digest, content))
    migrations.sort(key=lambda migration: migration.number)
    numbers = [migration.number for migration in migrations]
    if numbers != list(range(1, len(numbers) + 1)):
        raise MigrationError(f"migration numbers must run 1, 2, 3 with no gaps: {numbers}")
    return migrations


def bootstrap(conn: Connection, script: Path) -> None:
    """Run the cluster-level bootstrap script in one transaction."""
    _require_autocommit(conn)
    _require_supported_database(conn)
    with conn.transaction():
        conn.execute(script.read_bytes())


def migrate(conn: Connection, directory: Path) -> list[str]:
    """Apply every pending migration in `directory`, and return the names applied.

    The database must use UTF-8; otherwise text comes back as raw bytes.
    """
    _require_autocommit(conn)
    _require_supported_database(conn)
    row = conn.execute("SHOW server_encoding").fetchone()
    encoding = row[0] if row is not None else None
    if isinstance(encoding, bytes):
        encoding = encoding.decode("ascii", errors="replace")
    if encoding != "UTF8":
        raise MigrationError(f"the database must use UTF8 encoding, not {encoding}")
    migrations = discover(directory)
    # Take the lock before anything else: CREATE TABLE IF NOT EXISTS does not serialize
    # concurrent creators, and two runners on a fresh database would otherwise collide.
    conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migration ("
            " filename text PRIMARY KEY,"
            " sha256 text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
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
                conn.execute(migration.sql)
                conn.execute(
                    "INSERT INTO public.schema_migration (filename, sha256) VALUES (%s, %s)",
                    (migration.name, migration.sha256),
                )
            newly_applied.append(migration.name)
        return newly_applied
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


@contextlib.contextmanager
def database_lock(admin_dsn: str) -> Iterator[Connection]:
    """An autocommit connection to the admin database that holds the database lock
    (`DATABASE_LOCK_KEY`) until the block ends.

    Every create and drop in this module takes it, so a name's check and its change are
    one step to any other process that goes through this module. Advisory locks belong to
    the database they are taken in, so the processes must share the admin database; one
    that creates or drops databases another way is not held back.
    """
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        _require_supported_database(conn)
        conn.execute("SELECT pg_advisory_lock(%s)", (DATABASE_LOCK_KEY,))
        try:
            yield conn
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (DATABASE_LOCK_KEY,))


def recreate_database(admin_dsn: str, name: str, migrations: Path = DB_DIR / "migrations") -> str:
    """Reset an owned FiveFold development database and return its DSN.

    Both happen under the database lock (`database_lock`). The database always uses
    UTF-8 with C collation, as `create_database` makes it.
    """
    check_database_name(name)
    with database_lock(admin_dsn) as conn:
        oid = _development_database(conn, admin_dsn, name, migrations)
        _reset_database(conn, name, oid)
    return make_conninfo(admin_dsn, dbname=name)


def create_database(admin_dsn: str, name: str) -> CreatedDatabase:
    """Create the empty database `name`, and never drop anything to do it.

    Raises `DatabaseExists` when the name is taken; that database is left as it was. It
    holds the database lock (`database_lock`).

    Checked in psycopg 3.3.9 (`psycopg/errors.py`): `CREATE DATABASE` of a taken name
    raises `DuplicateDatabase`, SQLSTATE 42P04.
    """
    check_database_name(name)
    with database_lock(admin_dsn) as conn:
        try:
            _create_database(conn, name)
        except psycopg.errors.DuplicateDatabase:
            raise DatabaseExists(f"the database {name} already exists") from None
        row = conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if row is None:
        raise MigrationError(f"the database {name} was dropped as soon as it was created")
    return CreatedDatabase(name=name, oid=int(row[0]), dsn=make_conninfo(admin_dsn, dbname=name))


def _create_database(conn: Connection, name: str) -> None:
    """Create the empty database `name` on the admin connection `conn`.

    It always uses UTF-8 with C collation, whatever the server's defaults. C collation
    orders text by byte value, so query results do not depend on the host's locale (0009).
    """
    conn.execute(
        sql.SQL(
            "CREATE DATABASE {} ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0"
        ).format(sql.Identifier(name))
    )


def _mark_database(conn: Connection, name: str, marker: str) -> None:
    conn.execute(
        sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(name), sql.Literal(marker))
    )


def _database_identity(conn: Connection, name: str) -> tuple[int, str | None, bool] | None:
    row = conn.execute(
        "SELECT oid, pg_catalog.shobj_description(oid, 'pg_database'),"
        " pg_catalog.pg_get_userbyid(datdba) = current_user"
        " FROM pg_catalog.pg_database WHERE datname = %s",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), row[1], bool(row[2])


def _special_kind(name: str) -> str | None:
    if name in (TEMPLATE_DATABASE, template_database()):
        return TEMPLATE_DATABASE_COMMENT
    if name in (REGISTRY_DATABASE, registry_database()):
        return REGISTRY_DATABASE_COMMENT
    if name == DEVELOPMENT_DATABASE:
        return DEVELOPMENT_DATABASE_COMMENT
    return None


def _development_database(
    conn: Connection, admin_dsn: str, name: str, migrations: Path
) -> int | None:
    marker = _special_kind(name)
    if marker not in (None, DEVELOPMENT_DATABASE_COMMENT):
        raise MigrationError(f"the database {name} is reserved. Name a development database")
    return _managed_oid(conn, admin_dsn, name, migrations, DEVELOPMENT_DATABASE_COMMENT)


def _reset_database(conn: Connection, name: str, oid: int | None) -> None:
    if oid is not None and not drop_database_locked(conn, name, oid=oid):
        raise MigrationError(f"the database {name} changed during reset. Try again")
    _create_database(conn, name)
    _mark_database(conn, name, DEVELOPMENT_DATABASE_COMMENT)


def template_database(environ: Mapping[str, str] | None = None) -> str:
    """The template database's name: FIVEFOLD_TEMPLATE_DB if set, else `fivefold_template`.

    `environ` defaults to the process environment.
    """
    env = os.environ if environ is None else environ
    name = env.get(TEMPLATE_ENV) or TEMPLATE_DATABASE
    check_database_name(name)
    return name


def prepare_template(admin_dsn: str, migrations: Path, name: str | None = None) -> str:
    """Make the template database unless it exists, migrate it forward with the files in
    `migrations`, and return its name. `name` defaults to `template_database()`.

    It holds the database lock (`database_lock`), so no copy of the template starts while
    this connects to it.
    """
    name = template_database() if name is None else name
    check_database_name(name)
    with database_lock(admin_dsn) as conn:
        _prepare_template(conn, admin_dsn, name, migrations)
    return name


def create_run_database(
    admin_dsn: str,
    name: str,
    migrations: Path,
    *,
    template: str | None = None,
    replace: bool = False,
    before_copy: Callable[[Connection], None] | None = None,
) -> CreatedDatabase:
    """Make the run database `name` as a copy of the migrated template database, and
    return it. `template` defaults to `template_database()`.

    Everything happens in one hold of the database lock (`database_lock`):

    1. Check any existing destination's OID and shared catalog comment. If given,
       `before_copy` checks the request on the lock's connection.
    2. Make or migrate the template with the files in `migrations` and close its
       connection. With `replace`, drop the destination only if it bears the marker
       FiveFold sets on a run database or its full migration history matches. The
       database must belong to the current administrator and still have the checked OID.
    3. Copy the template and mark the new run database with its own shared catalog comment.

    Without `replace`, a taken name raises `DatabaseExists`, and that database is left as
    it was. The result's OID lets cleanup drop only the database made here
    (`drop_database`).

    Two processes that copy at once take turns on the lock, and both copies work. Another
    session connected to the template, one that does not go through this module, makes
    the copy wait up to 5 seconds for it to end, then fail: that raises `MigrationError`.
    So does a run database named as the template, and a template whose applied
    migrations do not match `migrations`.

    Checked in the PostgreSQL 16 source: `createdb` (`src/backend/commands/dbcommands.c`)
    reports a taken name before it checks the source database for other sessions, and
    `CountOtherDBBackends` (`src/backend/storage/ipc/procarray.c`) waits for them up to
    5 seconds. Checked in psycopg 3.3.9 (`psycopg/errors.py`): SQLSTATE 55006 raises
    `ObjectInUse`, and 42P04 raises `DuplicateDatabase`.
    """
    template = template_database() if template is None else template
    check_database_name(name)
    check_database_name(template)
    if name == template:
        raise MigrationError(f"the database {name} is the template run databases are copied from")
    if _special_kind(name) is not None:
        raise MigrationError(f"the database {name} is reserved. Use an unused run database name")
    with database_lock(admin_dsn) as conn:
        existing = _replacement_oid(conn, admin_dsn, name, migrations, replace=replace)
        if before_copy is not None:
            before_copy(conn)
        _prepare_template(conn, admin_dsn, template, migrations)
        if existing is not None and not drop_database_locked(conn, name, oid=existing):
            raise DatabaseExists(
                f"the database {name} changed during replacement. Use an unused name"
            )
        try:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(name), sql.Identifier(template)
                )
            )
        except psycopg.errors.DuplicateDatabase:
            raise DatabaseExists(f"the database {name} already exists") from None
        except psycopg.errors.ObjectInUse:
            raise MigrationError(
                f"the template database {template} is in use by another session, so it cannot"
                " be copied; close that session and try again"
            ) from None
        _mark_database(conn, name, RUN_DATABASE_COMMENT)
        row = conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if row is None:
        raise MigrationError(f"the database {name} was dropped as soon as it was created")
    return CreatedDatabase(name=name, oid=int(row[0]), dsn=make_conninfo(admin_dsn, dbname=name))


def check_run_destinations(
    admin_dsn: str, names: Sequence[str], migrations: Path, *, replace: bool
) -> None:
    """Refuse a group of runs before provisioning any of them if a name is taken.

    Each create must still recheck its destination after this preflight releases the lock.
    """
    template = template_database()
    with database_lock(admin_dsn) as conn:
        for name in names:
            check_database_name(name)
            if name == template:
                raise MigrationError(f"the database {name} is the template run databases copy")
            if _special_kind(name) is not None:
                raise MigrationError(
                    f"the database {name} is reserved. Use an unused run database name"
                )
            _replacement_oid(conn, admin_dsn, name, migrations, replace=replace)


def _replacement_oid(
    conn: Connection, admin_dsn: str, name: str, migrations: Path, *, replace: bool
) -> int | None:
    """Check an existing destination on the connection that holds the database lock."""
    identity = _database_identity(conn, name)
    if identity is None:
        return None
    if not replace:
        raise DatabaseExists(
            f"the database {name} already exists. Use an unused name or pass --replace"
        )
    oid, marker, owned = identity
    if not owned or (
        marker != RUN_DATABASE_COMMENT
        and (marker is not None or not _has_migration_history(admin_dsn, name, migrations))
    ):
        raise MigrationError(
            f"the database {name} is not a run database with FiveFold's marker or matching"
            " migration history and the current database administrator as its owner."
            " Use an unused database name"
        )
    return oid


def _has_migration_history(admin_dsn: str, name: str, migrations: Path) -> bool:
    """Recognize the complete current or 0.1.0 history using on-disk checksums."""
    expected = _expected_history(migrations)
    history = _read_history(admin_dsn, name, len(expected))
    if not history:
        return False
    if history == expected:
        return True
    for end, (filename, _) in enumerate(expected, start=1):
        if filename == UNMARKED_RELEASE_LAST_MIGRATION:
            return history == expected[:end]
    return False


def _expected_history(directory: Path) -> list[tuple[str, str]]:
    return [(migration.name, migration.sha256) for migration in discover(directory)]


def _read_history(admin_dsn: str, name: str, maximum: int) -> list[tuple[str, str]]:
    try:
        with psycopg.connect(make_conninfo(admin_dsn, dbname=name)) as target:
            target.read_only = True
            target.execute("SET LOCAL search_path = pg_catalog")
            target.execute("LOCK TABLE ONLY public.schema_migration IN ACCESS SHARE MODE")
            # Read ordinary heap data only. In particular, a same-named view or
            # foreign table must never run a query as the provisioning administrator.
            table = target.execute(
                "SELECT c.oid FROM pg_catalog.pg_class AS c"
                " JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace"
                " JOIN pg_catalog.pg_am AS am ON am.oid = c.relam"
                " WHERE n.nspname = 'public' AND c.relname = 'schema_migration'"
                " AND c.relkind = 'r' AND am.amname = 'heap' AND NOT c.relrowsecurity"
                " AND (SELECT count(*) FROM pg_catalog.pg_attribute AS a"
                " WHERE a.attrelid = c.oid AND a.attname IN ('filename', 'sha256')"
                " AND a.atttypid = 'pg_catalog.text'::pg_catalog.regtype) = 2"
            ).fetchone()
            if table is None:
                return []
            rows = target.execute(
                "SELECT filename, sha256 FROM ONLY public.schema_migration"
                ' ORDER BY filename COLLATE pg_catalog."C" LIMIT %s',
                (maximum + 1,),
            ).fetchall()
    except psycopg.Error:
        return []
    return [(str(filename), str(checksum)) for filename, checksum in rows]


def _managed_oid(
    conn: Connection, admin_dsn: str, name: str, migrations: Path, expected_marker: str
) -> int | None:
    """Recognize or adopt a database under the server-wide provisioning lock."""
    identity = _database_identity(conn, name)
    if identity is None:
        return None
    oid, marker, owned = identity
    if owned and marker == expected_marker:
        return oid
    # Retain prefix recognition through at least 0.3 for upgrades from unmarked releases.
    if owned and marker is None and expected_marker != RUN_DATABASE_COMMENT:
        expected = _expected_history(migrations)
        history = _read_history(admin_dsn, name, len(expected))
        if (
            history
            and history == expected[: len(history)]
            and _database_identity(conn, name) == identity
        ):
            _mark_database(conn, name, expected_marker)
            print(f"Adopted database {name} through {history[-1][0]}.")
            return oid
    remedy = (
        "Fix its owner or rename the database"
        if expected_marker == REGISTRY_DATABASE_COMMENT
        else "Drop it and FiveFold will create it again"
    )
    raise MigrationError(f"the database {name} is not an owned {expected_marker}. {remedy}")


def _migrate_managed(
    conn: Connection,
    admin_dsn: str,
    name: str,
    migrations: Path,
    marker: str,
    *,
    create: bool = False,
) -> list[str]:
    discover(migrations)
    if _special_kind(name) not in (None, marker):
        raise MigrationError(
            f"the database {name} is reserved for a different FiveFold database kind"
        )
    if _managed_oid(conn, admin_dsn, name, migrations, marker) is None:
        if not create:
            raise MigrationError(f"the database {name} does not exist. Create it through FiveFold")
        _create_database(conn, name)
        _mark_database(conn, name, marker)
    with psycopg.connect(make_conninfo(admin_dsn, dbname=name), autocommit=True) as database:
        return migrate(database, migrations)


def _migrate_from_cli(admin_dsn: str, db_dir: Path, name: str, *, reset: bool) -> list[str]:
    check_database_name(name)
    with database_lock(admin_dsn) as conn:
        if reset:
            migrations = db_dir / "migrations"
            discover(migrations)
            oid = _development_database(conn, admin_dsn, name, migrations)
            bootstrap_server_locked(conn, admin_dsn, db_dir)
            _reset_database(conn, name, oid)
            marker = DEVELOPMENT_DATABASE_COMMENT
        else:
            identity = _database_identity(conn, name)
            found = None if identity is None else identity[1]
            selected = _special_kind(name) or (found if found in MANAGED_COMMENTS else None)
            marker = selected or DEVELOPMENT_DATABASE_COMMENT
            migrations = db_dir / (
                "registry" if marker == REGISTRY_DATABASE_COMMENT else "migrations"
            )
        return _migrate_managed(conn, admin_dsn, name, migrations, marker)


def _prepare_template(conn: Connection, admin_dsn: str, template: str, migrations: Path) -> None:
    """`prepare_template` on `conn`, an admin connection that holds the database lock."""
    try:
        _migrate_managed(
            conn, admin_dsn, template, migrations, TEMPLATE_DATABASE_COMMENT, create=True
        )
    except MigrationError as error:
        raise MigrationError(
            f"the template database {template} cannot be used: {error}; drop it, and the"
            " next run makes it again"
        ) from None


def registry_database(environ: Mapping[str, str] | None = None) -> str:
    """The run registry's database name: FIVEFOLD_REGISTRY_DB if set, else `fivefold_registry`.

    `environ` defaults to the process environment.
    """
    env = os.environ if environ is None else environ
    name = env.get(REGISTRY_ENV) or REGISTRY_DATABASE
    check_database_name(name)
    return name


def prepare_registry(admin_dsn: str, directory: Path, name: str | None = None) -> str:
    """Create the run registry's database unless it exists, apply its migrations from
    `directory`, and return its DSN.

    `name` defaults to `registry_database()`. The rows of an existing registry stay. Two
    processes can prepare the same registry at once: the one that loses the race to
    create it finds it created.
    """
    name = registry_database() if name is None else name
    check_database_name(name)
    with database_lock(admin_dsn) as conn:
        _migrate_managed(conn, admin_dsn, name, directory, REGISTRY_DATABASE_COMMENT, create=True)
    return make_conninfo(admin_dsn, dbname=name)


def bootstrap_server(admin_dsn: str, db_dir: Path) -> str:
    """Create the component roles from `db_dir/bootstrap.sql` and the run registry from
    `db_dir/registry/`, and return the registry's database name.

    Runs as a superuser, and is safe to run again: what exists stays.
    """
    with database_lock(admin_dsn) as conn:
        return bootstrap_server_locked(conn, admin_dsn, db_dir)


def bootstrap_server_locked(conn: Connection, admin_dsn: str, db_dir: Path) -> str:
    """Bootstrap on a connection that already holds the provisioning lock."""
    bootstrap(conn, db_dir / "bootstrap.sql")
    name = registry_database()
    _migrate_managed(
        conn, admin_dsn, name, db_dir / "registry", REGISTRY_DATABASE_COMMENT, create=True
    )
    return name


def drop_database(admin_dsn: str, name: str, *, oid: int | None = None) -> bool:
    """Drop the database `name` if it exists, closing any open connections to it. Return
    whether a database was dropped.

    With `oid`, drop it only while `name` is still the database with that OID, as
    `create_database` reported it: a database someone made under the same name since is
    left alone. The check and the drop happen under the database lock (`database_lock`),
    so no process that goes through this module can replace the database between them.
    """
    check_database_name(name)
    with database_lock(admin_dsn) as conn:
        return drop_database_locked(conn, name, oid=oid)


def drop_database_locked(conn: Connection, name: str, *, oid: int | None = None) -> bool:
    """`drop_database` on `conn`, an admin connection that holds the database lock
    (`database_lock`), so that the caller can do more under the same lock: a study
    removes the dropped database's registry row before any other study can create the
    name again (`fivefold.harness.study`)."""
    check_database_name(name)
    row = conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if row is None or (oid is not None and int(row[0]) != oid):
        return False
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    return True


def check_database_name(name: str) -> None:
    """Refuse a name PostgreSQL would truncate or that needs quoting, before any change."""
    if DATABASE_NAME.fullmatch(name) is None:
        raise MigrationError(
            f"database names must be lowercase letters, digits and underscores, "
            f"start with a letter or underscore, and be at most 63 bytes: {name!r}"
        )


def _require_autocommit(conn: Connection) -> None:
    if not conn.autocommit:
        raise MigrationError("use an autocommit connection; the runner manages its transactions")


def _require_supported_database(conn: Connection) -> None:
    if conn.info.server_version < 160000:
        raise MigrationError("migrations require PostgreSQL 16 or newer")


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point. Returns the process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m fivefold.harness.migrations",
        description="Create the component roles and apply database migrations.",
    )
    parser.add_argument(
        "command",
        choices=["bootstrap", "migrate", "reset"],
        help="bootstrap: create roles and the run registry. migrate: apply pending "
        "migrations. reset: recreate the database, then bootstrap and migrate.",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_ADMIN_DSN),
        help="admin connection string (default: FIVEFOLD_ADMIN_DSN, or the local Compose database)",
    )
    parser.add_argument(
        "--database", default=DEVELOPMENT_DATABASE, help="database to migrate or reset"
    )
    parser.add_argument(
        "--db-dir",
        type=Path,
        default=DB_DIR,
        help="directory holding bootstrap.sql and migrations/ (default: bundled data)",
    )
    args = parser.parse_args(argv)
    admin_dsn: str = args.dsn
    database: str = args.database
    db_dir: Path = args.db_dir

    try:
        if args.command == "bootstrap":
            registry = bootstrap_server(admin_dsn, db_dir)
            print(f"bootstrap: roles and the run registry {registry} are in place")
            return 0
        applied = _migrate_from_cli(admin_dsn, db_dir, database, reset=args.command == "reset")
    except MigrationError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"{args.command}: applied {len(applied)} migration(s) to {database}: {applied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
