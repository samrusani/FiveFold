"""An agent worker: the shared LangGraph agent of record 0008, run on the scheduler.

The graph has a model node and the prebuilt `ToolNode` with its default error handling,
and the tool node has a `RetryPolicy` with the library's defaults (0008). The model node
calls the model with the worker's tools bound to it, so every adapter is asked the same
requests, with the tools' schemas, as a live model needs them. Each worker has its own
checkpoint thread, named after the worker, and its own step and token budgets (0009). A
worker that starts again after a crash resumes from its last checkpoint, so LangGraph
re-runs the node that was interrupted (0006, F11).

The budgets are counted from the conversation, which the checkpoint keeps, so they hold
across a crash and a replay counts them the same way: the steps are the model's replies,
and the tokens are their input and output tokens, as each reply's usage reports them. A
reply that reports no usage counts none, so the scripted adapter, and the replay of a
scripted run, never use up the token budget. Before each model call, a worker whose steps
or tokens already reached its budget makes no further call: it ends as `budget`, as it
does when the live adapter's spend cap refuses a call (`fivefold.agents.live`).

A worker ends in one of three ways, which the harness records: its graph finished, a
budget ran out, or the graph raised, for example when the tool node's retries ran out
(0008). The run does not stop for a worker's error.

Checked in langgraph 1.2.22 and langgraph-prebuilt 1.1.2: `StateGraph(MessagesState)`,
`add_node(name, node, retry_policy=RetryPolicy())`, `add_conditional_edges(name,
tools_condition)`, `compile(checkpointer=)`, and `ainvoke(None, config)` to resume a
thread from its last checkpoint; the config's `recursion_limit` bounds the steps. Checked
in langchain-core 1.8.0 (`language_models/chat_models.py`): `BaseChatModel.bind_tools
(tools, *, tool_choice=None, **kwargs)` returns a `Runnable` of the model with the tools
bound; `AIMessage.usage_metadata` is a `UsageMetadata` or None (`messages/ai.py`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import RetryPolicy

BUDGET_TEXT: Final = "Step budget used up."
TOKEN_BUDGET_TEXT: Final = "Token budget used up."
SPEND_CAP_TEXT: Final = "Spend cap reached."
BUDGET_TEXTS: Final = (BUDGET_TEXT, TOKEN_BUDGET_TEXT, SPEND_CAP_TEXT)
"""The last message of a worker that ended as `budget`: its steps or tokens ran out, or
the live adapter's spend cap refused its call."""

CallLog = Callable[[], Callable[[AIMessage], None]]
"""The harness's model-call log: called as a model call starts, it returns what to call
with the call's reply once the model answers."""


class WorkerEnd(StrEnum):
    """How an agent worker ended."""

    FINISHED = "finished"
    BUDGET = "budget"
    ERROR = "error"


@dataclass(frozen=True)
class WorkerSpec:
    """One agent worker: its name, which is also its checkpoint thread, and its budgets."""

    name: str
    task: str
    max_steps: int
    max_tokens: int | None = None
    """None for no token budget."""


def budget_end(message: BaseMessage) -> str | None:
    """The budget that `message` says ran out, if it is the last message of a worker that
    ended as `budget`."""
    if isinstance(message, AIMessage) and not message.tool_calls:
        content = message.content
        if isinstance(content, str) and content in BUDGET_TEXTS:
            return content
    return None


def tokens_used(messages: Sequence[BaseMessage]) -> int:
    """The input and output tokens of the model's replies in `messages`, as their usage
    reports them; a reply that reports none counts none."""
    used = 0
    for message in messages:
        if isinstance(message, AIMessage) and message.usage_metadata is not None:
            used += message.usage_metadata["input_tokens"] + message.usage_metadata["output_tokens"]
    return used


def build_graph(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    checkpointer: BaseCheckpointSaver[Any],
    *,
    max_steps: int,
    max_tokens: int | None = None,
    on_step: Callable[[int], None] | None = None,
    on_call: CallLog | None = None,
) -> Any:
    """The agent's graph: a model node, with the tools bound to the model, and the
    prebuilt tool node, with retries.

    `on_step` is called with the step's number each time the model finishes a step: the
    fault point `agent.step` (0006). A refused call is no step. `on_call` logs each model
    call (`CallLog`).
    """
    bound = model.bind_tools(list(tools))

    async def call_model(state: MessagesState) -> dict[str, list[Any]]:
        messages = state["messages"]
        steps = sum(1 for message in messages if isinstance(message, AIMessage))
        if steps >= max_steps:
            return {"messages": [AIMessage(content=BUDGET_TEXT)]}
        if max_tokens is not None and tokens_used(messages) >= max_tokens:
            return {"messages": [AIMessage(content=TOKEN_BUDGET_TEXT)]}
        answered = on_call() if on_call is not None else None
        reply = await bound.ainvoke(messages)
        if answered is not None:
            answered(reply)
        if on_step is not None and budget_end(reply) is None:
            on_step(steps + 1)
        return {"messages": [reply]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_node("tools", ToolNode(list(tools)), retry_policy=RetryPolicy())
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition)
    graph.add_edge("tools", "model")
    return graph.compile(checkpointer=checkpointer)


async def run_worker(graph: Any, spec: WorkerSpec, *, resume: bool) -> tuple[WorkerEnd, str]:
    """Run the worker's graph to its end; resume from the checkpoint after a crash.

    Returns how it ended and a short description. Cancellation, as a crash causes,
    propagates: the harness restarts the worker.
    """
    config: RunnableConfig = {
        "configurable": {"thread_id": spec.name},
        "recursion_limit": 2 * spec.max_steps + 4,
    }
    start = None if resume else {"messages": [HumanMessage(spec.task)]}
    try:
        final = await graph.ainvoke(start, config)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return WorkerEnd.ERROR, f"{type(error).__name__}: {error}"
    last = final["messages"][-1]
    budget = budget_end(last)
    if budget is not None:
        return WorkerEnd.BUDGET, budget
    return WorkerEnd.FINISHED, str(last.content)
