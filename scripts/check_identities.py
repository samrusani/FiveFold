"""Refuse commits whose author or committer could expose a personal identity.

Usage: python3 scripts/check_identities.py BASE..HEAD

Every author must use a GitHub user noreply address. A committer may also use GitHub's
own noreply address. A commit under the maintainer's noreply address must carry the
maintainer's GitHub username as its name (CLAUDE.md, "Commits and attribution"). CI runs
this on every pull request. The report names each commit and the field at fault, never
the value, so a public log cannot repeat what it found.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence

NOREPLY = re.compile(r"(?:\d+\+)?[A-Za-z0-9-]+@users\.noreply\.github\.com")
"""A GitHub noreply address, with or without the account id prefix."""

GITHUB = "noreply@github.com"
"""The committer of commits GitHub makes itself, such as edits in its web editor."""

MAINTAINER = "samrusani"
MAINTAINER_EMAIL = re.compile(r"(?:\d+\+)?samrusani@users\.noreply\.github\.com")


def problems(name: str, email: str, role: str) -> list[str]:
    """What is wrong with one identity, `role` being "author" or "committer"."""
    found: list[str] = []
    email = email.lower()
    if not NOREPLY.fullmatch(email) and (role != "committer" or email != GITHUB):
        found.append(f"{role} email is not a GitHub noreply address")
    if MAINTAINER_EMAIL.fullmatch(email) and name != MAINTAINER:
        found.append(f"{role} name is not {MAINTAINER}, for the maintainer's noreply address")
    return found


def check(revisions: str) -> list[str]:
    """One line per problem in the commits `git log revisions` lists."""
    listed = subprocess.run(
        ["git", "log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", "--end-of-options", revisions],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    report: list[str] = []
    for line in listed.splitlines():
        sha, author, author_email, committer, committer_email = line.split("\x00")
        found = problems(author, author_email, "author")
        found += problems(committer, committer_email, "committer")
        report.extend(f"{sha[:12]}: {problem}" for problem in found)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: check_identities.py BASE..HEAD", file=sys.stderr)
        return 2
    report = check(args[0])
    for line in report:
        print(line)
    if report:
        print(f"{len(report)} problem(s): commits must use GitHub noreply addresses.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
