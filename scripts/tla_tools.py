"""Install and check the pinned TLC development tool (0021)."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "1.7.3"
JAR = Path(".tla/tla2tools.jar")
URL = f"https://github.com/tlaplus/tlaplus/releases/download/v{VERSION}/{JAR.name}"
SHA256 = "ae7a33bbe99e5a3783c28d826d20e0028fc87f5a8cc8f9520afab00eabbc0bb1"
JAVA_HELP = (
    "Java 17 is required. Install Eclipse Temurin JRE 17.0.19+10, "
    "put java on PATH, then run make tla-setup."
)
JAR_HELP = "Run make tla-setup to install the verified TLC jar."


def verify_jar(jar: Path) -> None:
    with jar.open("rb") as data:
        if hashlib.file_digest(data, "sha256").hexdigest() != SHA256:
            raise ValueError("TLC jar SHA-256 mismatch.")


def install(jar: Path) -> None:
    jar.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=jar.parent) as directory:
        staged = Path(directory) / jar.name
        with urlopen(URL, timeout=60) as response, staged.open("wb") as out:
            shutil.copyfileobj(response, out)
        verify_jar(staged)
        staged.replace(jar)


def check_java() -> None:
    try:
        version = subprocess.run(
            ["java", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(JAVA_HELP) from error
    java17 = re.search(r'^(?:openjdk|java) version "17(?:\.[^"]*)?"', version.stdout, re.MULTILINE)
    if version.returncode or java17 is None:
        raise ValueError(JAVA_HELP)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "check"))
    args = parser.parse_args()
    if args.action == "install":
        try:
            install(JAR)
        except (OSError, ValueError) as error:
            detail = " ".join(str(error).splitlines())
            print(f"TLC jar installation failed. {detail}", file=sys.stderr)
            return 1
        return 0

    try:
        check_java()
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    try:
        verify_jar(JAR)
    except (OSError, ValueError) as error:
        print(f"{JAR_HELP} ({error})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
