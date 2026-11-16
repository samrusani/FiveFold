"""Paths to the runtime data in a checkout or an installed distribution."""

from __future__ import annotations

import atexit
from contextlib import ExitStack
from importlib.resources import as_file, files
from pathlib import Path

# The wheel contains one data directory. Keep its extraction alive for callers that
# need real paths, including when the package is imported from an archive.
_resources = files("fivefold").joinpath("_data")
_stack = ExitStack()
atexit.register(_stack.close)
if _resources.is_dir():
    _data = _stack.enter_context(as_file(_resources))
else:
    # Editable installs use the single copies at the repository root.
    _data = Path(__file__).resolve().parents[2]

DB_DIR = _data / "db"
DEV_001 = _data / "scenarios" / "dev" / "dev-001-single-po.yaml"
DEV_002 = _data / "scenarios" / "dev" / "dev-002-human-approval.yaml"
EXPECTED = _data / "faults" / "expected.yaml"
