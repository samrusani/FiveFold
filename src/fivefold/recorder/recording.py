"""The recording wrapper (0009): a chat model that answers with the model it wraps, and
records each request and response into a cassette.

The wrapper hands every call to the wrapped model's `ainvoke`, with the same messages and
arguments. It returns the same reply values, with tool argument object keys ordered as in
the cassette's canonical JSON. This keeps validation errors stable when a cassette is
reloaded; the cassette format and request-hashing algorithm stay the same. It takes no virtual
time of its own: the wrapped model takes its `think_ms` (0009). A call
that raises records nothing. Each worker incarnation gets its own wrapper, and all of a
run's wrappers share one `CassetteRecorder`, which keeps the calls in the order they are
answered.

It wraps any chat model: the scripted adapter when a run asks to record, and the live
adapter always (`fivefold.agents.live`). Binding tools binds them to the wrapped model,
and adds their schemas and what was bound with them to every request the wrapper records.
The agent's graph binds the worker's tools to every adapter (`fivefold.agents.worker`), so
every recorded request carries their schemas.

Checked in langchain-core 1.8.0 (`language_models/chat_models.py`): `BaseChatModel.ainvoke
(input, config=None, *, stop=None, **kwargs)` returns the message of the first generation;
a subclass implements `_generate`, `_agenerate` and `_llm_type`, and one with no `_astream`
always answers through `_agenerate`; `bind_tools(tools, *, tool_choice=None, **kwargs)`
returns a `Runnable` of the model with the tools bound.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import JsonValue, PrivateAttr

from fivefold.recorder.cassette import (
    Call,
    Cassette,
    ModelInfo,
    RunHeader,
    Tool,
    canonical_json,
    plain_object,
    request_hash,
    request_params,
    response_json,
    tool_schemas,
)


class CassetteRecorder:
    """Collects one run's model calls, in the order the model answers them."""

    def __init__(self, model: ModelInfo) -> None:
        self.model = model
        self._calls: list[Call] = []

    @property
    def calls(self) -> tuple[Call, ...]:
        """The calls recorded so far."""
        return tuple(self._calls)

    def record(self, worker: str, request: str, reply: AIMessage) -> None:
        """Record that `worker` asked the request whose hash is `request`, and the reply."""
        self._calls.append(Call(worker=worker, request=request, response=response_json(reply)))

    def cassette(self, run: RunHeader) -> Cassette:
        """The cassette of the calls so far, recorded from `run`."""
        return Cassette(run=run, model=self.model, calls=tuple(self._calls))


class RecordingModel(BaseChatModel):
    """A chat model that answers with the model it wraps and records each call (0009)."""

    _model: BaseChatModel = PrivateAttr()
    _bound: Runnable[LanguageModelInput, AIMessage] = PrivateAttr()
    _recorder: CassetteRecorder = PrivateAttr()
    _worker: str = PrivateAttr()
    _tools: list[JsonValue] = PrivateAttr()
    _with_tools: dict[str, JsonValue] = PrivateAttr()

    def __init__(self, model: BaseChatModel, recorder: CassetteRecorder, *, worker: str) -> None:
        """Wrap `model` for the agent worker `worker`, recording into `recorder`."""
        super().__init__()
        self._model = model
        self._bound = model
        self._recorder = recorder
        self._worker = worker
        self._tools = []
        self._with_tools = {}

    @property
    def _llm_type(self) -> str:
        return "fivefold-recording"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("the recording model runs on the scheduler's loop only")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        params = request_params(self._recorder.model, self._with_tools, stop, kwargs)
        request = request_hash(messages, tools=self._tools, params=params)
        reply = await self._bound.ainvoke(messages, stop=stop, **kwargs)
        self._recorder.record(self._worker, request, reply)
        if reply.tool_calls:
            calls = [
                {**call, "args": json.loads(canonical_json(call["args"]))}
                for call in reply.tool_calls
            ]
            reply = reply.model_copy(update={"tool_calls": calls})
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Tool], **kwargs: Any) -> RecordingModel:
        """Bind `tools` to the wrapped model, and record their schemas with every call."""
        bound = RecordingModel(self._model, self._recorder, worker=self._worker)
        bound._bound = self._model.bind_tools(tools, **kwargs)
        bound._tools = tool_schemas(tools)
        bound._with_tools = plain_object(kwargs, "what was bound with the tools")
        return bound
