"""A split run's directory: what its services and commands share (0009).

`start` creates a new owner-only directory (mode 0700). The services, `end` and
`score` read it. Its files:

- `run.json`: the run's id, its database, configuration, contract level and seed;
- `scenario.yaml`: the scenario, exactly as the run recorded its digest;
- `callers.json`: the SHA-256 of each holder's secret (`credentials.Callers`);
- `gateway.token`, `agent-1.token` and `harness.token`: each holder's secret, which only
  the file's owner may read (mode 0600);
- `ended`: the virtual time the run stopped at, written once `end` has stopped it.

It holds no database password: the component roles have none (`db/bootstrap.sql`), and
each service connects as its own role to the database `run.json` names.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from fivefold.db_connection import role_dsn
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario_file
from fivefold.harness.split.credentials import Callers

Config = Literal["B0", "G1", "G2"]

RUN_FILE = "run.json"
SCENARIO_FILE = "scenario.yaml"
CALLERS_FILE = "callers.json"
ENDED_FILE = "ended"
TOKEN_SUFFIX = ".token"


class RunDirError(Exception):
    """The run directory is missing, incomplete, or already exists."""


@dataclass(frozen=True)
class SplitRun:
    """A split run, as its directory describes it."""

    directory: Path
    run_id: str
    database: str
    config: Config
    contract: Contract
    seed: int

    @property
    def has_gateway(self) -> bool:
        """Whether the agent writes through the gateway: G1 and G2 (0008)."""
        return self.config != "B0"

    @property
    def ended(self) -> bool:
        """Whether `end` has stopped the run."""
        return (self.directory / ENDED_FILE).exists()

    def scenario(self) -> Scenario:
        """The run's scenario."""
        return load_scenario_file(self.directory / SCENARIO_FILE)

    def callers(self) -> Callers:
        """The digests of the run's secrets."""
        data = json.loads((self.directory / CALLERS_FILE).read_text(encoding="utf-8"))
        return Callers({str(holder): str(known) for holder, known in data.items()})

    def secret(self, holder: str) -> str:
        """`holder`'s secret."""
        path = self.directory / f"{holder}{TOKEN_SUFFIX}"
        if not path.exists():
            raise RunDirError(f"{self.directory} holds no secret for {holder}")
        return path.read_text(encoding="utf-8").strip()

    def conninfo(self, base_dsn: str, role: str) -> str:
        """A connection to the run's database as `role`, on the server `base_dsn` names."""
        return role_dsn(base_dsn, role, dbname=self.database)

    def mark_ended(self, stopped_vt: int) -> None:
        """Note that the run stopped at `stopped_vt`."""
        (self.directory / ENDED_FILE).write_text(f"{stopped_vt}\n", encoding="utf-8")


@dataclass
class _ReservedRunDirectory:
    descriptor: int
    created: dict[str, os.stat_result] = field(default_factory=dict)

    def rollback(self, error: BaseException) -> None:
        """Remove only entries this start created. Preserve any replacement files."""
        for name, original in self.created.items():
            try:
                current = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
                if os.path.samestat(current, original):
                    os.unlink(name, dir_fd=self.descriptor)
            except FileNotFoundError:
                pass
            except OSError as cleanup:
                error.add_note(f"Could not remove {name} from the failed run: {cleanup}")


@contextmanager
def new_run_directory(directory: Path) -> Iterator[_ReservedRunDirectory]:
    """Reserve a new private directory and roll it back on failure."""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError as error:
        raise RunDirError(
            f"{directory} already exists: choose a new run directory, or remove this one"
            " after saving anything you need"
        ) from error
    original = directory.lstat()
    if not stat.S_ISDIR(original.st_mode) or original.st_uid != os.geteuid():
        raise RunDirError(f"{directory} must be an owner-only run directory")
    reserved = None
    try:
        reserved = _ReservedRunDirectory(
            os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        )
        status = os.fstat(reserved.descriptor)
        if not os.path.samestat(status, original) or stat.S_IMODE(status.st_mode) & 0o077:
            raise RunDirError(f"{directory} must be an owner-only run directory")
        os.fchmod(reserved.descriptor, 0o700)
        yield reserved
    except BaseException as error:
        if reserved is not None:
            reserved.rollback(error)
        try:
            if os.path.samestat(directory.lstat(), original):
                directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError as cleanup:
            error.add_note(f"Could not remove failed run directory {directory}: {cleanup}")
        raise
    finally:
        if reserved is not None:
            os.close(reserved.descriptor)


def write_run(
    run: SplitRun,
    reserved: _ReservedRunDirectory,
    *,
    scenario_text: str,
    secrets: dict[str, str],
    callers: Callers,
) -> None:
    """Write the reserved directory. Publish the run description last."""
    description = {
        "run_id": run.run_id,
        "database": run.database,
        "config": run.config,
        "contract": str(run.contract),
        "seed": run.seed,
    }
    files = {
        SCENARIO_FILE: scenario_text,
        CALLERS_FILE: json.dumps(dict(callers.digests), indent=2, sort_keys=True) + "\n",
        **{f"{holder}{TOKEN_SUFFIX}": secret + "\n" for holder, secret in sorted(secrets.items())},
        RUN_FILE: json.dumps(description, indent=2, sort_keys=True) + "\n",
    }
    for name, text in files.items():
        try:
            _write_private(reserved, name, text)
        except FileExistsError as error:
            raise RunDirError(
                f"{run.directory} already contains {name}: choose a new run directory,"
                " or remove this one after saving anything you need"
            ) from error


def _write_private(reserved: _ReservedRunDirectory, name: str, text: str) -> None:
    """Create a new owner-only file without following links."""
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=reserved.descriptor,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        reserved.created[name] = os.fstat(file.fileno())
        os.fchmod(file.fileno(), 0o600)
        file.write(text)


def read_run(directory: Path) -> SplitRun:
    """The run that `directory` describes. Raises `RunDirError`."""
    path = directory / RUN_FILE
    if not path.exists():
        raise RunDirError(f"{directory} holds no split run: start one first")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        config = str(data["config"])
        if config not in ("B0", "G1", "G2"):
            raise ValueError(f"unknown configuration {config!r}")
        return SplitRun(
            directory=directory,
            run_id=str(data["run_id"]),
            database=str(data["database"]),
            config=cast(Config, config),
            contract=Contract(str(data["contract"])),
            seed=int(data["seed"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RunDirError(f"{path} does not describe a run: {error}") from error
