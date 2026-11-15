"""A split run's directory: what its services and commands share (0009).

`start` writes it; the services, `end` and `score` read it. Its files:

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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from psycopg.conninfo import make_conninfo

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario_file
from fivefold.harness.split.credentials import Callers

Config = Literal["B0", "G1", "G2"]

RUN_FILE = "run.json"
SCENARIO_FILE = "scenario.yaml"
CALLERS_FILE = "callers.json"
ENDED_FILE = "ended"
TOKEN_SUFFIX = ".token"

FILES = (RUN_FILE, SCENARIO_FILE, CALLERS_FILE, ENDED_FILE)
"""The directory's files, besides the tokens."""


class RunDirError(Exception):
    """The run directory is missing, incomplete, or holds another run."""


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
        return make_conninfo(base_dsn, dbname=self.database, user=role)

    def mark_ended(self, stopped_vt: int) -> None:
        """Note that the run stopped at `stopped_vt`."""
        (self.directory / ENDED_FILE).write_text(f"{stopped_vt}\n", encoding="utf-8")


def write_run(
    run: SplitRun, *, scenario_text: str, secrets: dict[str, str], callers: Callers
) -> None:
    """Write `run`'s directory. The directory must hold no run."""
    directory = run.directory
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / RUN_FILE).exists():
        raise RunDirError(f"{directory} already holds a run")
    (directory / SCENARIO_FILE).write_text(scenario_text, encoding="utf-8")
    (directory / CALLERS_FILE).write_text(
        json.dumps(dict(callers.digests), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for holder, secret in sorted(secrets.items()):
        _write_private(directory / f"{holder}{TOKEN_SUFFIX}", secret + "\n")
    description = {
        "run_id": run.run_id,
        "database": run.database,
        "config": run.config,
        "contract": str(run.contract),
        "seed": run.seed,
    }
    (directory / RUN_FILE).write_text(
        json.dumps(description, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_private(path: Path, text: str) -> None:
    """Write `text` to a file only its owner may read or write."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
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


def clear_run(directory: Path) -> None:
    """Remove a run's files from `directory`, and nothing else."""
    for name in FILES:
        (directory / name).unlink(missing_ok=True)
    for token in directory.glob(f"*{TOKEN_SUFFIX}"):
        token.unlink()
