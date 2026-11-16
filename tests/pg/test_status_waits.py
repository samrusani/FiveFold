"""Status waits end within the run's `max_wait_ms` (0005), in B0 and in G modes.

Record 0005 sets the longest `wait_ms` a status tool accepts: 60,000 virtual ms, unless
the scenario's `max_wait_ms` says otherwise. B0's `get_approval_status` refuses a longer
wait before it sends anything, as it refuses any other invalid argument: the tool node
gives the model an error message. The simulator's endpoint, which asks again every
`approval_poll_interval_ms` (500) while an approval is pending, refuses one too, so one
wait makes at most 1 + 60,000 / 500 = 121 lookups, each an `approval_status` event. The
gateway's `get_action_status` refuses a wait above the run's value as well. With every
tool call bounded, a worker's step budget bounds its work (0009).

The approvals here are queued for an approver with no rules, so they stay pending for
the whole run (the case `test_approver.py` covers).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any, LiteralString

import psycopg
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr

from fivefold.agents.tools import Timeouts, baseline_tools
from fivefold.agents.worker import build_graph
from fivefold.domain.api import RequestApproval
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import scenario_from_data
from fivefold.gateway.tools import ToolError
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.rng import Generators
from fivefold.harness.run import Config, InprocRun, RunSpec
from fivefold.harness.scheduler import ComponentFailed, Scheduler
from fivefold.harness.transport import Transport
from fivefold.sim import Simulator
from fivefold.sim.endpoint import endpoint

pytestmark = pytest.mark.pg

SCOPE: dict[str, object] = {
    "supplier_id": "S-1",
    "sku": "W-100",
    "max_quantity": 100,
    "max_amount_minor": 54_000,
    "currency": "USD",
    "cost_center": "CC-OPS",
}
REQUEST: dict[str, object] = {
    "intent_id": "REQ-1",
    "operation": "submit_purchase_order",
    "scope": SCOPE,
}
LOOKUPS_PER_WAIT = 1 + 60_000 // 500
"""One lookup as the request arrives, then one every 500 ms until the wait ends."""


def queued(dev_data: dict[str, Any], max_wait_ms: int | None = None) -> dict[str, Any]:
    """dev-001 with no fault, every order queued, and an approver that never decides."""
    dev_data["authority"]["auto_approve_up_to_minor"] = 0
    dev_data["authority"]["approver"] = []
    dev_data["faults"] = []
    if max_wait_ms is not None:
        dev_data["params"]["max_wait_ms"] = max_wait_ms
    return dev_data


def lookups(dsn: str) -> tuple[Any, ...]:
    """How many approval lookups the simulator served, and the first and last one's time."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT count(*), min(vt), max(vt) FROM sim.event WHERE kind = 'approval_status'"
        ).fetchone()
    assert row is not None
    return tuple(row)


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


class Script(BaseChatModel):
    """A model that makes the given tool calls, one per reply, then says it is done."""

    _calls: list[tuple[str, dict[str, object]]] = PrivateAttr()

    def __init__(self, calls: Sequence[tuple[str, dict[str, object]]]) -> None:
        super().__init__()
        self._calls = list(calls)

    @property
    def _llm_type(self) -> str:
        return "script"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        made = sum(1 for message in messages if isinstance(message, AIMessage))
        if made < len(self._calls):
            name, args = self._calls[made]
            call = {"name": name, "args": args, "id": f"call-{made + 1}"}
            reply = AIMessage("", tool_calls=[call])
        else:
            reply = AIMessage("Done.")
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)


def b0_worker(
    sim: Simulator, calls: Sequence[tuple[str, dict[str, object]]], *, max_wait_ms: int
) -> tuple[list[ToolMessage], MemoryHarnessLog]:
    """Run a B0 worker whose model makes `calls` through B0's tools and the worker's
    graph, against `sim` on the transport. Return the tool results the model got, and
    the harness log of the messages sent."""
    scheduler = Scheduler()
    log = MemoryHarnessLog()
    transport = Transport(scheduler, log, Generators(7).get("latency"), max_hop_latency_ms=50)
    transport.serve("simulator", endpoint(sim, scheduler, max_wait_ms=max_wait_ms))
    timeouts = Timeouts(t_ms=2_000, tool_wait_ms=2_500, max_wait_ms=max_wait_ms)
    tools = baseline_tools(transport, scheduler, timeouts, worker="agent-1", status=False)
    graph = build_graph(Script(calls), tools, InMemorySaver(), max_steps=10)
    final: list[BaseMessage] = []

    async def worker() -> None:
        start = {"messages": [HumanMessage("Wait for the approval.")]}
        state = await graph.ainvoke(start, {"configurable": {"thread_id": "agent-1"}})
        final.extend(state["messages"])

    async def main() -> None:
        scheduler.spawn("agent-1", worker(), name="agent")
        await scheduler.run(until=lambda: bool(final))

    asyncio.run(main())
    return [message for message in final if isinstance(message, ToolMessage)], log


def test_a_b0_wait_above_the_maximum_is_refused_like_any_invalid_argument_and_sends_nothing(
    make_sim: Callable[..., Simulator], dev_data: dict[str, Any], sim_database: str
) -> None:
    sim = make_sim(Contract.C2, data=queued(dev_data), credentials=("agent-1",))
    requested, too_long, negative = b0_worker(
        sim,
        [
            ("request_approval", REQUEST),
            ("get_approval_status", {"approval_id": "APR-0001", "wait_ms": 60_001}),
            ("get_approval_status", {"approval_id": "APR-0001", "wait_ms": -1}),
        ],
        max_wait_ms=60_000,
    )[0]
    assert json.loads(str(requested.content))["status"] == "pending"
    for refused in (too_long, negative):
        assert refused.status == "error"
        assert str(refused.content).startswith("Error invoking tool 'get_approval_status'")
    assert "wait_ms: Value error, the run's max_wait_ms is 60000" in str(too_long.content)
    assert "wait_ms: Input should be greater than or equal to 0" in str(negative.content)
    assert lookups(sim_database) == (0, None, None), "nothing reached the simulator"


def test_a_b0_wait_at_the_maximum_on_a_pending_approval_makes_121_lookups(
    make_sim: Callable[..., Simulator], dev_data: dict[str, Any], sim_database: str
) -> None:
    sim = make_sim(Contract.C2, data=queued(dev_data), credentials=("agent-1",))
    results, log = b0_worker(
        sim,
        [
            ("request_approval", REQUEST),
            ("get_approval_status", {"approval_id": "APR-0001", "wait_ms": 60_000}),
        ],
        max_wait_ms=60_000,
    )
    waited = json.loads(str(results[1].content))
    assert (waited["approval_id"], waited["status"]) == ("APR-0001", "pending")
    count, first, last = lookups(sim_database)
    assert (count, last - first) == (LOOKUPS_PER_WAIT, 60_000)
    answers = [m for m in log.messages if (m.api, m.kind) == ("get_approval_status", "answer")]
    assert [answer.fate for answer in answers] == ["delivered"], "the answer beat the timeout"


def test_the_simulator_s_endpoint_never_polls_past_the_run_s_maximum(
    make_sim: Callable[..., Simulator], dev_data: dict[str, Any], sim_database: str
) -> None:
    """A wait above the maximum is a body that does not parse: the endpoint raises, which
    fails the run loudly, and looks nothing up. B0's tool refuses such a wait first, so
    only a bug can send one."""
    sim = make_sim(Contract.C2, data=queued(dev_data), credentials=("agent-1",))
    sim.request_approval(RequestApproval.model_validate(REQUEST), caller="agent-1", vt=0)

    def ask(wait_ms: int) -> list[object]:
        scheduler = Scheduler()
        transport = Transport(
            scheduler, MemoryHarnessLog(), Generators(7).get("latency"), max_hop_latency_ms=50
        )
        transport.serve("simulator", endpoint(sim, scheduler, max_wait_ms=1_000))
        answers: list[object] = []

        async def caller() -> None:
            body = {"approval_id": "APR-0001", "wait_ms": wait_ms}
            reply = await transport.call(
                hop="agent_sim",
                sender="agent-1",
                receiver="simulator",
                api="get_approval_status",
                body=body,
                timeout_ms=wait_ms + 2_000,
            )
            answers.append(reply.body)

        async def main() -> None:
            scheduler.spawn("agent-1", caller(), name="caller")
            await scheduler.run()

        asyncio.run(main())
        return answers

    (answer,) = ask(1_000)
    assert isinstance(answer, dict) and answer["status"] == "pending"
    assert lookups(sim_database)[0] == 1 + 1_000 // 500
    with pytest.raises(ComponentFailed, match="the run's max_wait_ms is 1000"):
        ask(1_001)
    assert lookups(sim_database)[0] == 1 + 1_000 // 500, "the refused wait looked nothing up"


def run(dsn: str, data: dict[str, Any], config: Config) -> InprocRun:
    spec = RunSpec(scenario_from_data(data), config, Contract.C2, 7)
    inproc = InprocRun(spec, dsn)
    asyncio.run(inproc.execute())
    return inproc


@pytest.mark.parametrize(("max_wait_ms", "made"), [(60_000, 3 * LOOKUPS_PER_WAIT), (5_000, 3 * 11)])
def test_b0_s_scripted_waits_are_bounded_by_the_scenario_s_maximum(
    run_database: Callable[[str], str], dev_data: dict[str, Any], max_wait_ms: int, made: int
) -> None:
    """The policy waits at most 3 times, using the scenario's limit (0015)."""
    dsn = run_database(f"b0_wait_{max_wait_ms}")
    run(dsn, queued(dev_data, max_wait_ms), "B0")
    assert lookups(dsn)[0] == made
    sent = rows(dsn, "SELECT count(*) FROM harness.message_log WHERE api = 'get_approval_status'")
    assert sent == [(2 * 3,)], "each wait sent is a request and its answer"
    (report,) = rows(dsn, "SELECT report FROM harness.final_report WHERE valid")
    assert [claim["status"] for claim in report[0]["claims"]] == ["not_done"]


def test_the_gateway_s_status_tool_takes_the_scenario_s_maximum(
    run_database: Callable[[str], str], dev_data: dict[str, Any]
) -> None:
    """The scenario's `max_wait_ms` reaches the gateway's tools. Their schema keeps
    0005's 60,000, so the run's value is checked when a call is validated."""
    dev_data["faults"] = []
    dev_data["params"]["max_wait_ms"] = 5_000
    inproc = run(run_database("g2_wait_limit"), dev_data, "G2")
    arguments = {"action_id": "A-0001", "wait_ms": 5_001}
    with pytest.raises(ToolError) as refused:
        asyncio.run(inproc.tools.call("get_action_status", arguments, worker="agent-1"))
    assert (refused.value.code, refused.value.message) == (
        "invalid_args",
        "wait_ms: Value error, the run's max_wait_ms is 5000",
    )


@pytest.mark.parametrize("config", ["B0", "G2"])
def test_dev_002_with_short_wait_completes(
    run_database: Callable[[str], str], config: Config
) -> None:
    from fivefold.domain.scenario import load_scenario_file
    from fivefold.paths import DEV_002

    scenario = load_scenario_file(DEV_002)
    params = scenario.params.model_copy(update={"max_wait_ms": 5_000})
    scenario = scenario.model_copy(update={"params": params})
    dsn = run_database(f"short_wait_{config.lower()}")
    inproc = InprocRun(RunSpec(scenario, config, Contract.C2, 7), dsn)
    asyncio.run(inproc.execute())
    assert inproc.result.evaluation is not None
    assert str(inproc.result.evaluation.verdict) == "pass"
    if config == "B0":
        waits = rows(
            dsn,
            "SELECT body->>'wait_ms' FROM harness.message_log"
            " WHERE api = 'get_approval_status' AND kind = 'request'",
        )
    else:
        waits = rows(
            dsn,
            "SELECT args->>'wait_ms' FROM gw.tool_call"
            " WHERE tool = 'get_action_status' AND args->>'wait_ms' <> '0'",
        )
    assert waits and all(wait == "5000" for (wait,) in waits)
