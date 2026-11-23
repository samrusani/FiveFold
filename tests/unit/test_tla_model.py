"""Checks that the readable TLA+ edge table tracks the Python machine (0021)."""

from pathlib import Path
from runpy import run_path

import pytest

from fivefold.gateway import state_machine as machine

MODEL = Path(__file__).resolve().parents[2] / "tla"


def test_a_python_edge_removal_is_detected_before_running_tlc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_tables = run_path(str(MODEL / "check_model.py"))["check_tables"]
    table = MODEL / "GatewayTables.tla"
    assert check_tables(table) == table.read_bytes()

    # Simulate deleting T13 in Python and leaving the checked-in TLA+ table stale.
    monkeypatch.setattr(
        machine, "TRANSITIONS", tuple(t for t in machine.TRANSITIONS if t.tid != "T13")
    )
    with pytest.raises(ValueError, match=r"differs from Python.*make tla-tables"):
        check_tables(table)
