"""The agent worker's graph: tools bound to the model, and the step and token budgets
(0008, 0009).

The graph binds the worker's tools to the model, so every adapter is asked with their
schemas. A worker's budgets are counted from its conversation: steps are the model's
replies, and tokens are their input and output tokens, as each reply's usage reports
them. Before each call, a worker whose budget is used up ends as `budget`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr

from fivefold.agents.worker import (
    BUDGET_TEXT,
    SPEND_CAP_TEXT,
    TOKEN_BUDGET_TEXT,
    WorkerEnd,
    WorkerSpec,
    budget_end,
    build_graph,
    run_worker,
    tokens_used,
)


def usage(input_tokens: int, output_tokens: int) -> UsageMetadata:
    return UsageMetadata(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def quote_tool() -> BaseTool:
    async def get_quote(**arguments: Any) -> str:
        return json.dumps({"read_id": "R-0001", "args": arguments})

    return StructuredTool.from_function(
        coroutine=get_quote,
        name="get_quote",
        description="Read a supplier's quote.",
        args_schema={"type": "object", "properties": {"sku": {"type": "string"}}},
    )


class Caller(BaseChatModel):
    """A model that calls `get_quote` on every call, with the usage given, and checks that
    the tool is bound to it, as a provider's model needs. With `refuse`, it answers as the
    live adapter does at its spend cap."""

    _usage: UsageMetadata | None = PrivateAttr()
    _calls: list[int] = PrivateAttr()
    _refuse: bool = PrivateAttr()

    def __init__(
        self, usage: UsageMetadata | None, calls: list[int], *, refuse: bool = False
    ) -> None:
        super().__init__()
        self._usage = usage
        self._calls = calls
        self._refuse = refuse

    @property
    def _llm_type(self) -> str:
        return "caller"

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
        assert [tool["function"]["name"] for tool in kwargs["tools"]] == ["get_quote"]
        if self._refuse:
            refused = AIMessage(SPEND_CAP_TEXT, response_metadata={"fivefold_refused": "spend_cap"})
            return ChatResult(generations=[ChatGeneration(message=refused)])
        self._calls.append(len(self._calls) + 1)
        call = {"name": "get_quote", "args": {"sku": "W-100"}, "id": f"c-{len(self._calls)}"}
        reply = AIMessage("", tool_calls=[call], usage_metadata=self._usage)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)


def run_caller(
    usage: UsageMetadata | None, *, max_steps: int, max_tokens: int
) -> tuple[tuple[WorkerEnd, str], list[int], list[int], list[str]]:
    """Run a worker whose model always calls a tool; return how it ended, the model's
    calls, the steps `on_step` heard and the replies `on_call` logged."""
    calls: list[int] = []
    steps: list[int] = []
    logged: list[str] = []

    def on_call() -> Any:
        def answered(reply: AIMessage) -> None:
            logged.append(str(reply.usage_metadata))

        return answered

    graph = build_graph(
        Caller(usage, calls),
        [quote_tool()],
        InMemorySaver(),
        max_steps=max_steps,
        max_tokens=max_tokens,
        on_step=steps.append,
        on_call=on_call,
    )
    spec = WorkerSpec("agent-1", "Order W-100.", max_steps, max_tokens)
    end = asyncio.run(run_worker(graph, spec, resume=False))
    return end, calls, steps, logged


def test_a_worker_ends_as_budget_once_its_replies_used_its_tokens() -> None:
    """40 tokens a call and a budget of 100: the third call leaves 120 used, so the fourth
    would start past the budget and is never made."""
    end, calls, steps, logged = run_caller(usage(30, 10), max_steps=40, max_tokens=100)
    assert end == (WorkerEnd.BUDGET, TOKEN_BUDGET_TEXT)
    assert calls == [1, 2, 3] and steps == [1, 2, 3] and len(logged) == 3


def test_a_budget_of_exactly_the_tokens_used_stops_the_next_call() -> None:
    end, calls, _, _ = run_caller(usage(30, 10), max_steps=40, max_tokens=80)
    assert (end, calls) == ((WorkerEnd.BUDGET, TOKEN_BUDGET_TEXT), [1, 2])


def test_replies_without_usage_never_use_up_the_token_budget() -> None:
    """Scripted replies report no usage, so a scripted run never meets the token budget."""
    end, calls, _, logged = run_caller(None, max_steps=5, max_tokens=1)
    assert end == (WorkerEnd.BUDGET, BUDGET_TEXT)
    assert calls == [1, 2, 3, 4, 5] and logged == ["None"] * 5


def test_tokens_used_counts_input_and_output_of_each_reply() -> None:
    messages: list[BaseMessage] = [
        HumanMessage("Order W-100."),
        AIMessage("", usage_metadata=usage(100, 20)),
        AIMessage("", usage_metadata=usage(150, 5)),
        AIMessage("no usage"),
    ]
    assert tokens_used(messages) == 275


def test_a_refused_call_ends_the_worker_as_budget_and_is_no_step() -> None:
    steps: list[int] = []
    refusing = Caller(None, [], refuse=True)
    graph = build_graph(
        refusing, [quote_tool()], InMemorySaver(), max_steps=40, on_step=steps.append
    )
    end = asyncio.run(run_worker(graph, WorkerSpec("agent-1", "Order.", 40), resume=False))
    assert end == (WorkerEnd.BUDGET, SPEND_CAP_TEXT)
    assert steps == []


def test_model_text_does_not_end_a_worker_as_budget_by_itself() -> None:
    assert budget_end(AIMessage(BUDGET_TEXT)) is None
    assert budget_end(AIMessage(TOKEN_BUDGET_TEXT)) is None
    call = {"name": "get_quote", "args": {}, "id": "c-1"}
    assert budget_end(AIMessage(BUDGET_TEXT, tool_calls=[call])) is None
    assert budget_end(AIMessage([{"type": "text", "text": BUDGET_TEXT}])) is None
    assert budget_end(HumanMessage(BUDGET_TEXT)) is None
    assert budget_end(AIMessage("Done.")) is None
    assert budget_end(AIMessage(SPEND_CAP_TEXT)) is None
