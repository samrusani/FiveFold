"""The scripted model adapter (0009): the scripted policy behind a chat model interface.

Each model call replays the policy over the tool results in the conversation and replies
with the policy's next tool call, or with a closing message once the policy is done. The
policy holds no state of its own, so a worker resumed from its checkpoint makes the same
calls. Tool call ids are `call-0001`, `call-0002` and so on, so they are the same in
every run with the same results.

Every call takes `think_ms` of virtual time first (0009). The adapter spends no tokens.

Checked in langchain-core 1.8.0: `BaseChatModel` subclasses implement `_generate` and
`_llm_type`; `_agenerate` is the async path that `ainvoke` takes; `bind_tools` is the
hook a graph calls to attach tools, which a scripted model has no use for.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from fivefold.agents.policy import Policy, Result, ToolCall

DONE_TEXT = "Done."
"""The closing message once the policy has nothing left to call."""


class ScriptError(RuntimeError):
    """The conversation does not match the calls the policy made."""


def call_id(number: int) -> str:
    """The id of the policy's `number`-th tool call."""
    return f"call-{number:04d}"


def next_call(policy: Policy, messages: Sequence[BaseMessage]) -> ToolCall | None:
    """Replay `policy` over the tool results in `messages`; return its next call."""
    results = [message for message in messages if isinstance(message, ToolMessage)]
    try:
        call = next(policy)
        for number, message in enumerate(results, start=1):
            if message.tool_call_id != call_id(number):
                raise ScriptError(f"result {number} answers {message.tool_call_id}")
            if message.name is not None and message.name != call.name:
                raise ScriptError(f"result {number} came from {message.name}, not {call.name}")
            call = policy.send(_parse(message))
    except StopIteration:
        return None
    return call


def _parse(message: ToolMessage) -> Result:
    """A tool result as the policy reads it: the tool's JSON, or the error text."""
    content = message.content
    text = content if isinstance(content, str) else json.dumps(content)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"error": text}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


class ScriptedModel(BaseChatModel):
    """A chat model whose every reply is the scripted policy's next tool call."""

    _make_policy: Callable[[], Policy] = PrivateAttr()
    _think: Callable[[], Awaitable[None]] = PrivateAttr()

    def __init__(
        self, make_policy: Callable[[], Policy], think: Callable[[], Awaitable[None]]
    ) -> None:
        super().__init__()
        self._make_policy = make_policy
        self._think = think

    @property
    def _llm_type(self) -> str:
        return "fivefold-scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("the scripted model runs on the scheduler's loop only")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        await self._think()
        call = next_call(self._make_policy(), messages)
        if call is None:
            message = AIMessage(content=DONE_TEXT)
        else:
            number = sum(1 for m in messages if isinstance(m, ToolMessage)) + 1
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": call.name,
                        "args": dict(call.args),
                        "id": call_id(number),
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedModel:
        return self
