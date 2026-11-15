"""The branch adapter (0009, Playback and branching): replay a parent run's first K model
calls from its cassette, then hand every later call to another model.

An experimental branch re-executes the run a cassette was recorded from, but only its
first K model calls come from the cassette. K is the branch point. Model calls are the
only points where the model can switch, so K counts them: the run's calls across its
workers, in the order they are answered, as the cassette and `harness.model_call` number
them.

All of a branch run's models share one `BranchPlayer`. Each call first takes `think_ms`
of virtual time, once, as with every adapter (0009). The adapter then decides where the
call goes, at that moment rather than when the call starts: calls are numbered in the
order they are answered, and two workers can be thinking at once.

- While fewer than K calls have been answered, the call is one of the parent's. It is
  hashed as the parent's recorder hashed it, with the parent's model, and answered with
  the parent's recorded response, under the replay's checks (`fivefold.agents.replay`):
  the same worker and the same request hash, in order. A request that differs fails the
  call with `ReplayError`, naming the call's number and both hashes, and so does every
  later call.
- Once K calls have been answered, every later call goes to the next model: the scripted
  model or the live adapter, built to take no `think_ms` of its own.

`BranchPlayer.finish`, once the run's agents are done, raises the first failure, or a
`ReplayError` if the run made fewer than K calls: the run fails, as a replay does. A run
that asks for more calls than the parent's cassette holds is no failure here: with K at the
cassette's length, the next model answers them.

The scripted model can continue only a conversation it could have made itself: it checks
that each tool result answers its own call ids (`fivefold.agents.scripted`). So a branch to
the scripted model from a parent whose replies came from another model, such as a live one
with random tool call ids, ends its worker with `ScriptError` at the first call after the
branch point, unless K is 0.

Checked in langchain-core 1.8.0 (`language_models/chat_models.py`): a `BaseChatModel`
subclass implements `_generate`, `_agenerate` and `_llm_type`; `ainvoke(input, config=None,
*, stop=None, **kwargs)` passes `stop` and the other arguments on to `_agenerate`; one with no
`_astream` always answers through `_agenerate`; `bind_tools(tools, *, tool_choice=None,
**kwargs)` returns a `Runnable` of the model with the tools bound.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import JsonValue, PrivateAttr

from fivefold.agents.replay import CassettePlayer, ReplayError
from fivefold.recorder.cassette import (
    Cassette,
    Tool,
    plain_object,
    request_hash,
    request_params,
    tool_schemas,
)

Think = Callable[[], Awaitable[None]]
"""Takes `think_ms` of virtual time."""


class BranchError(ValueError):
    """A branch that cannot start: its branch point is not one of the parent's calls."""


def check_branch_point(parent: Cassette, point: int) -> None:
    """Refuse a branch point that is not from 0 to the number of calls in `parent`."""
    calls = len(parent.calls)
    if not 0 <= point <= calls:
        raise BranchError(
            f"the branch point must be from 0 to {calls}, the model calls in the parent's"
            f" cassette, not {point}"
        )


class BranchPlayer:
    """Serves the parent cassette's first `point` calls, in order, to every agent worker
    of one branch run, then lets every later call through to the next model."""

    def __init__(self, parent: Cassette, point: int) -> None:
        check_branch_point(parent, point)
        self.parent = parent
        self.point = point
        self._player = CassettePlayer(
            Cassette(run=parent.run, model=parent.model, calls=parent.calls[:point])
        )

    @property
    def served(self) -> int:
        """How many of the parent's calls the branch has replayed."""
        return self._player.served

    @property
    def replaying(self) -> bool:
        """Whether the next call to be answered is one of the parent's: fewer than K calls
        were answered, or the replay of the parent's calls already failed."""
        return self._player.failure is not None or self._player.served < self.point

    def answer(self, worker: str, request: str) -> AIMessage:
        """The parent's response to the next of its calls, if `worker` asked it as
        recorded. `request` is the request's hash with the parent's model."""
        return self._player.answer(worker, request)

    def finish(self) -> None:
        """Raise unless the run replayed the parent's first K calls, each as recorded."""
        if self._player.failure is not None:
            raise self._player.failure
        served = self._player.served
        if served < self.point:
            raise ReplayError(
                f"the branch made {served} model calls, but it replays the parent's first"
                f" {self.point}: calls {served + 1} to {self.point} were never asked",
                call=served + 1,
                recorded=self.parent.calls[served].request,
            )


class BranchModel(BaseChatModel):
    """A chat model that answers the parent's first K calls from its cassette and every
    later call with the next model (0009)."""

    _player: BranchPlayer = PrivateAttr()
    _next: BaseChatModel = PrivateAttr()
    _bound: Runnable[LanguageModelInput, AIMessage] = PrivateAttr()
    _worker: str = PrivateAttr()
    _think: Think = PrivateAttr()
    _tools: list[JsonValue] = PrivateAttr()
    _with_tools: dict[str, JsonValue] = PrivateAttr()

    def __init__(
        self, player: BranchPlayer, next_model: BaseChatModel, *, worker: str, think: Think
    ) -> None:
        """Branch `player`'s parent for the agent worker `worker`. `next_model` answers
        the calls after the branch point and must take no `think_ms` of its own: this
        model takes `think` once per call, whichever way the call goes."""
        super().__init__()
        self._player = player
        self._next = next_model
        self._bound = next_model
        self._worker = worker
        self._think = think
        self._tools = []
        self._with_tools = {}

    @property
    def _llm_type(self) -> str:
        return "fivefold-branch"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("the branch model runs on the scheduler's loop only")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        await self._think()
        if self._player.replaying:
            params = request_params(self._player.parent.model, self._with_tools, stop, kwargs)
            request = request_hash(messages, tools=self._tools, params=params)
            reply = self._player.answer(self._worker, request)
        else:
            reply = await self._bound.ainvoke(messages, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Tool], **kwargs: Any) -> BranchModel:
        """Bind `tools` to the next model, and hash them with every replayed request, as
        the parent's recorder did."""
        bound = BranchModel(self._player, self._next, worker=self._worker, think=self._think)
        bound._bound = self._next.bind_tools(tools, **kwargs)
        bound._tools = tool_schemas(tools)
        bound._with_tools = plain_object(kwargs, "what was bound with the tools")
        return bound
