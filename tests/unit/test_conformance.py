"""Exercise the installed command and both synthetic servers on real SDK transports."""

from __future__ import annotations

import asyncio
import json
import select
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fivefold.conformance import runner
from fivefold.conformance.fixture import EFFECT, NAMESPACE, OPERATION, Case, Fixture, Probe

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


def test_help_names_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "fivefold.conformance", "--help"],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert result.stdout.splitlines()[0].startswith("usage: python -m fivefold.conformance ")


def test_the_fixture_connects_before_the_evidence_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = declaration({"kind": "stdio", "command": sys.executable}, [Case.DUPLICATE])
    raw["evidence_wait_ms"] = 600
    fixture = Fixture.model_validate(raw)
    connections: list[float] = []
    deadlines: list[float | None] = []

    @asynccontextmanager
    async def connect(source: Fixture, seconds: float) -> AsyncIterator[SimpleNamespace]:
        assert source is fixture
        connections.append(seconds)
        yield SimpleNamespace(protocol_version="test", session=object())

    def timeout(seconds: float | None) -> asyncio.Timeout:
        deadlines.append(seconds)
        return asyncio.timeout(seconds)

    async def execute(attempt: runner.Attempt, case: Case) -> Probe:
        assert case is Case.DUPLICATE
        return Probe(count=1, matches=True)

    monkeypatch.setattr(runner, "connect", connect)
    monkeypatch.setattr(runner, "asyncio", SimpleNamespace(timeout=timeout))
    monkeypatch.setattr(runner.Attempt, "execute", execute)
    result = asyncio.run(runner.run_case(fixture, Case.DUPLICATE, {}))
    assert result.verdict is runner.Verdict.PASS
    assert connections == [10.0]
    assert deadlines == [10.0, 0.6]


def test_delayed_reply_can_arrive_after_the_connection_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "CONNECTION_TIMEOUT_S", 0.8)
    with connection(
        "streamable_http", "deduplicating", tmp_path / "calls.jsonl", "--reply-delay-ms", "1000"
    ) as transport:
        fixture = Fixture.model_validate(declaration(transport, [Case.DUPLICATE]))
        result = asyncio.run(runner.run(fixture, 7))

    row = next(row for row in result.cases if row.case is Case.DUPLICATE)
    assert row.verdict is runner.Verdict.PASS, row
    assert row.evidence.settled_count == 1


def test_connection_without_a_handshake_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Fixture.model_validate(
        declaration({"kind": "stdio", "command": sys.executable}, [Case.DUPLICATE])
    )

    @asynccontextmanager
    async def stalled_connect(source: Fixture, seconds: float) -> AsyncIterator[None]:
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr(runner, "CONNECTION_TIMEOUT_S", 0.02)
    monkeypatch.setattr(runner, "connect", stalled_connect)

    async def bounded_run() -> runner.CaseResult:
        return await asyncio.wait_for(runner.run_case(fixture, Case.DUPLICATE, {}), 1)

    result = asyncio.run(bounded_run())
    assert result.verdict is runner.Verdict.ERROR
    assert result.reason == "fixture connection timed out"
    assert result.evidence.writes_sent == 0


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
def test_missing_write_tool_is_a_test_error(kind: str, tmp_path: Path) -> None:
    with connection(kind, "deduplicating", tmp_path / "calls.jsonl") as transport:
        data = declaration(transport, REPLY_CASES)
        data["write"]["name"] = "missing_tool"
        result = command(tmp_path, data)
    assert result.returncode == 2, result.stdout + result.stderr
    rows = {row["case"]: row for row in json.loads(result.stdout)["cases"]}
    for case in REPLY_CASES:
        assert rows[case]["verdict"] == "error"
        assert rows[case]["reason"] == "fixture write returned an MCP error"
        assert rows[case]["evidence"]["writes_sent"] == 1
        assert rows[case]["evidence"]["replies_dropped"] == 0


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
