"""The replay adapter (0009): serves a cassette's responses in order.

All of a run's replay models share one `CassettePlayer`, which hands out the cassette's
calls in the order the recorded run made them. Each model call first takes `think_ms` of
virtual time, as with every adapter (0009). It then hashes the request as the recorder did
(`fivefold.recorder.cassette`). If the next recorded call has that hash and was made by
the same agent worker, the call answers with the recorded response.

Otherwise the replay has failed. The call raises `ReplayError`, naming the call's number
and both hashes, and so does every later call. The same happens when the run asks for more
calls than the cassette holds. `CassettePlayer.finish`, once the run's agents are done,
raises the first failure, or a `ReplayError` if the run left recorded calls unasked: the
run fails (0009). The adapter makes no network call and spends no tokens.

Checked in langchain-core 1.8.0: `BaseChatModel` subclasses implement `_generate`,
`_agenerate` and `_llm_type` (`language_models/chat_models.py`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NoReturn

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import JsonValue, PrivateAttr

from fivefold.recorder.cassette import (
    Cassette,
    Tool,
    plain_object,
    request_hash,
    request_params,
    response_message,
    tool_schemas,
)


class ReplayError(RuntimeError):
    """The run did not ask the model what the cassette recorded, so the run fails (0009).

    `call` is the number of the model call where the replay failed, counted from 1 across
    the run's workers. `recorded` and `asked` are the recorded request's hash and the
    hash of the request the run made, when there are two to compare.
    """

    def __init__(
        self, message: str, *, call: int, recorded: str | None = None, asked: str | None = None
    ) -> None:
        super().__init__(message)
        self.call = call
        self.recorded = recorded
        self.asked = asked


class CassettePlayer:
    """Serves one cassette's calls, in order, to every agent worker of one run."""

    def __init__(self, cassette: Cassette) -> None:
        self.cassette = cassette
        self._served = 0
        self.failure: ReplayError | None = None
        """The first failure, if the replay failed."""

    @property
    def served(self) -> int:
        """How many calls the player has answered."""
        return self._served

    def answer(self, worker: str, request: str) -> AIMessage:
        """The response to the next recorded call, if `worker` asked it as recorded."""
        if self.failure is not None:
            raise self.failure
        number = self._served + 1
        calls = self.cassette.calls
        if number > len(calls):
            self._fail(
                ReplayError(
                    f"model call {number} ({worker}): the cassette holds only {len(calls)}"
                    f" calls; the request's hash is {request}",
                    call=number,
                    asked=request,
                )
            )
        recorded = calls[number - 1]
        if recorded.request != request or recorded.worker != worker:
            self._fail(
                ReplayError(
                    f"model call {number}: {worker} asked a request with hash {request},"
                    f" but the cassette recorded {recorded.worker} asking one with hash"
                    f" {recorded.request}",
                    call=number,
                    recorded=recorded.request,
                    asked=request,
                )
            )
        self._served = number
        return response_message(recorded.response)

    def finish(self) -> None:
        """Raise unless the run asked for every recorded call, each as it was recorded."""
        if self.failure is not None:
            raise self.failure
        total = len(self.cassette.calls)
        if self._served < total:
            raise ReplayError(
                f"the run made {self._served} model calls, but the cassette holds {total}:"
                f" calls {self._served + 1} to {total} were never asked",
                call=self._served + 1,
                recorded=self.cassette.calls[self._served].request,
            )

    def _fail(self, error: ReplayError) -> NoReturn:
        self.failure = error
        raise error


class ReplayModel(BaseChatModel):
    """A chat model whose every reply is a cassette's next recorded response (0009)."""

    _player: CassettePlayer = PrivateAttr()
    _worker: str = PrivateAttr()
    _think: Callable[[], Awaitable[None]] = PrivateAttr()
    _tools: list[JsonValue] = PrivateAttr()
    _with_tools: dict[str, JsonValue] = PrivateAttr()

    def __init__(
        self, player: CassettePlayer, *, worker: str, think: Callable[[], Awaitable[None]]
    ) -> None:
        """Replay `player`'s cassette to the agent worker `worker`."""
        super().__init__()
        self._player = player
        self._worker = worker
        self._think = think
        self._tools = []
        self._with_tools = {}

    @property
    def _llm_type(self) -> str:
        return "fivefold-replay"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("the replay model runs on the scheduler's loop only")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        await self._think()
        params = request_params(self._player.cassette.model, self._with_tools, stop, kwargs)
        request = request_hash(messages, tools=self._tools, params=params)
        reply = self._player.answer(self._worker, request)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Tool], **kwargs: Any) -> ReplayModel:
        """Hash `tools` with every request, as the recording wrapper did."""
        bound = ReplayModel(self._player, worker=self._worker, think=self._think)
        bound._tools = tool_schemas(tools)
        bound._with_tools = plain_object(kwargs, "what was bound with the tools")
        return bound
