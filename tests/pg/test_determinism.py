"""The determinism check: each demo, run twice from its seed, gives the same records.

`docs/spec.md`, Runs and determinism (record 0009): "the same scenario, configuration,
contract level, seed and model outputs produce byte-identical logs, ledgers and results,
ignoring wall-clock columns. CI runs each demo twice and compares digests."

Each demo of record 0006 runs twice in B0, G1 and G2, into two fresh databases. Record
0006 shows the demos in B0 and G2; the contract holds for every configuration. The check
reads every record that recorded playback compares (the harness's logs, the effects
ledger, the approval and action events, and the evaluation), with the same reader, and
needs each one to match row for row and by digest.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from fivefold.harness.demo import DEMOS, Demo
from fivefold.harness.run import Config, InprocRun
from fivefold.recorder.cassette import sha256
from fivefold.recorder.playback import RECORDS, compare, records

pytestmark = pytest.mark.pg

SEED = 7
"""The demos' committed seed."""


def run_twice(
    run_database: Callable[[str], str], demo: Demo, config: Config
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """The records of two runs of `demo` in `config` from the same seed."""
    runs: list[dict[str, list[str]]] = []
    for attempt in (1, 2):
        dsn = run_database(f"det_{demo.key}_{config.lower()}_{attempt}")
        asyncio.run(InprocRun(demo.spec(config, SEED), dsn).execute())
        runs.append(records(dsn))
    return runs[0], runs[1]


def digests(run: dict[str, list[str]]) -> dict[str, str]:
    """The SHA-256 of each record, as canonical JSON lines."""
    return {name: sha256("\n".join(rows)) for name, rows in run.items()}


@pytest.mark.parametrize("config", ["B0", "G1", "G2"])
@pytest.mark.parametrize("demo", DEMOS, ids=[demo.key for demo in DEMOS])
def test_each_demo_run_twice_from_its_seed_gives_the_same_records(
    run_database: Callable[[str], str], demo: Demo, config: Config
) -> None:
    first, second = run_twice(run_database, demo, config)
    assert compare(first, second) == ()
    assert digests(first) == digests(second)
    assert set(first) == {name for name, _ in RECORDS}, "every record playback compares"
    assert first["message log"], "the run sent messages"
    assert len(first["evaluation"]) == 1, "the run was evaluated"
    assert len(first["run stop"]) == 1, "the run stopped"


def test_another_seed_gives_other_records(run_database: Callable[[str], str]) -> None:
    """The check can fail: another seed draws other latencies, so the message log differs."""
    demo = DEMOS[0]
    runs: list[dict[str, list[str]]] = []
    for seed in (SEED, SEED + 1):
        dsn = run_database(f"det_seed_{seed}")
        asyncio.run(InprocRun(demo.spec("G2", seed), dsn).execute())
        runs.append(records(dsn))
    differing = {difference.records for difference in compare(runs[0], runs[1])}
    assert "message log" in differing


def test_the_check_sees_a_difference_in_any_record() -> None:
    """A changed row in one record is reported, with its record and row."""
    run = {name: [f'{{"row":{n}}}' for n in (1, 2)] for name, _ in RECORDS}
    changed = {**run, "effects ledger": ['{"row":1}', '{"row":3}']}
    (difference,) = compare(run, changed)
    assert (difference.records, difference.row) == ("effects ledger", 2)
    assert digests(run)["effects ledger"] != digests(changed)["effects ledger"]
