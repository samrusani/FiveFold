"""Property tests for migration discovery (0007)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fivefold.harness.migrations import MigrationError, discover

words = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8)


@given(st.lists(words, min_size=1, max_size=12))
def test_contiguous_migrations_come_back_in_number_order(words_for_files: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        # Write the files in reverse order, so order on disk cannot explain the result.
        for number, word in reversed(list(enumerate(words_for_files, start=1))):
            (directory / f"{number:04d}_{word}.sql").write_text(f"-- {number}\n", encoding="utf-8")
        found = discover(directory)
    assert [migration.number for migration in found] == list(range(1, len(words_for_files) + 1))


@given(
    st.sets(st.integers(min_value=1, max_value=60), min_size=1, max_size=12).filter(
        lambda numbers: sorted(numbers) != list(range(1, len(numbers) + 1))
    )
)
def test_any_gap_is_rejected(numbers: set[int]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        for number in numbers:
            (directory / f"{number:04d}_step.sql").write_text("SELECT 1;\n", encoding="utf-8")
        with pytest.raises(MigrationError):
            discover(directory)
