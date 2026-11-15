"""Recorded playback of whole runs (0009, Playback and branching; Determinism contract).

Demo 1 is recorded in B0, G1 and G2, then played back from its cassette into a fresh run
database: the replay serves every recorded call, and its message log, effects ledger,
action events, approval events, other harness logs and evaluation JSON are byte-identical
to the original run's, ignoring wall-clock columns. A replay against a changed scenario or
a different fault plan fails on the first request that differs, which a second recording
locates independently. Playback checks the scenario and the original run before it runs,
and never writes to the original run's database.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest

from fivefold.agents.replay import ReplayError
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario_file, scenario_from_data
from fivefold.evaluator import without_wall_clock
from fivefold.harness.cells import cell_plan, cell_scenario, load_expected
from fivefold.harness.demo import DEV_001, DEV_002, demo_1_plan, demo_1_spec
from fivefold.harness.migrations import drop_database
from fivefold.harness.models import Replay
from fivefold.harness.run import CONFIGS, Config, InprocRun, RunSpec
from fivefold.recorder.__main__ import main
from fivefold.recorder.cassette import Cassette
from fivefold.recorder.playback import PlaybackError, compare, play, record, records

pytestmark = pytest.mark.pg

EXPECTED = load_expected()

MESSAGE_LOG: LiteralString = (
    "SELECT seq, hop, kind, sender, receiver, api, correlation, intent_id, idempotency_key,"
    " answer_class, body, sent_vt, fate, drop_reason, delivered_seq, delivered_vt, copy_of"
    " FROM harness.message_log ORDER BY seq"
)
COPIES: LiteralString = (
    "SELECT seq, hop, sender, receiver, api, copy_of, fate, drop_reason, delivered_seq"
    " FROM harness.message_log WHERE copy_of IS NOT NULL ORDER BY seq"
)
EFFECTS: LiteralString = "SELECT to_jsonb(e) FROM sim.effect e ORDER BY commit_seq"
EVALUATION: LiteralString = "SELECT result FROM eval.run_result"
RUN: LiteralString = "SELECT model_adapter, model, cassette_digest FROM harness.run"
FAULTS: LiteralString = "SELECT DISTINCT fault FROM harness.fault_log ORDER BY fault"


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def without_adapter(result: dict[str, Any]) -> object:
    """The evaluation JSON without the run's model adapter, which a replay names `replay`,
    and without its wall-clock fields, as playback compares it (0014)."""
    run = {k: v for k, v in result["run"].items() if k != "model_adapter"}
    return without_wall_clock({**result, "run": run})


def dearer_quote(dev_data: dict[str, Any]) -> Scenario:
    """dev-001 with S-1's quote at 5.45 USD a unit instead of 5.40: still the cheapest."""
    dev_data["facts"]["quote/S-1/W-100"]["unit_price_minor"] = 545
    return scenario_from_data(dev_data)


def first_difference(recorded: Cassette, changed: Cassette) -> int:
    """The number of the first call whose request differs between two recordings."""
    return next(
        number
        for number, (a, b) in enumerate(zip(recorded.calls, changed.calls, strict=False), start=1)
        if a.request != b.request
    )


@pytest.mark.parametrize(("config", "verdict"), [("B0", "fail"), ("G1", "fail"), ("G2", "pass")])
def test_demo_1_plays_back_byte_for_byte_from_its_cassette(
    run_database: Callable[[str], str], config: Config, verdict: str
) -> None:
    original, replay = run_database(f"{config.lower()}_rec"), run_database(f"{config.lower()}_play")
    recorded, cassette = record(demo_1_spec(config, 7), original)
    assert recorded.result.evaluation is not None
    assert str(recorded.result.evaluation.verdict) == verdict
    assert rows(original, FAULTS) == [("F11",), ("F2",)], "both of demo 1's faults fired"

    playback = play(cassette, load_scenario_file(DEV_001), original=original, replay=replay)

    assert playback.differences == ()
    assert playback.matches
    assert (playback.calls, len(cassette.calls)) == (7, 7)
    assert playback.verdict == verdict
    assert playback.run_id == cassette.run.run_id == f"dev-001-single-po.{config}.C2.7"
    assert rows(replay, MESSAGE_LOG) == rows(original, MESSAGE_LOG)
    assert rows(replay, EFFECTS) == rows(original, EFFECTS)
    ((replayed,),), ((first,),) = rows(replay, EVALUATION), rows(original, EVALUATION)
    assert without_adapter(replayed) == without_adapter(first)
    assert replayed["run"]["model_adapter"] == "replay"
    assert rows(original, RUN) == [("scripted", "scripted", cassette.digest())]
    assert rows(replay, RUN) == [("replay", "scripted", cassette.digest())]


def test_recording_changes_nothing_about_the_run(run_database: Callable[[str], str]) -> None:
    """The wrapper returns what the scripted model returns, so the run is the same run."""
    plain_dsn, recorded_dsn = run_database("plain"), run_database("recorded")
    asyncio.run(InprocRun(demo_1_spec("G2", 7), plain_dsn).execute())
    _, cassette = record(demo_1_spec("G2", 7), recorded_dsn)
    differences = compare(records(plain_dsn), records(recorded_dsn))
    assert [difference.records for difference in differences] == ["evaluation"]
    ((plain,),), ((recorded,),) = rows(plain_dsn, EVALUATION), rows(recorded_dsn, EVALUATION)
    assert (plain["run"]["cassette_digest"], recorded["run"]["cassette_digest"]) == (
        None,
        cassette.digest(),
    )
    recorded["run"]["cassette_digest"] = None
    assert without_wall_clock(plain) == without_wall_clock(recorded), (
        "only the run's record of its cassette differs, and the wall clock"
    )


def test_a_replay_against_a_changed_scenario_fails_on_the_first_differing_request(
    run_database: Callable[[str], str], dev_data: dict[str, Any]
) -> None:
    _, cassette = record(demo_1_spec("G2", 7), run_database("scenario_rec"))
    changed = dearer_quote(dev_data)
    _, other = record(demo_1_spec("G2", 7, changed), run_database("scenario_other"))
    first = first_difference(cassette, other)
    assert first == 2, "the first request to differ carries S-1's quote"

    replay = run_database("scenario_play")
    spec = replace(demo_1_spec("G2", 7, changed), model=Replay(cassette))
    run = InprocRun(spec, replay)
    with pytest.raises(ReplayError) as caught:
        asyncio.run(run.execute())
    assert run.models.served == first - 1
    error = caught.value
    assert (error.call, error.recorded, error.asked) == (
        first,
        cassette.calls[first - 1].request,
        other.calls[first - 1].request,
    )
    assert f"model call {first}: agent-1 asked a request with hash {error.asked}" in str(error)
    assert rows(replay, RUN) == [("replay", "scripted", cassette.digest())]
    assert rows(replay, EVALUATION) == [], "a failed replay is never evaluated"


def test_a_replay_against_a_different_fault_plan_fails_on_the_first_differing_request(
    run_database: Callable[[str], str],
) -> None:
    """Without F11 the agent is not killed after the order call: the order's result, which
    the fifth request carries, differs."""
    _, cassette = record(demo_1_spec("G2", 7), run_database("plan_rec"))
    lost_reply_only = demo_1_plan("G2")[:1]
    spec = replace(demo_1_spec("G2", 7), plan=lost_reply_only)
    _, other = record(spec, run_database("plan_other"))
    first = first_difference(cassette, other)
    assert first == 5

    run = InprocRun(replace(spec, model=Replay(cassette)), run_database("plan_play"))
    with pytest.raises(ReplayError) as caught:
        asyncio.run(run.execute())
    assert run.models.served == first - 1
    error = caught.value
    assert (error.call, error.recorded, error.asked) == (
        first,
        cassette.calls[first - 1].request,
        other.calls[first - 1].request,
    )


def test_two_workers_play_back_in_the_order_they_were_recorded(
    run_database: Callable[[str], str],
) -> None:
    """F9 starts a second worker; the cassette keeps both workers' calls in run order."""
    f9 = {
        "fault": "F9",
        "variant": "different_args",
        "at": {"point": "agent.step", "match": {"worker": "agent-1"}, "occurrence": 2},
    }
    spec = RunSpec(load_scenario_file(DEV_001), "G2", Contract.C2, 7, plan=[f9])
    original, replay = run_database("f9_rec"), run_database("f9_play")
    _, cassette = record(spec, original)
    workers = [call.worker for call in cassette.calls]
    assert set(workers) == {"agent-1", "agent-2"}
    assert workers != sorted(workers), "the two workers' calls interleave"

    playback = play(cassette, load_scenario_file(DEV_001), original=original, replay=replay)
    assert playback.differences == ()
    assert playback.calls == len(cassette.calls)


def cell_run(name: str) -> tuple[RunSpec, Scenario]:
    """The run of the cell `name` of `faults/expected.yaml`, as its own test runs it."""
    (cell,) = [cell for cell in EXPECTED.cells if cell.name == name]
    scenario = cell_scenario(cell, load_scenario_file(DEV_001))
    (config,) = [config for config in CONFIGS if config == cell.config]
    return RunSpec(scenario, config, cell.contract, EXPECTED.seed, plan=cell_plan(cell)), scenario


@pytest.mark.parametrize(
    ("name", "copies"),
    [
        ("F4-agent_gw-C2-G2", 1),
        ("F4-agent_gw-C2-G1", 1),
        ("F4-gw_sim-C2-G2", 1),
        ("F4-agent_sim-C2-B0", 1),
        ("F5-in_contract-C2-G2", 0),
        ("F5-beyond_deadline-C1-G2", 0),
        ("F10-stale_within_lag-C1-G2", 0),
        ("F10-stale_beyond_lag-C1-G2", 0),
    ],
)
def test_copies_and_the_simulator_s_faults_play_back_byte_for_byte(
    run_database: Callable[[str], str], name: str, copies: int
) -> None:
    """F4's second delivery is a message of its own, a copy, and F5 and F10 act at the
    simulator's points: a playback reproduces them with everything else. F4's copy delay
    is drawn from the seed, and the cassette's plan keeps the value drawn. The F10 cell
    within the lag runs dev-001 with a status lag of 3,000 ms, so its scenario is not the
    file's. The F10 cell beyond the lag fires where the order reaches the simulator (0013)."""
    spec, scenario = cell_run(name)
    tag = name.lower().replace("-", "_")
    original, replay = run_database(f"{tag}_rec"), run_database(f"{tag}_play")
    recorded, cassette = record(spec, original)
    assert recorded.result.evaluation is not None

    playback = play(cassette, scenario, original=original, replay=replay)

    assert playback.differences == ()
    assert playback.calls == len(cassette.calls)
    assert playback.verdict == str(recorded.result.evaluation.verdict)
    assert len(rows(original, COPIES)) == copies
    assert rows(replay, COPIES) == rows(original, COPIES)
    assert rows(replay, MESSAGE_LOG) == rows(original, MESSAGE_LOG)
    fault = name.split("-")[0]
    assert (fault,) in rows(replay, "SELECT fault FROM harness.fault_log")
    plan = cassette.run.params["plan"]
    assert isinstance(plan, list) and len(plan) == len(spec.plan or ())
    drawn = [entry["params"] for entry in plan if isinstance(entry, dict)]
    assert any(isinstance(p, dict) and "copy_delay_ms" in p for p in drawn) == (fault == "F4")


def test_playback_checks_the_scenario_before_it_runs(
    run_database: Callable[[str], str], dev_data: dict[str, Any]
) -> None:
    original, replay = run_database("check_rec"), run_database("check_play")
    _, cassette = record(demo_1_spec("G2", 7), original)
    with pytest.raises(PlaybackError, match="changed since the recording"):
        play(cassette, dearer_quote(dev_data), original=original, replay=replay)
    with pytest.raises(PlaybackError, match="recorded on dev-001-single-po, not dev-002"):
        play(cassette, load_scenario_file(DEV_002), original=original, replay=replay)
    assert rows(replay, "SELECT count(*) FROM harness.run") == [(0,)]


def test_playback_checks_that_the_original_run_recorded_the_cassette(
    run_database: Callable[[str], str],
) -> None:
    recorded, plain, replay = (
        run_database("own_rec"),
        run_database("own_plain"),
        run_database("own_play"),
    )
    _, cassette = record(demo_1_spec("G2", 7), recorded)
    asyncio.run(InprocRun(demo_1_spec("G2", 7), plain).execute())
    scenario = load_scenario_file(DEV_001)
    with pytest.raises(PlaybackError, match="recorded the cassette None, not this one"):
        play(cassette, scenario, original=plain, replay=replay)
    edited = cassette.model_copy(update={"calls": cassette.calls[:-1]})
    with pytest.raises(PlaybackError, match=f"recorded the cassette {cassette.digest()}"):
        play(edited, scenario, original=recorded, replay=replay)
    assert rows(replay, "SELECT count(*) FROM harness.run") == [(0,)]


def test_playback_never_writes_to_the_original_run(run_database: Callable[[str], str]) -> None:
    original, replay = run_database("ro_rec"), run_database("ro_play")
    _, cassette = record(demo_1_spec("G2", 7), original)
    scenario = load_scenario_file(DEV_001)
    before = records(original)
    with pytest.raises(PlaybackError, match="already holds a run"):
        play(cassette, scenario, original=original, replay=original)
    assert play(cassette, scenario, original=original, replay=replay).matches
    assert records(original) == before
    assert rows(original, RUN) == [("scripted", "scripted", cassette.digest())]


@pytest.fixture
def cli_databases(admin_dsn: str, db_prefix: str) -> Iterator[tuple[str, str]]:
    """Names for the command's two databases, which it creates; dropped afterwards."""
    names = (f"{db_prefix}_cli_recorded", f"{db_prefix}_cli_replayed")
    yield names
    for name in names:
        drop_database(admin_dsn, name)


@pytest.mark.parametrize("config", ["B0", "G1"])
def test_the_command_records_and_plays_back(
    admin_dsn: str,
    cli_databases: tuple[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config: Config,
) -> None:
    recorded, replayed = cli_databases
    path = tmp_path / f"demo-1-{config.lower()}.json"
    common = ["--dsn", admin_dsn]
    assert (
        main(
            [
                *common,
                "record",
                "--demo",
                "1",
                "--config",
                config,
                "--cassette",
                str(path),
                "--database",
                recorded,
            ]
        )
        == 0
    )
    cassette = Cassette.load(path)
    assert len(cassette.calls) == 7
    assert f"sha256 {cassette.digest()}" in capsys.readouterr().out

    play_args = [
        *common,
        "play",
        str(path),
        "--scenario",
        str(DEV_001),
        "--original",
        recorded,
        "--database",
        replayed,
    ]
    assert main(play_args) == 0
    out = capsys.readouterr().out
    assert "message log: identical" in out
    assert f"The replay matches the original run in {recorded}." in out

    edited = json.loads(path.read_text(encoding="utf-8"))
    edited["calls"][3]["response"]["content"] = "Edited."
    path.write_text(json.dumps(edited, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    assert main(play_args) == 1
    assert "play failed: the original run recorded the cassette" in capsys.readouterr().err


def test_the_command_reports_a_refused_run_in_one_line(
    admin_dsn: str,
    cli_databases: tuple[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """dev-001's own fault fires on the gateway's hop, which B0 does not have."""
    recorded, _ = cli_databases
    args = ["--dsn", admin_dsn, "record", "--scenario", str(DEV_001), "--contract", "C2"]
    path = tmp_path / "refused.json"
    args += ["--config", "B0", "--cassette", str(path), "--database", recorded]
    assert main(args) == 1
    error = capsys.readouterr().err
    assert error == (
        "record failed: not in a configuration without a gateway: F2 at net.gw_sim.answer"
        " (0006, 0008)\n"
    )
    assert not path.exists()
