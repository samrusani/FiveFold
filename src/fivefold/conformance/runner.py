"""Exercise record 0019 at the handoff from the SDK to a simulated caller."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path

import httpx2
from mcp import Client, ClientSession, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult
from pydantic import JsonValue, ValidationError

from fivefold.conformance.fixture import (
    EFFECT,
    NAMESPACE,
    OPERATION,
    Case,
    Fixture,
    Hooks,
    Probe,
    Stdio,
    Tool,
    render,
)
from fivefold.harness.clock import wall_ms, within
from fivefold.harness.rng import Generators, credential


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


class Inconclusive(Exception):
    """Required evidence did not arrive."""


class TestError(Exception):
    """The fixture or its probe returned invalid evidence."""


@dataclass
class Evidence:
    writes_sent: int = 0
    replies_dropped: int = 0
    initial_count: int | None = None
    count_before_retry: int | None = None
    settled_count: int | None = None
    caller_wait_ms: int | None = None
    events: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CaseResult:
    case: Case
    verdict: Verdict
    reason: str
    protocol_version: str | None
    evidence: Evidence


@dataclass(frozen=True)
class Report:
    fivefold_version: str
    sdk_version: str
    fixture_version: str
    transport: str
    tool: str
    seed: int
    retry_identity: str
    cases: list[CaseResult]

    @property
    def exit_code(self) -> int:
        verdicts = {row.verdict for row in self.cases}
        if Verdict.FAIL in verdicts:
            return 1
        if Verdict.ERROR in verdicts or Verdict.INCONCLUSIVE in verdicts:
            return 2
        return 0 if Verdict.PASS in verdicts else 2


async def run(fixture: Fixture, seed: int) -> Report:
    generator = Generators(seed).get("mcp_conformance")
    cases = list(Case)
    generator.shuffle(cases)
    namespace = credential()
    results = []
    for case in cases:
        values: dict[str, JsonValue] = {
            NAMESPACE: f"{namespace}-{case}",
            OPERATION: f"{generator.getrandbits(128):032x}",
            EFFECT: fixture.expected_effect,
        }
        results.append(await run_case(fixture, case, values))
    return Report(
        version("fivefold"),
        version("mcp"),
        fixture.version,
        fixture.transport.kind,
        fixture.write.name,
        seed,
        fixture.retry_identity,
        results,
    )


async def run_case(fixture: Fixture, case: Case, values: dict[str, JsonValue]) -> CaseResult:
    evidence = Evidence()
    if case not in fixture.supported_cases:
        return CaseResult(
            case, Verdict.NOT_APPLICABLE, "case unavailable in fixture declaration", None, evidence
        )
    protocol = None
    seconds = fixture.evidence_wait_ms / 1000
    try:
        async with asyncio.timeout(seconds) as timeout, connect(fixture, seconds) as client:
            timeout.reschedule(None)
            protocol = client.protocol_version
            attempt = Attempt(client.session, fixture, values, evidence)
            try:
                async with asyncio.timeout(seconds):
                    settled = await attempt.execute(case)
            except Inconclusive as error:
                return CaseResult(case, Verdict.INCONCLUSIVE, str(error), protocol, evidence)
            except (TimeoutError, MCPError):
                return CaseResult(
                    case,
                    Verdict.INCONCLUSIVE,
                    "required evidence did not arrive",
                    protocol,
                    evidence,
                )
            except TestError as error:
                return CaseResult(case, Verdict.ERROR, str(error), protocol, evidence)
        if settled.count == 1 and settled.matches:
            return CaseResult(case, Verdict.PASS, "one matching effect", protocol, evidence)
        if case is Case.LOST_REPLY and evidence.count_before_retry == 1 and settled.count == 2:
            reason = "a retry places a second effect"
        else:
            reason = "settled count or effect differs from the declaration"
        return CaseResult(case, Verdict.FAIL, reason, protocol, evidence)
    except TimeoutError:
        return CaseResult(case, Verdict.ERROR, "fixture connection timed out", protocol, evidence)
    except Exception:
        # Transport and SDK exceptions may contain fixture arguments or reply bodies.
        return CaseResult(case, Verdict.ERROR, "fixture connection failed", protocol, evidence)


@asynccontextmanager
async def connect(fixture: Fixture, seconds: float) -> AsyncIterator[Client]:
    if isinstance(fixture.transport, Stdio):
        parameters = StdioServerParameters(
            command=fixture.transport.command, args=fixture.transport.args
        )
        with Path(os.devnull).open("w", encoding="utf-8") as stderr:
            async with Client(
                stdio_client(parameters, errlog=stderr),
                mode="auto",
                read_timeout_seconds=seconds,
            ) as client:
                yield client
    else:
        async with httpx2.AsyncClient(timeout=seconds, trust_env=False) as http:
            transport = streamable_http_client(fixture.transport.url, http_client=http)
            async with Client(transport, mode="auto", read_timeout_seconds=seconds) as client:
                yield client


@dataclass
class Attempt:
    session: ClientSession
    fixture: Fixture
    values: dict[str, JsonValue]
    evidence: Evidence

    async def execute(self, case: Case) -> Probe:
        initial = await self.probe()
        self.evidence.initial_count = initial.count
        if initial.count != 0:
            raise TestError("the case namespace already contains effects")
        if case is Case.PRE_COMMIT:
            hooks = self.fixture.pre_commit
            assert hooks is not None  # Required by fixture validation
            await self.pre_commit(hooks)
        else:
            first_reply = await self.write()
            self.evidence.events.append("first_reply_received")
            if first_reply.is_error:
                raise TestError("fixture write returned an MCP error")
            if case is Case.LOST_REPLY:
                before = await self.probe()
                while before.count == 0:
                    await pause(10)
                    before = await self.probe()
                self.evidence.count_before_retry = before.count
                self.evidence.events.append("commit_witnessed")
            del first_reply
            self.evidence.replies_dropped += 1
            self.evidence.events.append("first_reply_dropped")
            if case is Case.LOST_REPLY:
                await self.expire_caller()

        await self.write()
        self.evidence.events.append("second_reply_received")
        observed = await self.probe()
        while not self.fixture.terminal_replies_settle and observed.settled is not True:
            await pause(10)
            observed = await self.probe()
        self.evidence.settled_count = observed.count
        return observed

    async def pre_commit(self, hooks: Hooks) -> None:
        if not await self.flag(hooks.arm, "armed"):
            raise TestError("fixture did not arm its pre-commit hook")
        first = asyncio.create_task(self.write())
        try:
            while not await self.flag(hooks.held, "held"):
                if first.done():
                    raise Inconclusive("the first request ended before the hold was witnessed")
                await pause(10)
            self.evidence.events.append("first_attempt_held")
            await self.expire_caller()
            before = await self.probe()
            self.evidence.count_before_retry = before.count
            if before.count != 0 or first.done() or not await self.flag(hooks.held, "held"):
                raise Inconclusive("the first attempt was not held with zero effects")
            if not await self.flag(hooks.abort, "aborted"):
                raise Inconclusive("the hook did not establish that the first attempt ended")
            await first
            if (await self.probe()).count != 0:
                raise Inconclusive("the first attempt committed before the retry")
            self.evidence.events.append("first_attempt_ended_without_commit")
        finally:
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    async def expire_caller(self) -> None:
        started = wall_ms()
        await pause(self.fixture.caller_wait_ms)
        self.evidence.caller_wait_ms = round(wall_ms() - started)
        self.evidence.events.append("caller_wait_expired")

    async def write(self) -> CallToolResult:
        self.evidence.writes_sent += 1
        return await self.call(self.fixture.write, key=self.fixture.key_argument)

    async def probe(self) -> Probe:
        try:
            result = await self.call(self.fixture.probe)
            if result.is_error:
                raise TestError("probe returned an error")
            return Probe.model_validate(result.structured_content)
        except (ValidationError, MCPError):
            raise TestError("probe did not return valid structured evidence") from None

    async def flag(self, tool: Tool, name: str) -> bool:
        result = await self.call(tool)
        payload = result.structured_content
        if result.is_error or not isinstance(payload, dict) or type(payload.get(name)) is not bool:
            raise TestError("fixture hook did not return valid structured evidence")
        return payload[name] is True

    async def call(self, tool: Tool, *, key: str | None = None) -> CallToolResult:
        arguments = {name: render(value, self.values) for name, value in tool.arguments.items()}
        if key is not None:
            arguments[key] = f"{self.values[NAMESPACE]}/{self.values[OPERATION]}"
        return await self.session.call_tool(tool.name, arguments)


async def pause(milliseconds: int) -> None:
    """Let a simulated caller's wait expire without abandoning its SDK exchange."""
    try:
        await within(asyncio.Event().wait(), milliseconds / 1000)
    except TimeoutError:
        return
