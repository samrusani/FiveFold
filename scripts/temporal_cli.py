"""Provision the checksummed Temporal CLI for the deterministic test suite (0018)."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "1.10.1"
RELEASE = f"https://github.com/temporalio/cli/releases/download/v{VERSION}"
CHECKSUMS = {
    "darwin_amd64": "8ec6c513ad2177de4eb15d2cb17a4fe5070e6078afd7f80bdfcc37fae459d3c4",
    "darwin_arm64": "94f7f982b26438e8abbde6000ba471b694be6a2413b31f6162f87a9f9c48c738",
    "linux_amd64": "4c1500e2ce2d4de136e5fe8852d9b7f82eb62f19b6b383b3e0c040519a9d20b1",
    "linux_arm64": "2f748e9411b37c9cabb57f1f08e37765388c12b5534432c7aea28b593df9145d",
}
# Hash of the regular `temporal` member in each archive verified above.
EXECUTABLE_CHECKSUMS = {
    "darwin_amd64": "cd364ad328a745a0afc562b8cae64cb3ea99117825950ae87d5db7440a96362f",
    "darwin_arm64": "3aa75ebc866fc7a995a560b748546902ac294ae071121ef091ccff48d5bcd8be",
    "linux_amd64": "960afddde142595ea4bf6c74f0213b4634a4ef2ef8778c2e930af2cce30d7c8a",
    "linux_arm64": "fcd71cb3898fc91ce7c8e934d70a0a32c11d73c8955c462eaab2becf88effa1a",
}
ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def target_platform() -> str:
    machine = platform.machine().lower()
    target = f"{sys.platform}_{ARCHITECTURES.get(machine, machine)}"
    if target not in CHECKSUMS:
        raise ValueError(f"Temporal CLI {VERSION} has no pinned archive for {target}")
    return target


def check(binary: Path) -> None:
    expected = EXECUTABLE_CHECKSUMS[target_platform()]
    with binary.open("rb") as data:
        if hashlib.file_digest(data, "sha256").hexdigest() != expected:
            raise ValueError("Temporal CLI executable SHA-256 mismatch")
    result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0 or result.stdout.split()[:3] != ["temporal", "version", VERSION]:
        raise ValueError(f"expected Temporal CLI {VERSION}")


def install(binary: Path) -> None:
    target = target_platform()
    checksum = CHECKSUMS[target]
    archive_name = f"temporal_cli_{VERSION}_{target}.tar.gz"
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=binary.parent) as directory:
        archive = Path(directory) / archive_name
        with (
            urlopen(f"{RELEASE}/{archive_name}", timeout=60) as response,
            archive.open("wb") as out,
        ):
            shutil.copyfileobj(response, out)
        with archive.open("rb") as downloaded:
            if hashlib.file_digest(downloaded, "sha256").hexdigest() != checksum:
                raise ValueError(f"checksum mismatch for {archive_name}")
        staged = Path(directory) / "temporal"
        with tarfile.open(archive, "r:gz") as package:
            member = package.getmember("temporal")
            if not member.isfile():
                raise ValueError("the pinned archive lacks a regular temporal executable")
            source = package.extractfile(member)
            if source is None:
                raise ValueError("the Temporal executable could not be read")
            with source, staged.open("wb") as out:
                shutil.copyfileobj(source, out)
        staged.chmod(0o755)
        check(staged)
        staged.replace(binary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "check"))
    args = parser.parse_args()
    binary = Path(os.environ.get("FIVEFOLD_TEMPORAL_CLI", ".temporal/temporal")).absolute()
    try:
        if args.action == "install":
            install(binary)
        else:
            check(binary)
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(
            f"Temporal CLI {VERSION} is required. Run make temporal-setup. ({error})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
