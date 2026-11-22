"""Exercise the installed command and both synthetic servers on real SDK transports."""

from __future__ import annotations

import json
import select
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from fivefold.conformance.fixture import EFFECT, NAMESPACE, OPERATION, Case

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_conformance_server.py"
REPLY_CASES = [Case.LOST_REPLY, Case.DUPLICATE]


def tool(name: str, **arguments: object) -> dict[str, object]:
    return {"name": name, "arguments": {"namespace": NAMESPACE, **arguments}}


def declaration(transport: dict[str, object], cases: list[Case]) -> dict[str, Any]:
    fixture = {
        "version": "1",
        "transport": transport,
        "write": tool("add", operation_id=OPERATION, value="synthetic item"),
        "probe": tool("read", expected=EFFECT),
        "expected_effect": "synthetic item",
        "retry_identity": "key",
        "key_argument": "key",
        "supported_cases": cases,
        "terminal_replies_settle": True,
        "evidence_wait_ms": 3000,
        "caller_wait_ms": 20,
    }
    if Case.PRE_COMMIT in cases:
        fixture["pre_commit"] = {
            k: tool(v) for k, v in (("arm", "arm"), ("held", "holding"), ("abort", "abort"))
        }
    return fixture


@contextmanager
def connection(kind: str, server: str, audit: Path, *flags: str) -> Iterator[dict[str, object]]:
    arguments = [str(SERVER), server, "--audit", str(audit), *flags]
    if kind == "stdio":
        yield {"kind": kind, "command": sys.executable, "args": arguments}
        return
    with subprocess.Popen(
        [sys.executable, *arguments, "--http"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ) as process:
        try:
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], 10)
            assert readable, "the local fixture did not start"
            port = int(process.stdout.readline())
            yield {"kind": kind, "url": f"http://127.0.0.1:{port}/mcp"}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def command(
    tmp_path: Path, fixture: dict[str, Any], *, machine: bool = True
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "fivefold.conformance", "--fixture", str(path), "--seed", "7"]
        + (["--json"] if machine else []),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize("kind", ["stdio", "streamable_http"])
@pytest.mark.parametrize("server", ["deduplicating", "repeating"])
def test_reply_drops_need_no_fixture_hooks(kind: str, server: str, tmp_path: Path) -> None:
    audit = tmp_path / "calls.jsonl"
    with connection(kind, server, audit) as transport:
        declaration_json = declaration(transport, REPLY_CASES)
        if server == "repeating":
            declaration_json["retry_identity"] = "none"
            del declaration_json["key_argument"]
        result = command(tmp_path, declaration_json)
    passed = server == "deduplicating"
    assert result.returncode == (0 if passed else 1), result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["sdk_version"] == "2.3.0"
    rows = {row["case"]: row for row in report["cases"]}
    for case in REPLY_CASES:
        row = rows[case]
        assert row["verdict"] == ("pass" if passed else "fail")
        assert row["protocol_version"]
        evidence = row["evidence"]
        assert evidence["writes_sent"] == 2
        assert evidence["replies_dropped"] == 1
        assert evidence["settled_count"] == (1 if passed else 2)
        events = evidence["events"]
        assert events.index("first_reply_dropped") < events.index("second_reply_received")
        if case is Case.LOST_REPLY:
            assert events.index("commit_witnessed") < events.index("first_reply_dropped")
            assert events.index("first_reply_dropped") < events.index("caller_wait_expired")
        else:
            assert "caller_wait_expired" not in events
    if not passed:
        assert rows[Case.LOST_REPLY]["reason"] == "a retry places a second effect"
    assert rows[Case.PRE_COMMIT]["verdict"] == "not_applicable"
    calls = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(calls) == 4
    assert calls[0] == calls[1] and calls[2] == calls[3] and calls[0][0] != calls[2][0]
    assert calls[0][0] not in result.stdout
    assert "synthetic item" not in result.stdout


@pytest.mark.parametrize("kind", ["stdio", "streamable_http"])
@pytest.mark.parametrize("server", ["deduplicating", "repeating"])
def test_pre_commit_timeout_ends_the_held_attempt(kind: str, server: str, tmp_path: Path) -> None:
    audit = tmp_path / "calls.jsonl"
    with connection(kind, server, audit, "--hooks") as transport:
        result = command(tmp_path, declaration(transport, [Case.PRE_COMMIT]))
    assert result.returncode == 0, result.stdout + result.stderr
    rows = json.loads(result.stdout)["cases"]
    row = next(row for row in rows if row["case"] == Case.PRE_COMMIT)
    assert row["verdict"] == "pass"
    assert row["evidence"]["count_before_retry"] == 0
    assert row["evidence"]["settled_count"] == 1
    assert row["evidence"]["replies_dropped"] == 0
    assert row["evidence"]["events"][:3] == [
        "first_attempt_held",
        "caller_wait_expired",
        "first_attempt_ended_without_commit",
    ]
    calls = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.parametrize(
    ("flag", "verdict"), [("--bad-probe", "error"), ("--unsettled", "inconclusive")]
)
def test_invalid_and_missing_evidence_have_exit_two(
    flag: str, verdict: str, tmp_path: Path
) -> None:
    with connection("stdio", "deduplicating", tmp_path / "calls.jsonl", flag) as transport:
        data = declaration(transport, [Case.DUPLICATE])
        data["terminal_replies_settle"] = False
        data["evidence_wait_ms"] = 600
        result = command(tmp_path, data)
    assert result.returncode == 2, result.stdout + result.stderr
    assert verdict in {row["verdict"] for row in json.loads(result.stdout)["cases"]}
    assert "private invalid probe value" not in result.stdout + result.stderr


def test_no_applicable_case_has_exit_two(tmp_path: Path) -> None:
    result = command(
        tmp_path, declaration({"kind": "stdio", "command": "/unused"}, []), machine=False
    )
    assert result.returncode == 2
    assert result.stdout.count("not_applicable") == 3


def test_nonlocal_fixture_endpoint_is_invalid(tmp_path: Path) -> None:
    result = command(
        tmp_path,
        declaration({"kind": "streamable_http", "url": "http://192.0.2.1/mcp"}, REPLY_CASES),
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "invalid fixture declaration"
