"""Checksum verification shared by the Temporal CLI setup and B1."""

import hashlib
import platform
import sys
from pathlib import Path

VERSION = "1.10.1"

# Verified the four pinned release archives, then hashed the regular `temporal`
# member from each with SHA-256. The macOS and ARM64 binaries were read as bytes
# on the same host as the Linux amd64 binary. They were not run to obtain these hashes.
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
    if target not in EXECUTABLE_CHECKSUMS:
        raise ValueError(f"Temporal CLI {VERSION} has no pinned archive for {target}")
    return target


def verify_executable(binary: Path) -> None:
    expected = EXECUTABLE_CHECKSUMS[target_platform()]
    with binary.open("rb") as data:
        if hashlib.file_digest(data, "sha256").hexdigest() != expected:
            raise ValueError("Temporal CLI executable SHA-256 mismatch")
