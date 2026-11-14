"""Import boundaries between components (0007).

The evaluator reads other components' tables, never their code. The gateway reaches the
simulator only through the simulator's API. Agents reach tools only through MCP or the
baseline client.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "fivefold"

FORBIDDEN: dict[str, frozenset[str]] = {
    "evaluator": frozenset({"agents", "faults", "gateway", "harness", "recorder", "sim"}),
    "gateway": frozenset({"sim"}),
    "agents": frozenset({"gateway", "sim"}),
}
"""For each component, the fivefold subpackages it must never import."""


def package_of(path: Path) -> str:
    """Return the dotted package that relative imports in `path` resolve against.

    For `pkg/__init__.py` that is `pkg` itself; for `pkg/mod.py` it is also `pkg`.
    """
    parts = path.relative_to(SRC.parent).with_suffix("").parts
    return ".".join(parts[:-1])


def imported_components(source: str, package: str) -> set[str]:
    """Return the fivefold subpackages that `source` imports.

    `package` is the dotted package the source belongs to, for resolving relative imports.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                package_parts = package.split(".")
                base_parts = package_parts[: len(package_parts) - (node.level - 1)]
                if node.module:
                    base_parts = [*base_parts, *node.module.split(".")]
                base = ".".join(base_parts)
            targets = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        for target in targets:
            parts = target.split(".")
            if len(parts) >= 2 and parts[0] == "fivefold":
                found.add(parts[1])
    return found


@pytest.mark.parametrize("component", sorted(FORBIDDEN))
def test_component_respects_import_boundaries(component: str) -> None:
    directory = SRC / component
    assert directory.is_dir(), f"{component} is missing, so this test would check nothing"
    violations = []
    for path in sorted(directory.rglob("*.py")):
        imported = imported_components(path.read_text(encoding="utf-8"), package_of(path))
        forbidden = imported & FORBIDDEN[component]
        if forbidden:
            violations.append(f"{path.relative_to(SRC)} imports {sorted(forbidden)}")
    assert violations == []


@pytest.mark.parametrize(
    ("source", "package", "expected"),
    [
        ("import fivefold.sim.ledger", "fivefold.gateway", {"sim"}),
        ("from fivefold import sim", "fivefold.gateway", {"sim"}),
        ("from fivefold.domain import rules", "fivefold.evaluator", {"domain"}),
        ("from ..sim import ledger", "fivefold.gateway", {"sim"}),
        ("from .. import sim", "fivefold.gateway", {"sim"}),
        ("from . import dispatch", "fivefold.gateway", {"gateway"}),
        ("import json\nfrom pathlib import Path", "fivefold.gateway", set()),
    ],
)
def test_checker_resolves_imports(source: str, package: str, expected: set[str]) -> None:
    assert imported_components(source, package) == expected
