"""Record 0021 requires hard failures for unavailable TLC prerequisites."""

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
    if jar_contents:
        jar = tmp_path / ".tla/tla2tools.jar"
        jar.parent.mkdir()
        jar.write_bytes(jar_contents)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "check"],
        cwd=tmp_path,
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert message in result.stderr
