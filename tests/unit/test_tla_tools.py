"""Record 0021 requires hard failures for unavailable TLC prerequisites."""

import io
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/tla_tools.py"
JAVA_HELP = (
    "Java 17 is required. Install Eclipse Temurin JRE 17.0.19+10, "
    "put java on PATH, then run make tla-setup."
)
JAR_HELP = "Run make tla-setup to install the verified TLC jar."


@pytest.mark.parametrize(
    ("java_version", "jar_contents", "message"),
    [
        (None, None, JAVA_HELP),
        ("25.0.2", None, JAVA_HELP),
        ("17.0.19", None, JAR_HELP),
        ("17.0.19", b"wrong jar", JAR_HELP),
    ],
    ids=["missing-java", "wrong-java", "missing-jar", "invalid-jar"],
)
def test_prerequisite_errors(
    tmp_path: Path, java_version: str | None, jar_contents: bytes | None, message: str
) -> None:
    if java_version:
        java = tmp_path / "java"
        java.write_text(f"""#!/bin/sh
echo 'openjdk version "{java_version}"' >&2
""")
        java.chmod(0o755)
    command = tmp_path / "scripts/tla_tools.py"
    command.parent.mkdir()
    shutil.copyfile(SCRIPT, command)
    if jar_contents:
        jar = tmp_path / ".tla/tla2tools.jar"
        jar.parent.mkdir()
        jar.write_bytes(jar_contents)
    result = subprocess.run(
        [sys.executable, str(command), "check"],
        cwd=command.parent,
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert message in result.stderr


def test_tools_resolve_jar_from_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = runpy.run_path(str(SCRIPT))
    assert config["JAR"] == SCRIPT.parents[1] / ".tla/tla2tools.jar"


def test_direct_model_check_refuses_an_unverified_jar(tmp_path: Path) -> None:
    scripts, model, tools = (tmp_path / name for name in ("scripts", "tla", ".tla"))
    for directory in (scripts, model, tools):
        directory.mkdir()
    shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
    command = model / "check_model.py"
    shutil.copyfile(SCRIPT.parents[1] / "tla/check_model.py", command)
    (tools / "tla2tools.jar").write_bytes(b"unverified")
    java = tmp_path / "java"
    java.write_text("""#!/bin/sh\necho 'openjdk version "17.0.19"' >&2\n""")
    java.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(command), "ci"],
        cwd=model,
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "TLC jar SHA-256 mismatch" in result.stderr


@pytest.mark.parametrize(
    ("contents", "message"),
    [(None, "download failed connection closed"), (b"wrong jar", "TLC jar SHA-256 mismatch")],
    ids=["download", "checksum"],
)
def test_install_reports_its_own_error(
    contents: bytes | None,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def download(*args: object, **kwargs: object) -> io.BytesIO:
        if contents is None:
            raise OSError("download failed\nconnection closed")
        return io.BytesIO(contents)

    monkeypatch.chdir(tmp_path)
    command = tmp_path / "scripts/tla_tools.py"
    command.parent.mkdir()
    shutil.copyfile(SCRIPT, command)
    monkeypatch.setattr(sys, "argv", [str(command), "install"])
    monkeypatch.setattr("urllib.request.urlopen", download)
    with pytest.raises(SystemExit) as status:
        runpy.run_path(str(command), run_name="__main__")
    assert status.value.code == 1
    stderr = capsys.readouterr().err
    assert stderr.startswith("TLC jar installation failed.")
    assert message in stderr and stderr.count("\n") == 1
    assert JAR_HELP not in stderr
    assert not (tmp_path / ".tla/tla2tools.jar").exists()
