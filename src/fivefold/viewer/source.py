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
the server coordinates and TLS server verification settings. It connects as
`ff_viewer` to the selected database.

Checked in psycopg 3.3.9: `conninfo_to_dict` and `make_conninfo` (`conninfo.py`),
`Connection.read_only` and `Connection.isolation_level` (`_connection_base.py`), and
`rows.dict_row` (`rows.py`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from concurrent.futures import Executor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, LiteralString

import psycopg
from psycopg.rows import DictRow, dict_row

from fivefold.db_connection import role_dsn

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

_CELL_FIELDS = ("scenario_digest", "topology", "model_adapter", "model", "planned_faults", "params")
_RUN_LIST_FIELDS = ("parent_run_id", "branch_point", *_CELL_FIELDS[:4])

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
        return role_dsn(
            self.server, VIEWER_ROLE, dbname=database, default_timeout=CONNECT_TIMEOUT_S
        )

    @contextmanager
    def _read(self, database: str) -> Iterator[Connection]:
        """One read-only, repeatable-read transaction in `database`, as `ff_viewer`."""
        with psycopg.connect(self._conninfo(database), row_factory=dict_row) as conn:
            conn.read_only = True
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.execute("SET LOCAL statement_timeout = '5s'")
            row = conn.execute("SELECT current_user AS role").fetchone()
            if row is None or row["role"] != VIEWER_ROLE:
                raise WrongRole(f"the viewer connects as {VIEWER_ROLE}, not {row}")
            yield conn

    def runs(self, *, reads: Executor | None = None) -> list[dict[str, Any]]:
        """Every run database the registry lists, newest first: by the wall-clock time its
        row was written, which serves display only (0009). Each row includes comparison
        fields and branch metadata from its own `harness.run`, when available."""
        rows = self._registry_rows("TRUE", ())
        if reads is None:
            return [self._listed_run(row) for row in rows]
        return list(reads.map(self._listed_run, rows))

    def _listed_run(self, registered: dict[str, Any]) -> dict[str, Any]:
        """Read one registry entry's run metadata. Unavailable runs stay manually listed."""
        database = str(registered["database"])
        try:
            with self._read(database) as conn:
                row = conn.execute(
                    "SELECT run_id, scenario_id, config, contract, seed::text AS seed,"
                    " parent_run_id, branch_point, scenario_digest, topology, model_adapter,"
                    " model, params FROM harness.run"
                ).fetchone()
        except (
            psycopg.OperationalError,
            psycopg.errors.UndefinedTable,
            psycopg.errors.InsufficientPrivilege,
            psycopg.errors.QueryCanceled,
        ):
            row = None
        if row is None or any(
            row[field] != registered[field]
            for field in ("run_id", "scenario_id", "config", "contract", "seed")
        ):
            row = {}
        return {**registered, **_comparison_fields(row)}

    def registered(self, database: str) -> dict[str, Any]:
        """The registry's row for `database`; `ViewerError` 404 if it has none."""
        if DATABASE_NAME.fullmatch(database) is not None:
            rows = self._registry_rows("database_name = %s", (database,))
            if rows:
                return rows[0]
        raise ViewerError(404, "unknown_run", f"the run registry lists no database {database!r}")

    def _registry_rows(self, where: LiteralString, args: tuple[str, ...]) -> list[dict[str, Any]]:
        """Registry rows, with the seed as its decimal text (`fivefold.viewer.app`)."""
        query: LiteralString = (
            "SELECT database_name AS database, run_id, scenario_id, config, contract,"
            " seed::text AS seed, created_wall_at FROM registry.run WHERE "
            + where
            + " ORDER BY created_wall_at DESC, database_name"
        )
        try:
            with self._read(self.registry) as conn:
                rows = conn.execute(query, args).fetchall()
        except psycopg.errors.QueryCanceled as error:
            raise ViewerError(503, "query_timeout", "the registry query exceeded 5 s") from error
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
        except psycopg.errors.QueryCanceled as error:
            raise ViewerError(503, "query_timeout", "the run query exceeded 5 s") from error
        except psycopg.OperationalError as error:
            raise ViewerError(
                404, "run_unavailable", f"the run database {database} cannot be read: {error}"
            ) from error
        except psycopg.errors.UndefinedTable as error:
            raise ViewerError(
                404, "not_a_run", f"{database} is not a migrated run database: {error}"
            ) from error


def _comparison_fields(run: dict[str, Any]) -> dict[str, Any]:
    fields = dict.fromkeys((*_RUN_LIST_FIELDS, "planned_faults", "params", "cell_key"))
    fields.update((field, run.get(field)) for field in _RUN_LIST_FIELDS)
    raw_params = run.get("params")
    if not isinstance(raw_params, dict) or not isinstance(raw_params.get("params"), dict):
        return fields
    faults = _planned_faults(raw_params.get("plan"))
    if faults is None:
        return fields
    fields["planned_faults"] = faults
    fields["params"] = {key: value for key, value in raw_params.items() if key != "plan"}
    if run["parent_run_id"] is None and run["branch_point"] is None:
        # Canonicalize and hash before JSON reaches JavaScript, which can round integers.
        data = [fields[key] for key in _CELL_FIELDS]
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        fields["cell_key"] = hashlib.sha256(encoded).hexdigest()
    return fields


def _planned_faults(plan: object) -> list[dict[str, str | None]] | None:
    if not isinstance(plan, list):
        return None
    faults = []
    for entry in plan:
        if not isinstance(entry, dict) or not isinstance(fault := entry.get("fault"), str):
            return None
        variant = entry.get("variant")
        if variant is not None and not isinstance(variant, str):
            return None
        # The saved plan encodes these catalog variants partly in the selector (0006).
        if (fault == "F7" and variant in {"price_up", "harmless"}) or (
            fault in {"F4", "F12"} and variant is None
        ):
            variant = _selector_variant(fault, variant, entry.get("at"))
            if variant is None:
                return None
        faults.append({"fault": fault, "variant": variant})
    return faults


def _selector_variant(fault: str, variant: str | None, at: object) -> str | None:
    if not isinstance(at, dict):
        return None
    match at.get("point"):
        case "net.agent_gw.request" if fault in {"F4", "F12"}:
            return "agent_gw"
        case "net.agent_sim.request" if fault in {"F4", "F12"}:
            return "agent_sim"
        case "net.gw_sim.request" if fault in {"F4", "F12"}:
            return "gw_sim"
        case "net.agent_sim.request" | "net.gw_sim.request" if fault == "F7":
            return f"late_{variant}"
        case "gw.dispatch.before_check" | "net.agent_sim.answer" if fault == "F7":
            return f"early_{variant}"
    return None
