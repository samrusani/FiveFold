"""An agent worker: the shared LangGraph agent of record 0008, run on the scheduler.

The graph has a model node and the prebuilt `ToolNode` with its default error handling,
and the tool node has a `RetryPolicy` with the library's defaults (0008). Each worker has
its own checkpoint thread, named after the worker, and its own step budget (0009). A
worker that starts again after a crash resumes from its last checkpoint, so LangGraph
re-runs the node that was interrupted (0006, F11).

A worker ends in one of three ways, which the harness records: its graph finished, its
step budget ran out, or the graph raised, for example when the tool node's retries ran
out (0008). The run does not stop for a worker's error.

Checked in langgraph 1.2.22 and langgraph-prebuilt 1.1.2: `StateGraph(MessagesState)`,
`add_node(name, node, retry_policy=RetryPolicy())`, `add_conditional_edges(name,
tools_condition)`, `compile(checkpointer=)`, and `ainvoke(None, config)` to resume a
thread from its last checkpoint; the config's `recursion_limit` bounds the steps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import RetryPolicy

BUDGET_TEXT = "Step budget used up."


class WorkerEnd(StrEnum):
    """How an agent worker ended."""

    FINISHED = "finished"
    BUDGET = "budget"
    ERROR = "error"


@dataclass(frozen=True)
class WorkerSpec:
    """One agent worker: its name, which is also its checkpoint thread, and its budget."""

    name: str
    task: str
    max_steps: int


def build_graph(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    checkpointer: BaseCheckpointSaver[Any],
    *,
    max_steps: int,
    on_step: Callable[[], None] | None = None,
) -> Any:
    """The agent's graph: a model node and the prebuilt tool node, with retries."""

    async def call_model(state: MessagesState) -> dict[str, list[Any]]:
        steps = sum(1 for message in state["messages"] if isinstance(message, AIMessage))
        if steps >= max_steps:
            return {"messages": [AIMessage(content=BUDGET_TEXT)]}
        if on_step is not None:
            on_step()
        reply = await model.ainvoke(state["messages"])
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
    if isinstance(last, AIMessage) and last.content == BUDGET_TEXT:
        return WorkerEnd.BUDGET, BUDGET_TEXT
    return WorkerEnd.FINISHED, str(last.content)
