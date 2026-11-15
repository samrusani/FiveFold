"""The names of the test databases: every prefix FIVEFOLD_TEST_DB_PREFIX accepts works.

The prefix may have 40 characters, and `run_database` names each database after the
template, `PREFIX_simtpl`, and the test's suffix. PostgreSQL keeps 63 bytes of a name,
and the harness refuses a longer one before it reaches the server
(`fivefold.harness.migrations.check_database_name`). So a name that would be longer keeps
the start of its suffix and ends with a short hash of it.
"""

from __future__ import annotations

from collections.abc import Callable

from fivefold.harness.cells import load_expected
from fivefold.harness.migrations import check_database_name

LONGEST_PREFIX = "p" * 40
"""As long as FIVEFOLD_TEST_DB_PREFIX may be."""

LONGEST_SUFFIX = "f10_stale_within_lag_c1_g2_play"
"""The longest suffix the suite gives `run_database`: test_playback.py replays the F10
cell under it."""

Namer = Callable[[str, str], str]


def test_the_longest_prefix_and_suffix_give_a_name_postgres_keeps_whole(
    run_database_name: Namer,
) -> None:
    name = run_database_name(f"{LONGEST_PREFIX}_simtpl", LONGEST_SUFFIX)
    check_database_name(name)
    assert len(name) == 63
    assert name.startswith(f"{LONGEST_PREFIX}_simtpl_f10_st_")
    assert name == run_database_name(f"{LONGEST_PREFIX}_simtpl", LONGEST_SUFFIX)


def test_a_name_that_fits_keeps_its_whole_suffix(run_database_name: Namer) -> None:
    name = run_database_name("fivefold_test_simtpl", LONGEST_SUFFIX)
    assert name == f"fivefold_test_simtpl_{LONGEST_SUFFIX}"


def test_each_suffix_of_the_cells_keeps_a_name_of_its_own_under_the_longest_prefix(
    run_database_name: Namer,
) -> None:
    """test_expected.py names a database after each cell, and test_playback.py adds
    `_rec` and `_play`."""
    cells = sorted({cell.name.lower().replace("-", "_") for cell in load_expected().cells})
    suffixes = [*cells, *(f"{cell}_rec" for cell in cells), *(f"{cell}_play" for cell in cells)]
    names = [run_database_name(f"{LONGEST_PREFIX}_simtpl", suffix) for suffix in suffixes]
    for name in names:
        check_database_name(name)
    assert len(set(names)) == len(suffixes)
