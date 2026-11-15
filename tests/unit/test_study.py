"""The study's matrix, and what the study command refuses before it touches a database.

A study runs cells of `faults/expected.yaml`, or cells with no fault, in the chosen
configurations and contract levels, each with the seeds S, S+1, ... (docs/plan.md, M4).
Its episodes run in a fixed order, by seed and then by cell, and each has a name no other
episode shares. These tests need no database: the command refuses every input here,
live runs without the flag, the spend cap or a price included, before it connects to one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file, scenario_from_data
from fivefold.harness.cells import load_expected
from fivefold.harness.demo import DEV_001, DEV_002
from fivefold.harness.models import SCRIPTED
from fivefold.harness.run import CONFIGS
from fivefold.harness.study import (
    MAX_SEED,
    Episode,
    NoFault,
    Study,
    check_out,
    check_study,
    database_name,
    main,
    select_cells,
    study_episodes,
    study_seeds,
)

EXPECTED = load_expected()
SCENARIO = load_scenario_file(DEV_001)
ALL_LEVELS = tuple(Contract)


def names(
    selectors: list[str],
    configs: tuple[str, ...] = CONFIGS,
    contracts: tuple[Contract, ...] = ALL_LEVELS,
) -> list[str]:
    cells = select_cells(
        EXPECTED.cells,
        selectors,
        configs=[config for config in CONFIGS if config in configs],
        contracts=contracts,
    )
    return [cell.name for cell in cells]


# Cells ---------------------------------------------------------------------------------------


def test_all_names_every_expected_cell_of_the_configurations_and_levels() -> None:
    assert sorted(names(["all"])) == sorted(cell.name for cell in EXPECTED.cells)
    b0_at_c0 = names(["all"], ("B0",), (Contract.C0,))
    assert sorted(b0_at_c0) == sorted(
        cell.name for cell in EXPECTED.cells if cell.config == "B0" and cell.contract is Contract.C0
    )
    assert len(b0_at_c0) == 18


def test_none_names_one_cell_with_no_fault_per_configuration_and_level() -> None:
    assert names(["none"], contracts=(Contract.C0, Contract.C2)) == [
        "none-C0-B0",
        "none-C0-G1",
        "none-C0-G2",
        "none-C2-B0",
        "none-C2-G1",
        "none-C2-G2",
    ]


def test_a_fault_a_variant_or_a_cell_names_its_cells() -> None:
    assert names(["F2"], contracts=(Contract.C2,)) == ["F2-C2-B0", "F2-C2-G1", "F2-C2-G2"]
    assert names(["F11-after_tool_call"], ("G2",), (Contract.C0, Contract.C3)) == [
        "F11-after_tool_call-C0-G2",
        "F11-after_tool_call-C3-G2",
    ]
    assert names(["F12-agent_sim-C1-B0", "F2-C2-G2"]) == ["F2-C2-G2", "F12-agent_sim-C1-B0"]
    assert names(["F10"]) == [
        "F10-delayed-C1-G2",
        "F10-stale_beyond_lag-C1-G2",
        "F10-stale_within_lag-C1-G2",
    ], "F10 has G2 cells only"


def test_cells_come_in_one_order_whatever_the_order_they_are_named_in() -> None:
    """No fault first, then by fault number, so F2 comes before F10, then by variant,
    contract level and configuration; each cell once."""
    picked = ["F10", "F2", "none", "F1-b", "F2"]
    expected = [
        "none-C1-B0",
        "none-C1-G1",
        "none-C1-G2",
        "F1-b-C1-B0",
        "F1-b-C1-G1",
        "F1-b-C1-G2",
        "F2-C1-B0",
        "F2-C1-G1",
        "F2-C1-G2",
        "F10-delayed-C1-G2",
        "F10-stale_beyond_lag-C1-G2",
        "F10-stale_within_lag-C1-G2",
    ]
    assert names(picked, contracts=(Contract.C1,)) == expected
    assert names(list(reversed(picked)), contracts=(Contract.C1,)) == expected


@pytest.mark.parametrize(
    ("selectors", "configs"),
    [
        (["F99"], CONFIGS),
        (["F2-C2"], CONFIGS),
        (["F10"], ("B0", "G1")),
        (["F2-C2-G2"], ("B0",)),
        (["F11-after_tool_call", "F12-agent_sim"], ("G2",)),
    ],
)
def test_a_name_that_names_no_cell_there_is_refused(
    selectors: list[str], configs: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match="no cell '"):
        names(selectors, configs)


# Episodes ------------------------------------------------------------------------------------


def test_each_cell_runs_with_consecutive_seeds() -> None:
    assert study_seeds(7, 3) == (7, 8, 9)
    assert study_seeds(0, 1) == (0,)
    assert study_seeds(MAX_SEED, 1) == (MAX_SEED,)
    for first, count in ((7, 0), (-1, 2), (MAX_SEED, 2)):
        with pytest.raises(ValueError):
            study_seeds(first, count)


def test_episodes_run_by_seed_then_by_cell_each_with_its_own_name() -> None:
    cells = select_cells(
        EXPECTED.cells, ["none", "F2"], configs=["B0", "G2"], contracts=[Contract.C2]
    )
    episodes = study_episodes(cells, (7, 8))
    assert [(episode.number, episode.name) for episode in episodes] == [
        (1, "none-C2-B0-seed-7"),
        (2, "none-C2-G2-seed-7"),
        (3, "F2-C2-B0-seed-7"),
        (4, "F2-C2-G2-seed-7"),
        (5, "none-C2-B0-seed-8"),
        (6, "none-C2-G2-seed-8"),
        (7, "F2-C2-B0-seed-8"),
        (8, "F2-C2-G2-seed-8"),
    ]
    assert database_name("fivefold_study", episodes[4]) == "fivefold_study_0005"


def test_every_episode_of_every_cell_has_its_own_name() -> None:
    cells = select_cells(
        EXPECTED.cells, ["all", "none"], configs=list(CONFIGS), contracts=list(ALL_LEVELS)
    )
    episodes = study_episodes(cells, study_seeds(7, 3))
    assert len(episodes) == 3 * (len(EXPECTED.cells) + 12)
    assert len({episode.name for episode in episodes}) == len(episodes)
    with pytest.raises(ValueError, match="share a name"):
        study_episodes([*cells, cells[0]], (7,))


def test_an_episode_runs_its_cell_s_plan_and_scenario() -> None:
    (lost,) = select_cells(EXPECTED.cells, ["F2-C2-G2"], configs=["G2"], contracts=[Contract.C2])
    (stale,) = select_cells(
        EXPECTED.cells, ["F10-stale_within_lag"], configs=["G2"], contracts=[Contract.C1]
    )
    plain = NoFault(Contract.C0, "B0")
    spec = Episode(1, plain, 9).spec(SCENARIO)
    assert (spec.config, spec.contract, spec.seed, spec.plan) == ("B0", Contract.C0, 9, [])
    assert spec.scenario == SCENARIO and spec.model == SCRIPTED
    spec = Episode(2, lost, 7).spec(SCENARIO)
    assert spec.plan is not None
    assert [entry["fault"] for entry in spec.plan] == ["F2"]
    assert spec.plan[0]["at"]["point"] == "net.gw_sim.answer"
    spec = Episode(3, stale, 7).spec(SCENARIO)
    assert spec.plan is not None
    assert [entry["fault"] for entry in spec.plan] == ["F2", "F10"]
    assert spec.scenario.params.status_lag_ms == 3_000, "0006, notes"


# Checks before anything runs -----------------------------------------------------------------


def study_of(selectors: list[str], scenario: Any = SCENARIO) -> Study:
    cells = select_cells(EXPECTED.cells, selectors, configs=["G2"], contracts=list(ALL_LEVELS))
    return Study(scenario, study_episodes(cells, (7,)), SCRIPTED, EXPECTED)


def test_a_study_must_fit_its_scenario_and_name_valid_databases(
    dev_data: dict[str, object],
) -> None:
    check_study(study_of(["all"]), "fivefold_study")
    with pytest.raises(ValueError, match="database names"):
        check_study(study_of(["F2"]), "Fivefold-Study")
    with pytest.raises(ValueError, match="database names"):
        check_study(study_of(["F2"]), "f" * 59)
    dev_data["contracts"] = ["C2"]
    with pytest.raises(ValueError, match="dev-001-single-po runs at C2, not at C0"):
        check_study(study_of(["F2"], scenario_from_data(dev_data)), "fivefold_study")
    dev_data["contracts"] = ["C0", "C1", "C2", "C3"]
    dev_data["params"] = {"commit_deadline_ms": 30_000}
    with pytest.raises(ValueError, match=r"episode F5-beyond_deadline-C0-G2-seed-7: .*above D"):
        check_study(study_of(["F5"], scenario_from_data(dev_data)), "fivefold_study")


def test_a_cell_with_a_fault_runs_only_on_the_scenario_its_plan_is_written_for() -> None:
    """The cells' plans target dev-001's order and quote; another scenario may have
    neither, so a fault there could fail partway through the study."""
    other = load_scenario_file(DEV_002)
    check_study(study_of(["none"], other), "fivefold_study")
    for selectors in (["F7"], ["all"], ["none", "F2"]):
        with pytest.raises(ValueError, match="written for dev-001-single-po, not dev-002"):
            check_study(study_of(selectors, other), "fivefold_study")


def test_the_output_directory_must_be_new_or_empty(tmp_path: Path) -> None:
    check_out(tmp_path / "new")
    check_out(tmp_path)
    (tmp_path / "old.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="not a new or empty directory"):
        check_out(tmp_path)
    with pytest.raises(ValueError, match="not a new or empty directory"):
        check_out(tmp_path / "old.json")


# The command refuses before it touches a database --------------------------------------------


@pytest.fixture
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a database connection."""

    def connect(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("the command touched a database")

    monkeypatch.setattr(psycopg, "connect", connect)


@pytest.fixture
def prices(tmp_path: Path) -> Path:
    path = tmp_path / "prices.yaml"
    path.write_text(
        "price_date: 2026-11-15\nprices:\n  ollama:\n    'priced:1b':\n"
        "      input_usd_per_million_tokens: 0\n      output_usd_per_million_tokens: 0\n",
        encoding="utf-8",
    )
    return path


def refused(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """Run the command, expect it to refuse with one line, and return the line."""
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("study failed: ") and captured.err.count("\n") == 1
    return captured.err


def test_the_command_refuses_a_live_study_before_it_touches_a_database(
    tmp_path: Path,
    prices: Path,
    no_database: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No flag, a cap of 0, an unpriced model, an unknown provider or parameter: each is
    refused with one line, and nothing is written."""
    out = tmp_path / "study"
    live = ["--live", "ollama:priced:1b", "--spend-cap-usd", "0.50", "--prices", str(prices)]
    args = ["--out", str(out), "--cells", "F2-C2-G2", *live]

    def changed(old: str, new: str) -> list[str]:
        return [new if arg == old else arg for arg in args]

    monkeypatch.delenv("FIVEFOLD_LIVE", raising=False)
    assert "live runs are off: set FIVEFOLD_LIVE=1" in refused(args, capsys)
    monkeypatch.setenv("FIVEFOLD_LIVE", "yes")
    assert "live runs are off" in refused(args, capsys)
    monkeypatch.setenv("FIVEFOLD_LIVE", "1")
    cases = [
        (changed("0.50", "0"), "above 0 USD"),
        (changed("ollama:priced:1b", "ollama:unpriced:1b"), "no price for ollama:unpriced:1b"),
        (changed("ollama:priced:1b", "nobody:m"), "no live provider 'nobody'"),
        ([*args, "--param", "base_url=http://x"], "not base_url"),
        (changed(str(prices), str(tmp_path / "missing.yaml")), "missing.yaml"),
    ]
    for argv, message in cases:
        assert message in refused(argv, capsys)
    assert not out.exists()


def test_the_command_needs_a_cap_and_prices_with_a_live_model(
    tmp_path: Path, prices: Path, no_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--out", str(tmp_path / "study"), "--live", "ollama:priced:1b"]
    for extra in (["--prices", str(prices)], ["--spend-cap-usd", "1"]):
        with pytest.raises(SystemExit) as exit_:
            main([*args, *extra])
        assert exit_.value.code == 2
        assert "--live needs --spend-cap-usd and --prices" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--out", str(tmp_path / "study"), "--spend-cap-usd", "1"])
    assert "go with --live" in capsys.readouterr().err
    assert not (tmp_path / "study").exists()


def test_the_command_refuses_a_study_it_could_not_run_before_it_touches_a_database(
    tmp_path: Path, no_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "study"
    cases = [
        (["--cells", "F99"], "no cell 'F99'"),
        (["--cells", "F10", "--configs", "B0"], "no cell 'F10' in B0"),
        (["--episodes", "0"], "at least 1 episode"),
        (["--seed", "-1"], "seeds"),
        (["--database-prefix", "Bad-Name"], "database names"),
        (["--scenario", str(tmp_path / "missing.yaml")], "missing.yaml"),
    ]
    for extra, message in cases:
        assert message in refused(["--out", str(out), *extra], capsys)
    assert not out.exists()
    out.mkdir()
    (out / "F2-C2-G2-seed-7.json").write_text("{}", encoding="utf-8")
    assert "not a new or empty directory" in refused(["--out", str(out)], capsys)
