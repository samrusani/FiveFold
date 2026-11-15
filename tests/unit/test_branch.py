"""The branch adapter and the branch's model spec, outside a run (0009, Playback and
branching).

The scripted policy plays dev-001 in G2 against fixed tool results: six tool calls, then
"Done.". That conversation, recorded, is the parent. A branch at K must answer the first K
calls with the parent's responses, under the replay's checks, and every later call with
the next model; take one think per call, whichever way the call goes; decide where a call
goes when its think ends, since calls are numbered in the order they are answered; and
fail, with the call's number and both hashes, as a replay does. The harness gives a
branch its own cassette, hashed with the model it switches to, which a replay serves.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from fivefold.agents.branch import BranchError, BranchModel, BranchPlayer
from fivefold.agents.policy import Mode, scripted_policy
from fivefold.agents.replay import CassettePlayer, ReplayError, ReplayModel
from fivefold.agents.scripted import ScriptedModel, ScriptError
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.demo import DEV_001
from fivefold.harness.log import RunRecord
from fivefold.harness.models import (
    SCRIPTED_MODEL,
    Branch,
    Parent,
    Replay,
    RunModels,
    Scripted,
    branch_run_id,
    parent_of,
    run_header,
)
from fivefold.recorder.cassette import (
    Cassette,
    ModelInfo,
    RunHeader,
    request_hash,
    request_params,
)
from fivefold.recorder.recording import CassetteRecorder, RecordingModel

SCENARIO = load_scenario_file(DEV_001)
HEADER = RunHeader(
    run_id="dev-001-single-po.G2.C2.7",
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


class Counted:
    """A think that counts how often it was taken."""

    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


def scripted(think: Think = no_time) -> ScriptedModel:
    return ScriptedModel(lambda: scripted_policy(SCENARIO.intents, mode=Mode.GATEWAY), think)


class Answering(BaseChatModel):
    """A next model that answers as the scripted model does, and counts its calls."""

    _calls: int = PrivateAttr(default=0)

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def _llm_type(self) -> str:
        return "answering"

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
        self._calls += 1
        reply = await scripted().ainvoke(messages)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Answering:
        return self


async def converse(
    model: Runnable[Any, AIMessage], results: Sequence[object] = RESULTS
) -> list[AIMessage]:
    """Ask `model` until it calls no tool, answering each call with the next result."""
    messages: list[BaseMessage] = [HumanMessage(SCENARIO.task)]
    replies: list[AIMessage] = []
    for result in [*results, None]:
        reply = await model.ainvoke(messages)
        replies.append(reply)
        if not reply.tool_calls:
            break
        call = reply.tool_calls[0]
        assert call["id"] is not None
        answer = ToolMessage(json.dumps(result), tool_call_id=call["id"], name=call["name"])
        messages += [reply, answer]
    return replies


def without_id(message: BaseMessage) -> BaseMessage:
    """LangChain gives each reply a random id; nothing else about it may differ."""
    return message.model_copy(update={"id": None})


def record(results: Sequence[object] = RESULTS) -> tuple[Cassette, list[AIMessage]]:
    """Record the scripted model's conversation over `results`: the parent."""
    recorder = CassetteRecorder(SCRIPTED_MODEL)
    replies = asyncio.run(converse(RecordingModel(scripted(), recorder, worker="agent-1"), results))
    return recorder.cassette(HEADER), replies


def changed_price() -> tuple[dict[str, object], ...]:
    """`RESULTS` with a dearer second quote: the third request differs."""
    second = {**RESULTS[1], "data": {**QUOTE, "unit_price_minor": 580}}
    return (RESULTS[0], second, *RESULTS[2:])


PARENT, PARENT_REPLIES = record()


# The branch adapter --------------------------------------------------------------------------


@pytest.mark.parametrize("point", range(CALLS + 1))
def test_a_branch_answers_the_first_k_calls_from_the_parent_and_the_rest_with_the_next_model(
    point: int,
) -> None:
    player = BranchPlayer(PARENT, point)
    think, then = Counted(), Answering()
    replies = asyncio.run(converse(BranchModel(player, then, worker="agent-1", think=think)))
    assert [without_id(reply) for reply in replies] == [
        without_id(reply) for reply in PARENT_REPLIES
    ]
    assert (player.served, then.calls) == (point, CALLS - point)
    assert think.count == CALLS, "one think per call, whichever way it goes (0009)"
    player.finish()


def test_a_branch_at_the_end_of_a_cut_short_cassette_goes_on_with_the_next_model() -> None:
    """A parent whose cassette ends early, as when its provider stopped answering: with K
    at the cassette's length, the next model answers the calls the cassette lacks."""
    short = PARENT.model_copy(update={"calls": PARENT.calls[:3]})
    player = BranchPlayer(short, 3)
    then = Answering()
    replies = asyncio.run(converse(BranchModel(player, then, worker="agent-1", think=no_time)))
    assert [without_id(reply) for reply in replies] == [
        without_id(reply) for reply in PARENT_REPLIES
    ]
    assert (player.served, then.calls) == (3, CALLS - 3)
    player.finish()


def test_the_branch_point_is_from_0_to_the_parent_s_calls() -> None:
    for point in (-1, CALLS + 1):
        with pytest.raises(BranchError, match=f"from 0 to {CALLS}, .* not {point}"):
            BranchPlayer(PARENT, point)
        with pytest.raises(BranchError):
            Branch(PARENT, point)
    assert Branch(PARENT, 0).point == 0 and Branch(PARENT, CALLS).point == CALLS


def test_a_request_that_differs_before_the_branch_point_fails_as_a_replay_does() -> None:
    changed, _ = record(changed_price())
    player = BranchPlayer(PARENT, 5)
    model = BranchModel(player, Answering(), worker="agent-1", think=no_time)
    with pytest.raises(ReplayError) as caught:
        asyncio.run(converse(model, changed_price()))
    error = caught.value
    assert (error.call, error.recorded, error.asked) == (
        3,
        PARENT.calls[2].request,
        changed.calls[2].request,
    )
    assert player.served == 2
    with pytest.raises(ReplayError) as later:
        asyncio.run(model.ainvoke([HumanMessage(SCENARIO.task)]))
    assert later.value is error, "every later call fails the same way, past K too"
    with pytest.raises(ReplayError) as finished:
        player.finish()
    assert finished.value is error


def test_after_the_branch_point_the_run_may_differ_from_the_parent() -> None:
    """The next model answers what the run asks, so a changed quote is no failure."""
    player = BranchPlayer(PARENT, 2)
    then = Answering()
    replies = asyncio.run(
        converse(BranchModel(player, then, worker="agent-1", think=no_time), changed_price())
    )
    assert len(replies) == CALLS and (player.served, then.calls) == (2, CALLS - 2)
    player.finish()


def test_a_run_that_asks_fewer_than_k_calls_fails() -> None:
    player = BranchPlayer(PARENT, 5)
    asyncio.run(converse(BranchModel(player, Answering(), worker="agent-1", think=no_time), []))
    assert player.served == 1
    with pytest.raises(ReplayError) as caught:
        player.finish()
    error = caught.value
    assert (error.call, error.recorded, error.asked) == (2, PARENT.calls[1].request, None)
    assert str(error) == (
        "the branch made 1 model calls, but it replays the parent's first 5: calls 2 to 5"
        " were never asked"
    )


def test_the_scripted_model_cannot_continue_another_model_s_conversation() -> None:
    """A parent whose first reply carries a provider's random tool call id, as a live
    model's does: after the branch point, the scripted model refuses the conversation."""
    first = dict(PARENT.calls[0].response)
    calls = first["tool_calls"]
    assert isinstance(calls, list) and isinstance(calls[0], dict)
    first["tool_calls"] = [{**calls[0], "id": "3f2a9c1e-from-a-provider"}]
    parent = PARENT.model_copy(
        update={"calls": (PARENT.calls[0].model_copy(update={"response": first}),)}
    )
    model = BranchModel(BranchPlayer(parent, 1), scripted(), worker="agent-1", think=no_time)
    with pytest.raises(ScriptError, match="result 1 answers 3f2a9c1e-from-a-provider"):
        asyncio.run(converse(model))


def test_a_call_by_another_worker_before_the_branch_point_fails() -> None:
    player = BranchPlayer(PARENT, 3)
    with pytest.raises(ReplayError, match="model call 1: agent-2 asked"):
        asyncio.run(converse(BranchModel(player, Answering(), worker="agent-2", think=no_time)))


def test_a_call_cancelled_while_it_thinks_takes_no_call() -> None:
    """As when a crash stops a worker in the middle of a model call (F11): the parent
    recorded nothing for that call, so the branch counts nothing either."""
    player = BranchPlayer(PARENT, 1)
    thinking = asyncio.Event()

    async def think() -> None:
        thinking.set()
        await asyncio.get_running_loop().create_future()

    async def cancelled() -> None:
        model = BranchModel(player, Answering(), worker="agent-1", think=think)
        call = asyncio.ensure_future(model.ainvoke([HumanMessage(SCENARIO.task)]))
        await thinking.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(cancelled())
    assert player.served == 0 and player.replaying
    then = Answering()
    reply = asyncio.run(
        BranchModel(player, then, worker="agent-1", think=no_time).ainvoke(
            [HumanMessage(SCENARIO.task)]
        )
    )
    assert (player.served, then.calls) == (1, 0)
    assert without_id(reply) == without_id(PARENT_REPLIES[0])


def test_where_a_call_goes_is_decided_when_its_think_ends() -> None:
    """Two workers think at once with one call of the parent left to replay. The call
    whose think ends first is answered first, so it is the parent's last call; the other
    is the next model's, whichever worker started first."""
    recorder = CassetteRecorder(SCRIPTED_MODEL)
    task = [HumanMessage(SCENARIO.task)]
    for worker in ("agent-2", "agent-1"):
        asyncio.run(RecordingModel(scripted(), recorder, worker=worker).ainvoke(task))
    parent = recorder.cassette(HEADER)

    async def both() -> tuple[int, int]:
        player = BranchPlayer(parent, 1)
        gates = {worker: asyncio.Event() for worker in ("agent-1", "agent-2")}
        nexts = {worker: Answering() for worker in gates}

        def model(worker: str) -> BranchModel:
            async def think() -> None:
                await gates[worker].wait()

            return BranchModel(player, nexts[worker], worker=worker, think=think)

        first = asyncio.ensure_future(model("agent-1").ainvoke(task))
        second = asyncio.ensure_future(model("agent-2").ainvoke(task))
        await asyncio.sleep(0)
        gates["agent-2"].set()
        await second
        gates["agent-1"].set()
        await first
        player.finish()
        return nexts["agent-1"].calls, nexts["agent-2"].calls

    assert asyncio.run(both()) == (1, 0), "agent-2 got the parent's call, agent-1 the next"


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
        }
        return ChatResult(generations=[ChatGeneration(message=AIMessage(json.dumps(given)))])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)


def quote_tool() -> BaseTool:
    async def get_quote(**arguments: Any) -> str:
        return json.dumps(arguments)

    return StructuredTool.from_function(
        coroutine=get_quote,
        name="get_quote",
        description="Read a supplier's quote.",
        args_schema={"type": "object", "properties": {"sku": {"type": "string"}}},
    )


def test_tools_are_hashed_as_the_parent_did_and_bound_to_the_next_model() -> None:
    echo = ModelInfo(adapter="live", name="echo")
    recorder = CassetteRecorder(echo)
    messages = [HumanMessage("Quote W-100.")]
    recording = RecordingModel(ToolEcho(), recorder, worker="agent-1")
    recorded = asyncio.run(
        recording.bind_tools([quote_tool()], tool_choice="any").ainvoke(messages)
    )
    parent = recorder.cassette(HEADER)

    player = BranchPlayer(parent, 1)
    model = BranchModel(player, ToolEcho(), worker="agent-1", think=no_time)
    bound = model.bind_tools([quote_tool()], tool_choice="any")
    assert without_id(asyncio.run(bound.ainvoke(messages))) == without_id(recorded)
    after = asyncio.run(bound.ainvoke([*messages, recorded, HumanMessage("Again.")]))
    assert json.loads(str(after.content)) == {
        "messages": 3,
        "tools": ["get_quote"],
        "tool_choice": "any",
    }, "the next model got the tools"
    assert player.served == 1
    player.finish()

    unbound = BranchPlayer(parent, 1)
    with pytest.raises(ReplayError, match="model call 1"):
        asyncio.run(
            BranchModel(unbound, ToolEcho(), worker="agent-1", think=no_time).ainvoke(messages)
        )


def test_a_branch_s_own_cassette_hashes_every_call_with_its_model_and_replays() -> None:
    """The branch's recorder hashes each call with the model the branch switches to, the
    replayed calls included, so a replay of the branch's cassette, which hashes with the
    cassette's model, serves every call."""
    live = ModelInfo(adapter="live", name="fake:m", params={"temperature": 0})
    recorder = CassetteRecorder(live)
    player = BranchPlayer(PARENT, 3)
    branch = RecordingModel(
        BranchModel(player, Answering(), worker="agent-1", think=no_time),
        recorder,
        worker="agent-1",
    )
    replies = asyncio.run(converse(branch))
    own = recorder.cassette(HEADER)
    assert [call.response for call in own.calls] == [call.response for call in PARENT.calls]
    first = [HumanMessage(SCENARIO.task)]
    assert own.calls[0].request == request_hash(
        first, tools=[], params=request_params(live, {}, None, {})
    )
    assert all(a.request != b.request for a, b in zip(own.calls, PARENT.calls, strict=True))

    replay = CassettePlayer(own)
    replayed = asyncio.run(converse(ReplayModel(replay, worker="agent-1", think=no_time)))
    assert [without_id(r) for r in replayed] == [without_id(r) for r in replies]
    replay.finish()


# The model spec and the harness's models ---------------------------------------------------

RUN = RunRecord(
    run_id=branch_run_id(Parent(HEADER.run_id, 3)),
    scenario_id=SCENARIO.id,
    scenario_digest="00" * 32,
    config="G2",
    contract="C2",
    seed=7,
    model_adapter="scripted",
    model="scripted",
    params={},
    parent_run_id=HEADER.run_id,
    branch_point=3,
)


def test_a_branch_is_named_after_its_parent_and_its_branch_point() -> None:
    assert parent_of(Branch(PARENT, 3)) == Parent("dev-001-single-po.G2.C2.7", 3)
    assert branch_run_id(Parent("dev-001-single-po.G2.C2.7", 3)) == (
        "dev-001-single-po.G2.C2.7.branch-3"
    )
    assert branch_run_id(Parent("dev-001-single-po.G2.C2.7.branch-3", 5)) == (
        "dev-001-single-po.G2.C2.7.branch-3.branch-5"
    )
    own = PARENT.model_copy(update={"run": run_header(RUN)})
    assert parent_of(Replay(own)) == Parent("dev-001-single-po.G2.C2.7", 3)
    assert parent_of(Replay(PARENT)) is None
    assert parent_of(Scripted()) is None


def test_the_harness_gives_a_branch_its_own_cassette_and_one_think_per_call() -> None:
    models = RunModels(Branch(PARENT, 3))
    assert (models.adapter, models.name, models.cassette_digest) == ("scripted", "scripted", None)
    given: list[Think] = []

    def builder(its_think: Think) -> BaseChatModel:
        given.append(its_think)
        return scripted(its_think)

    think = Counted()
    model = models.chat_model("agent-1", scripted=builder, think=think)
    assert isinstance(model, RecordingModel)
    assert len(given) == 1 and given[0] is not think, "the next model takes no think"
    with pytest.raises(ReplayError, match="the branch made 0 model calls"):
        models.check()
    asyncio.run(converse(model))
    assert (models.served, think.count) == (3, CALLS)
    models.check()
    digest = models.stopped(RUN)
    assert models.cassette is not None and digest == models.cassette.digest()
    assert models.cassette.model == SCRIPTED_MODEL
    assert models.cassette.calls == PARENT.calls, "the same model gives the parent's hashes"
    assert (models.cassette.run.parent_run_id, models.cassette.run.branch_point) == (
        HEADER.run_id,
        3,
    )
    assert '"branch_point": 3' in models.cassette.text()
    assert '"parent_run_id": "dev-001-single-po.G2.C2.7"' in models.cassette.text()


def test_a_branch_whatever_its_model_s_record_flag_records() -> None:
    models = RunModels(Branch(PARENT, 0, Scripted(record=False)))
    model = models.chat_model("agent-1", scripted=scripted, think=no_time)
    asyncio.run(converse(model))
    assert models.stopped(replace(RUN, branch_point=0)) is not None
    assert models.cassette is not None and len(models.cassette.calls) == CALLS
