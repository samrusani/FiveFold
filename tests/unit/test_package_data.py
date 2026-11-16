"""Runtime data is available independently of the current directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from fivefold.harness import migrations


def test_runtime_data_is_present() -> None:
    from fivefold.paths import DB_DIR, DEV_001, DEV_002, EXPECTED

    assert (DB_DIR / "bootstrap.sql").is_file()
    assert list((DB_DIR / "migrations").glob("*.sql"))
    assert list((DB_DIR / "registry").glob("*.sql"))
    assert DEV_001.is_file()
    assert DEV_002.is_file()
    assert EXPECTED.is_file()


def test_migrations_default_to_runtime_data_from_another_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    seen: list[Path] = []

    def bootstrap(_dsn: str, path: Path) -> str:
        seen.append(path)
        return "r"

    monkeypatch.setattr(migrations, "bootstrap_server", bootstrap)
    assert migrations.main(["bootstrap"]) == 0
    assert seen[0].is_absolute()
    assert (seen[0] / "bootstrap.sql").is_file()


def test_wheel_contains_runtime_data_and_resolves_it_outside_the_checkout(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys
    import zipfile

    root = Path(__file__).resolve().parents[2]
    built = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(built)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = built.glob("*.whl")
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        for source in [
            root / "db" / "bootstrap.sql",
            root / "faults" / "expected.yaml",
            *sorted((root / "db" / "migrations").glob("*.sql")),
            *sorted((root / "db" / "registry").glob("*.sql")),
            *sorted((root / "scenarios" / "dev").glob("*.yaml")),
        ]:
            assert archive.read(f"fivefold/_data/{source.relative_to(root)}") == source.read_bytes()
        archive.extractall(installed)

    script = (
        "from fivefold.harness.demo import DB_DIR, DEV_001, DEV_002; "
        "from fivefold.harness.cells import load_expected; "
        "from fivefold.harness.migrations import discover; "
        "assert (DB_DIR / 'bootstrap.sql').is_file(); "
        "assert discover(DB_DIR / 'migrations'); "
        "assert discover(DB_DIR / 'registry'); "
        "assert DEV_001.is_file() and DEV_002.is_file(); "
        "assert load_expected().cells"
    )
    env = {**os.environ, "PYTHONPATH": str(installed)}
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, check=True)
