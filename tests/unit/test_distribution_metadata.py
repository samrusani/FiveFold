"""The wheel's project links and source-distribution parity."""

import subprocess
import zipfile
from email.parser import BytesParser
from pathlib import Path


def test_project_links_and_sdist_wheel_file_parity(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    direct = tmp_path / "direct"
    from_sdist = tmp_path / "from-sdist"
    for args in (("--wheel",), ()):
        output = direct if args else from_sdist
        subprocess.run(
            ["uv", "build", *args, "--out-dir", str(output)],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    (direct_wheel,) = direct.glob("*.whl")
    (sdist_wheel,) = from_sdist.glob("*.whl")
    with zipfile.ZipFile(direct_wheel) as first, zipfile.ZipFile(sdist_wheel) as second:
        assert set(first.namelist()) == set(second.namelist())
        (metadata_name,) = [
            name for name in first.namelist() if name.endswith(".dist-info/METADATA")
        ]
        metadata = BytesParser().parsebytes(first.read(metadata_name))
        assert set(metadata.get_all("Project-URL", [])) == {
            "Repository, https://github.com/samrusani/FiveFold",
            "Issues, https://github.com/samrusani/FiveFold/issues",
        }


def test_sdist_excludes_pilot_results(tmp_path: Path) -> None:
    """A source archive stays small even when pilot data exists in the checkout."""
    import shutil
    import tarfile

    root = Path(__file__).resolve().parents[2]
    project = tmp_path / "project"
    shutil.copytree(
        root,
        project,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".hypothesis",
            "wiki",
            "results",
            "dist",
        ),
    )
    result = project / "results" / "pilot-ollama" / "sample.json"
    result.parent.mkdir(parents=True)
    result.write_text('{"sample": true}\n', encoding="utf-8")
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(output)],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    (sdist,) = output.glob("*.tar.gz")
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
    assert any(name.endswith("/pyproject.toml") for name in names)
    assert not any("/results/" in name for name in names)
