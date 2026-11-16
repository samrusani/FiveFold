"""A tool call holding a NUL character in the inproc topology (0008, 0009).

PostgreSQL's JSONB cannot hold \\u0000, so the harness's log cannot store a message that
holds one, and a model can emit one in a tool call. A fake model, the scripted model with
a policy of its own, emits such calls in dev-001 on seed 7:

- in G1 and G2, a read through the gateway holding a NUL in a value or a key: the
  connection refuses its MCP message as the split gateway does, with invalid params, and
  logs nothing (`fivefold.harness.mcp_link`);
- in B0, a read and a final report holding a NUL: the tools refuse them as invalid
  arguments, since every request model refuses a NUL, so the model sees a tool error and
  reports again. This held before the connection's check too.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import psycopg
import pytest

from fivefold.agents.policy import Mode, Policy, ToolCall
from fivefold.agents.worker import WorkerEnd
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Intent, load_scenario_file
from fivefold.harness import run as inproc
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import Config, InprocRun, RunResult, RunSpec

pytestmark = pytest.mark.pg

SEED = 7
NUL = "\x00"
WORKER = "agent-1"


def run_with(
    run_database: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    policy: Callable[..., Policy],
) -> tuple[RunResult, str]:
    """Run dev-001 at C2 with no fault, the model following `policy`."""
    monkeypatch.setattr(inproc, "scripted_policy", policy)
    dsn = run_database(f"nul_{config.lower()}")
    spec = RunSpec(load_scenario_file(DEV_001), config, Contract.C2, SEED, plan=[])
    return asyncio.run(InprocRun(spec, dsn).execute()), dsn


def rows(dsn: str, query: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


ARGUMENTS = {
    "value": {"supplier_id": "S-1", "sku": f"W-100{NUL}"},
    "key": {"supplier_id": "S-1", "sku": "W-100", f"note{NUL}": "text"},
}
"""A read's arguments, holding a NUL in a value or in a key."""


@pytest.mark.parametrize(("config", "where"), [("G2", "value"), ("G2", "key"), ("G1", "value")])
def test_a_gateway_call_holding_a_nul_is_refused_with_invalid_params_and_not_logged(
    run_database: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    where: str,
) -> None:
    """The connection logged the agent's MCP message before the gateway saw it, so the
    log raised `UntranslatableCharacter` inside the agent's own send, and the worker
    ended with that error. The connection now refuses the message, with the split
    gateway's invalid params, and nothing is logged. The agent's framework retries a
    refused call as it retries any MCP error, so the worker still ends with an error."""

    def reads_with_a_nul(
        intents: Sequence[Intent], *, mode: Mode, rank: int = 0, max_wait_ms: int = 60_000
    ) -> Policy:
        yield ToolCall("get_quote", ARGUMENTS[where])

    result, dsn = run_with(run_database, monkeypatch, config, reads_with_a_nul)
    end, detail = result.ends[WORKER]
    assert end is WorkerEnd.ERROR, detail
    assert "MCPError" in detail and "holds a NUL character" in detail, detail
    assert rows(dsn, "SELECT count(*) FROM harness.message_log WHERE api = 'get_quote'") == [(0,)]


def test_a_baseline_report_holding_a_nul_is_a_tool_error_the_model_sees(
    run_database: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    refusals: dict[str, str] = {}
    """The answer to each call holding a NUL; the model replays its policy at each call."""

    def calls_with_a_nul(
        intents: Sequence[Intent], *, mode: Mode, rank: int = 0, max_wait_ms: int = 60_000
    ) -> Policy:
        refused = yield ToolCall("get_quote", {"supplier_id": "S-1", "sku": f"W-100{NUL}"})
        refusals["get_quote"] = str(refused)
        claims = [{"intent_id": intent.id, "status": "unknown"} for intent in intents]
        refused = yield ToolCall(
            "submit_final_report", {"claims": [{**claims[0], "note": f"a{NUL}b"}, *claims[1:]]}
        )
        refusals["submit_final_report"] = str(refused)
        yield ToolCall("submit_final_report", {"claims": claims})

    result, dsn = run_with(run_database, monkeypatch, "B0", calls_with_a_nul)
    end, detail = result.ends[WORKER]
    assert end is WorkerEnd.FINISHED, detail
    assert sorted(refusals) == ["get_quote", "submit_final_report"]
    for refusal in refusals.values():
        assert "Error invoking tool" in refusal and "should match pattern" in refusal, refusal
    sent = rows(dsn, "SELECT api FROM harness.message_log WHERE kind = 'request' ORDER BY seq")
    assert sent == [("submit_final_report",)]
    assert rows(dsn, "SELECT valid FROM harness.final_report") == [(True,)]
