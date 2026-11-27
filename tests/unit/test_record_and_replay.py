"""The recording wrapper and the replay adapter, outside a run (0009, Models).

The scripted policy plays dev-001 in G2 against fixed tool results: six tool calls, then
"Done.". The recording wrapper must return exactly what the scripted model returns and
record each call in order. The replay adapter must serve the recorded responses in order,
and fail with the call's number and both hashes on a request that differs, on a call the
cassette does not hold, and on recorded calls the run never asks.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from fivefold.agents.policy import Mode, scripted_policy
from fivefold.agents.replay import CassettePlayer, ReplayError, ReplayModel
from fivefold.agents.scripted import ScriptedModel
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.demo import DEV_001
from fivefold.recorder.cassette import (
    Cassette,
    CassetteError,
    ModelInfo,
    RunHeader,
    request_hash,
    request_params,
    response_message,
    tool_schemas,
)
from fivefold.recorder.recording import CassetteRecorder, RecordingModel

SCENARIO = load_scenario_file(DEV_001)
SCRIPTED = ModelInfo(adapter="scripted", name="scripted")
HEADER = RunHeader(
    run_id="unit",
    scenario_id=SCENARIO.id,
    scenario_digest="00" * 32,
    config="G2",
    contract="C2",
    seed=7,
    topology="inproc",
    params={},
)

QUOTE = {"currency": "USD", "lead_time_days": 5, "min_quantity": 10, "status": "active"}
RESULTS: tuple[dict[str, object], ...] = (
    {"read_id": "R-0001", "version": 1, "data": {**QUOTE, "unit_price_minor": 540}},
    {"read_id": "R-0002", "version": 1, "data": {**QUOTE, "unit_price_minor": 575}},
    {"read_id": "R-0003", "version": 1, "data": {"limit_minor": 100000, "committed_minor": 20000}},
    {"action_id": "A-0001", "state": "confirmed", "effect_id": "PO-0001"},
    {"action_id": "A-0001", "state": "confirmed", "effect_id": "PO-0001"},
    {"result": "accepted"},
)
"""What the gateway answers the policy's six tool calls: two quotes, the budget, the
order, the order's status and the report."""

CALLS = len(RESULTS) + 1
"""Six tool calls and the closing "Done."."""

Think = Callable[[], Awaitable[None]]


async def no_time() -> None:
    """A think that takes no time: these tests have no scheduler."""


def scripted(think: Think = no_time) -> ScriptedModel:
    return ScriptedModel(lambda: scripted_policy(SCENARIO.intents, mode=Mode.GATEWAY), think)


async def converse(
    model: Runnable[Any, AIMessage], results: Sequence[object] = RESULTS
) -> tuple[list[AIMessage], list[list[BaseMessage]]]:
    """Ask `model` until it calls no tool, answering each call with the next result.

    Returns its replies and the messages of each request.
    """
    messages: list[BaseMessage] = [HumanMessage(SCENARIO.task)]
    replies: list[AIMessage] = []
    requests: list[list[BaseMessage]] = []
    for result in [*results, None]:
        requests.append(list(messages))
        reply = await model.ainvoke(messages)
        replies.append(reply)
        if not reply.tool_calls:
            break
        call = reply.tool_calls[0]
        assert call["id"] is not None
        answer = ToolMessage(json.dumps(result), tool_call_id=call["id"], name=call["name"])
        messages += [reply, answer]
    return replies, requests


async def cancelled_while_thinking(build: Callable[[Think], Runnable[Any, AIMessage]]) -> None:
    """Start a model call whose think never ends, and cancel it there, as a crash cancels
    a worker's task (`fivefold.harness.scheduler`)."""
    thinking = asyncio.Event()

    async def think() -> None:
        thinking.set()
        await asyncio.get_running_loop().create_future()

    call = asyncio.ensure_future(build(think).ainvoke([HumanMessage(SCENARIO.task)]))
    await thinking.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call


def without_id(message: BaseMessage) -> BaseMessage:
    """LangChain gives each reply a random id; nothing else about it may differ."""
    return message.model_copy(update={"id": None})


def scripted_hash(messages: Sequence[BaseMessage]) -> str:
    return request_hash(messages, tools=[], params=request_params(SCRIPTED, {}, None, {}))


def record(results: Sequence[object] = RESULTS) -> tuple[Cassette, list[AIMessage]]:
    """Record the scripted model's conversation over `results`."""
    recorder = CassetteRecorder(SCRIPTED)
    replies, _ = asyncio.run(
        converse(RecordingModel(scripted(), recorder, worker="agent-1"), results)
    )
    return recorder.cassette(HEADER), replies


def changed_price() -> tuple[dict[str, object], ...]:
    """`RESULTS` with a dearer second quote: the third request differs."""
    second = {**RESULTS[1], "data": {**QUOTE, "unit_price_minor": 580}}
    return (RESULTS[0], second, *RESULTS[2:])


class ToolEcho(BaseChatModel):
    """A chat model that binds tools as provider models do, and says what it was given."""

    @property
    def _llm_type(self) -> str:
        return "tool-echo"

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
        given = {
            "messages": len(messages),
            "tools": [tool["function"]["name"] for tool in kwargs.get("tools", [])],
            "tool_choice": kwargs.get("tool_choice"),
            "stop": stop,
        }
        return ChatResult(generations=[ChatGeneration(message=AIMessage(json.dumps(given)))])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)


class Down(BaseChatModel):
    """A chat model whose every call fails."""

    @property
    def _llm_type(self) -> str:
        return "down"

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
        raise ConnectionError("the model is down")


def quote_tool() -> BaseTool:
    async def get_quote(**arguments: Any) -> str:
        return json.dumps(arguments)

    return StructuredTool.from_function(
        coroutine=get_quote,
        name="get_quote",
        description="Read a supplier's quote.",
        args_schema={"type": "object", "properties": {"sku": {"type": "string"}}},
    )


# The recording wrapper ---------------------------------------------------------------------


def test_the_policy_plays_the_whole_task() -> None:
    replies, _ = asyncio.run(converse(scripted()))
    names = [reply.tool_calls[0]["name"] for reply in replies[:-1]]
    assert names == [
        "get_quote",
        "get_quote",
        "get_budget",
        "submit_purchase_order",
        "get_action_status",
        "submit_final_report",
    ]
    assert replies[-1].content == "Done."


@pytest.mark.parametrize("kind", ["surrogate", "large_integer", "deep_value"])
def test_an_unreadable_reply_does_not_enter_the_cassette(kind: str) -> None:
    recorder = CassetteRecorder(SCRIPTED)
    request = "ab" * 32
    recorder.record("agent-1", request, AIMessage(content="first"))
    previous = recorder.calls
    value: Any
    if kind == "surrogate":
        value = chr(0xD800)
    elif kind == "large_integer":
        value = 10**4999
    else:
        value = None
        for _ in range(200):
            value = [value]
    reply = AIMessage(content="", response_metadata={"sample": value})
    with pytest.raises(CassetteError, match="cannot be stored"):
        recorder.record("agent-1", request, reply)
    assert recorder.calls == previous
    assert Cassette.from_text(recorder.cassette(HEADER).text()).calls == previous


def test_the_wrapper_returns_what_the_wrapped_model_returns() -> None:
    plain, _ = asyncio.run(converse(scripted()))
    recorder = CassetteRecorder(SCRIPTED)
    wrapped, _ = asyncio.run(converse(RecordingModel(scripted(), recorder, worker="agent-1")))
    assert [without_id(reply) for reply in wrapped] == [without_id(reply) for reply in plain]


def test_the_wrapper_records_each_call_in_order() -> None:
    recorder = CassetteRecorder(SCRIPTED)
    first, asked_first = asyncio.run(
        converse(RecordingModel(scripted(), recorder, worker="agent-1"))
    )
    second, asked_second = asyncio.run(
        converse(RecordingModel(scripted(), recorder, worker="agent-2"))
    )
    calls = recorder.calls
    assert [call.worker for call in calls] == ["agent-1"] * CALLS + ["agent-2"] * CALLS
    assert [call.request for call in calls] == [
        scripted_hash(messages) for messages in [*asked_first, *asked_second]
    ]
    assert [response_message(call.response) for call in calls] == [
        without_id(reply) for reply in [*first, *second]
    ]
    assert calls[0].response == {
        "content": "",
        "tool_calls": [
            {
                "name": "get_quote",
                "args": {"supplier_id": "S-1", "sku": "W-100"},
                "id": "call-0001",
                "type": "tool_call",
            }
        ],
    }


def test_the_wrapper_takes_no_virtual_time_of_its_own() -> None:
    """The wrapped model thinks once per call (0009); the wrapper adds nothing."""
    thought: list[int] = []

    async def think() -> None:
        thought.append(1)

    recorder = CassetteRecorder(SCRIPTED)
    asyncio.run(converse(RecordingModel(scripted(think), recorder, worker="agent-1")))
    assert len(thought) == len(recorder.calls) == CALLS


def test_a_call_that_fails_records_nothing() -> None:
    recorder = CassetteRecorder(ModelInfo(adapter="live", name="down"))
    with pytest.raises(ConnectionError, match="the model is down"):
        asyncio.run(RecordingModel(Down(), recorder, worker="agent-1").ainvoke("hi"))
    assert recorder.calls == ()


def test_a_reply_beyond_the_cassette_format_limit_has_a_recording_error() -> None:
    recorder = CassetteRecorder(SCRIPTED)
    argument: object = "R-0001"
    for _ in range(1_200):
        argument = [argument]
    message = AIMessage(
        "",
        tool_calls=[
            {"name": "submit_purchase_order", "args": {"based_on": argument}, "id": "call-0001"}
        ],
    )
    with pytest.raises(CassetteError, match="the reply cannot be stored in the cassette format"):
        recorder.record("agent-1", "00" * 32, message)
    assert recorder.calls == ()


def test_a_call_cancelled_while_it_thinks_records_nothing() -> None:
    """As when a crash stops a worker in the middle of a model call (F11)."""
    recorder = CassetteRecorder(SCRIPTED)
    asyncio.run(
        cancelled_while_thinking(
            lambda think: RecordingModel(scripted(think), recorder, worker="agent-1")
        )
    )
    assert recorder.calls == ()


def test_binding_tools_binds_them_to_the_wrapped_model_and_records_them() -> None:
    model = ModelInfo(adapter="live", name="echo")
    recorder = CassetteRecorder(model)
    messages = [HumanMessage("Quote W-100.")]
    direct = ToolEcho().bind_tools([quote_tool()], tool_choice="any")
    wrapped = RecordingModel(ToolEcho(), recorder, worker="agent-1").bind_tools(
        [quote_tool()], tool_choice="any"
    )
    expected = asyncio.run(direct.ainvoke(messages, stop=["END"]))
    reply = asyncio.run(wrapped.ainvoke(messages, stop=["END"]))
    assert reply.content == expected.content
    assert json.loads(str(reply.content)) == {
        "messages": 1,
        "tools": ["get_quote"],
        "tool_choice": "any",
        "stop": ["END"],
    }
    (call,) = recorder.calls
    params = request_params(model, {"tool_choice": "any"}, ["END"], {})
    assert params == {"model": "echo", "tool_choice": "any", "stop": ["END"]}
    assert call.request == request_hash(messages, tools=tool_schemas([quote_tool()]), params=params)
    assert call.request != request_hash(messages, tools=[], params=params)


# The replay adapter ------------------------------------------------------------------------


def test_a_replay_serves_the_recorded_responses_in_order() -> None:
    cassette, recorded = record()
    thought: list[int] = []

    async def think() -> None:
        thought.append(1)

    player = CassettePlayer(cassette)
    replayed, _ = asyncio.run(converse(ReplayModel(player, worker="agent-1", think=think)))
    assert [without_id(reply) for reply in replayed] == [without_id(reply) for reply in recorded]
    assert player.served == len(cassette.calls) == CALLS
    assert len(thought) == CALLS, "every adapter takes think_ms per call (0009)"
    player.finish()


def test_a_request_that_differs_fails_the_replay_with_the_call_and_both_hashes() -> None:
    cassette, _ = record()
    changed, _ = record(changed_price())
    assert [a.request == b.request for a, b in zip(cassette.calls, changed.calls, strict=True)] == [
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    player = CassettePlayer(cassette)
    model = ReplayModel(player, worker="agent-1", think=no_time)
    with pytest.raises(ReplayError) as caught:
        asyncio.run(converse(model, changed_price()))
    error = caught.value
    assert (error.call, error.recorded, error.asked) == (
        3,
        cassette.calls[2].request,
        changed.calls[2].request,
    )
    assert str(error) == (
        f"model call 3: agent-1 asked a request with hash {changed.calls[2].request}, but the"
        f" cassette recorded agent-1 asking one with hash {cassette.calls[2].request}"
    )
    assert player.served == 2
    with pytest.raises(ReplayError) as later:
        asyncio.run(model.ainvoke([HumanMessage(SCENARIO.task)]))
    assert later.value is error, "every later call fails the same way"
    with pytest.raises(ReplayError) as finished:
        player.finish()
    assert finished.value is error


def test_a_call_by_another_worker_fails_the_replay() -> None:
    cassette, _ = record()
    player = CassettePlayer(cassette)
    with pytest.raises(ReplayError, match="model call 1: agent-2 asked") as caught:
        asyncio.run(converse(ReplayModel(player, worker="agent-2", think=no_time)))
    assert caught.value.recorded == caught.value.asked == cassette.calls[0].request
    assert "recorded agent-1 asking" in str(caught.value)


def test_a_run_that_asks_for_more_calls_than_recorded_fails() -> None:
    cassette, _ = record()
    short = cassette.model_copy(update={"calls": cassette.calls[:3]})
    player = CassettePlayer(short)
    with pytest.raises(ReplayError) as caught:
        asyncio.run(converse(ReplayModel(player, worker="agent-1", think=no_time)))
    error = caught.value
    assert (error.call, error.recorded, error.asked) == (4, None, cassette.calls[3].request)
    assert str(error).startswith("model call 4 (agent-1): the cassette holds only 3 calls")
    with pytest.raises(ReplayError) as finished:
        player.finish()
    assert finished.value is error


def test_a_run_that_leaves_recorded_calls_unasked_fails() -> None:
    cassette, _ = record()
    player = CassettePlayer(cassette)
    asyncio.run(converse(ReplayModel(player, worker="agent-1", think=no_time), RESULTS[:1]))
    assert player.served == 2
    with pytest.raises(ReplayError) as caught:
        player.finish()
    error = caught.value
    assert (error.call, error.recorded, error.asked) == (3, cassette.calls[2].request, None)
    assert str(error) == (
        "the run made 2 model calls, but the cassette holds 7: calls 3 to 7 were never asked"
    )


def test_a_call_cancelled_while_it_thinks_takes_no_recorded_call() -> None:
    """A worker that crashes during a model call asks again when it resumes; the
    recording kept nothing for the crashed call, so the replay must use nothing either."""
    cassette, recorded = record()
    player = CassettePlayer(cassette)
    asyncio.run(
        cancelled_while_thinking(lambda think: ReplayModel(player, worker="agent-1", think=think))
    )
    assert player.served == 0
    resumed = ReplayModel(player, worker="agent-1", think=no_time)
    reply = asyncio.run(resumed.ainvoke([HumanMessage(SCENARIO.task)]))
    assert (player.served, without_id(reply)) == (1, without_id(recorded[0]))


def test_a_replay_hashes_bound_tools_as_the_recording_did() -> None:
    echo = ModelInfo(adapter="live", name="echo")
    recorder = CassetteRecorder(echo)
    messages = [HumanMessage("Quote W-100.")]
    recording = RecordingModel(ToolEcho(), recorder, worker="agent-1")
    reply = asyncio.run(recording.bind_tools([quote_tool()], tool_choice="any").ainvoke(messages))
    cassette = recorder.cassette(HEADER)

    player = CassettePlayer(cassette)
    replay = ReplayModel(player, worker="agent-1", think=no_time)
    replayed = asyncio.run(replay.bind_tools([quote_tool()], tool_choice="any").ainvoke(messages))
    assert without_id(replayed) == without_id(reply)
    player.finish()

    unbound = CassettePlayer(cassette)
    with pytest.raises(ReplayError, match="model call 1"):
        asyncio.run(ReplayModel(unbound, worker="agent-1", think=no_time).ainvoke(messages))
