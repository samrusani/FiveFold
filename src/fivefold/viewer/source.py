"""Where the viewer reads from: the run registry and the run databases, as `ff_viewer` (0012).

The viewer never writes. Every connection it opens is as `ff_viewer`, which can only
select (0008), and every transaction it opens is read-only, so a bug in a query cannot
change a record either. Each request reads in one repeatable-read transaction, so a run
that is still going on is read as one consistent snapshot.

It reads two kinds of database on one server:

- the run registry, `fivefold_registry` unless FIVEFOLD_REGISTRY_DB names another, which
  lists the run databases (0012; the harness writes it);
- a run database, only one the registry lists, and in it only the schemas `sim`, `gw`,
  `harness` and `eval`, the ones `ff_viewer` may select from (0008).

The server comes from a connection string, such as FIVEFOLD_ADMIN_DSN. The viewer keeps
its host, port and other settings, and replaces its user, password and database: it
always connects as `ff_viewer`.

Checked in psycopg 3.3.9: `conninfo_to_dict` and `make_conninfo` (`conninfo.py`),
`Connection.read_only` and `Connection.isolation_level` (`_connection_base.py`), and
`rows.dict_row` (`rows.py`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, LiteralString

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import DictRow, dict_row

VIEWER_ROLE: Final = "ff_viewer"
"""The only role the viewer connects as (0008)."""

DEFAULT_REGISTRY: Final = "fivefold_registry"
"""The run registry's database (0012), unless FIVEFOLD_REGISTRY_DB names another. The
harness uses the same name; a test checks that they agree."""

REGISTRY_ENV: Final = "FIVEFOLD_REGISTRY_DB"

DATABASE_NAME: Final = re.compile(r"[a-z_][a-z0-9_]{0,62}")
"""A database name the harness can make (`fivefold.harness.migrations`)."""

CONNECT_TIMEOUT_S: Final = 5
"""Seconds to wait for the server before a request fails."""

Connection = psycopg.Connection[DictRow]


class ViewerError(Exception):
    """A request the viewer cannot answer, with a reason code and an HTTP status."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


class WrongRole(Exception):
    """A connection's role is not `ff_viewer`."""


def registry_name(environ: Mapping[str, str]) -> str:
    """The registry's database name: FIVEFOLD_REGISTRY_DB if set, else `fivefold_registry`."""
    name = environ.get(REGISTRY_ENV) or DEFAULT_REGISTRY
    if DATABASE_NAME.fullmatch(name) is None:
        raise ValueError(f"not a database name the harness makes: {name!r}")
    return name


def jsonable(value: object) -> Any:
    """`value` as JSON can carry it: wall-clock times become ISO 8601 text."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class RunSource:
    """The registry and the run databases on one server, read as `ff_viewer`."""

    server: str
    """A connection string naming the server. Its user, password and database are
    replaced."""
    registry: str = DEFAULT_REGISTRY

    def _conninfo(self, database: str) -> str:
        params = {
            key: value
            for key, value in conninfo_to_dict(self.server).items()
            if key not in ("user", "password", "dbname")
        }
        # A connect_timeout in the server string wins; it must not be passed twice.
        params.setdefault("connect_timeout", CONNECT_TIMEOUT_S)
        return make_conninfo("", **params, user=VIEWER_ROLE, dbname=database)

    @contextmanager
    def _read(self, database: str) -> Iterator[Connection]:
        """One read-only, repeatable-read transaction in `database`, as `ff_viewer`."""
        with psycopg.connect(self._conninfo(database), row_factory=dict_row) as conn:
            conn.read_only = True
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            row = conn.execute("SELECT current_user AS role").fetchone()
            if row is None or row["role"] != VIEWER_ROLE:
                raise WrongRole(f"the viewer connects as {VIEWER_ROLE}, not {row}")
            yield conn

    def runs(self) -> list[dict[str, Any]]:
        """Every run database the registry lists, newest first: by the wall-clock time its
        row was written, which serves display only (0009)."""
        return self._registry_rows("TRUE", ())

    def registered(self, database: str) -> dict[str, Any]:
        """The registry's row for `database`; `ViewerError` 404 if it has none."""
        if DATABASE_NAME.fullmatch(database) is not None:
            rows = self._registry_rows("database_name = %s", (database,))
            if rows:
                return rows[0]
        raise ViewerError(404, "unknown_run", f"the run registry lists no database {database!r}")

    def _registry_rows(self, where: LiteralString, args: tuple[str, ...]) -> list[dict[str, Any]]:
        query: LiteralString = (
            "SELECT database_name AS database, run_id, scenario_id, config, contract, seed,"
            " created_wall_at FROM registry.run WHERE "
            + where
            + " ORDER BY created_wall_at DESC, database_name"
        )
        try:
            with self._read(self.registry) as conn:
                rows = conn.execute(query, args).fetchall()
        except (psycopg.OperationalError, psycopg.errors.UndefinedTable) as error:
            raise ViewerError(
                503,
                "registry_unavailable",
                f"the run registry {self.registry} cannot be read: {error}. Run a demo, or"
                " `python -m fivefold.harness.migrations bootstrap`, to create it.",
            ) from error
        return [jsonable(row) for row in rows]

    @contextmanager
    def run(self, database: str) -> Iterator[Connection]:
        """One read-only snapshot of a run database the registry lists."""
        self.registered(database)
        try:
            with self._read(database) as conn:
                yield conn
        except psycopg.OperationalError as error:
            raise ViewerError(
                404, "run_unavailable", f"the run database {database} cannot be read: {error}"
            ) from error
        except psycopg.errors.UndefinedTable as error:
            raise ViewerError(
                404, "not_a_run", f"{database} is not a migrated run database: {error}"
            ) from error
