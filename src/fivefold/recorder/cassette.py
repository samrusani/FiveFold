"""Model cassettes (0009): the model calls of one run, in order, as one stable file.

A cassette holds what a replay needs to serve a run its model's answers, and to check
that it serves the same run:

- `format`: `fivefold-cassette/1`.
- `run`: the run it was recorded from, as `harness.run` records it: the run id, the
  scenario's id and digest, the configuration, the contract level, the seed, the
  topology, and the run's parameters (the scenario's parameters, the fault plan and the
  workers). A branch's header also names its parent's run id and its branch point
  (`parent_run_id` and `branch_point`); any other run's leaves both out, so its file is
  the same as before branches existed.
- `model`: the model that answered: its adapter (`scripted` or `live`), its name and its
  parameters. A branch names the model it switched to, and hashes every call with it,
  the calls it replayed from its parent included (`fivefold.agents.branch`).
- `calls`: every model call, in the order the run's workers made them: the worker that
  asked, the hash of the request, and the response.

The request hash is the SHA-256 of the request's canonical JSON,
`{"messages": [...], "params": {...}, "tools": [...]}`:

- messages keep what a model is sent: the type, the content, the name, an AI message's
  tool calls, a tool message's call id and status, a chat message's role, and any
  provider fields in `additional_kwargs`. Never their ids: LangChain and LangGraph draw
  those at random.
- tools are the schemas bound to the model, in the OpenAI tool format that langchain-core
  writes for every kind of tool. The agent's graph binds the worker's tools to every
  adapter, so a run's requests carry them.
- params are the model's name and parameters, what was bound with the tools (such as
  `tool_choice`), and the call's own arguments, `stop` among them.

Canonical JSON has sorted keys, no spaces, non-ASCII characters as they are, and no NaN or
infinity. The file is the cassette as JSON with sorted keys, two-space indents, non-ASCII
characters as they are and a final newline, so one cassette has one text. Its digest, the
SHA-256 of that text, is what `harness.run.cassette_digest` records. Loading refuses a
file in any other form, so a file's SHA-256 is always its cassette's digest.

Checked in langchain-core 1.8.0: the message fields (`messages/base.py`, `messages/ai.py`,
`messages/tool.py`, `messages/chat.py`); `AIMessage`'s validator, which gives every tool
call its `type` (`messages/ai.py`); `convert_to_openai_tool(tool, *, strict=None)`
(`utils/function_calling.py`). Checked in pydantic 2.14.3: `JsonValue` (`types.py`),
`BaseModel.model_validate_json` (`main.py`), `Field(ge=...)` (`fields.py`) and
`model_validator(mode="after")` (`functional_validators.py`).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from langchain_core.messages import AIMessage, BaseMessage, ChatMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

FORMAT: Final = "fivefold-cassette/1"
"""The format a cassette file names, so a later format can refuse an old file clearly."""

BRANCH_FIELDS: Final = ("parent_run_id", "branch_point")
"""The header fields only a branch's cassette writes (0009, Playback and branching)."""

Tool = dict[str, Any] | type | Callable[..., Any] | BaseTool
"""Anything `bind_tools` takes (langchain-core 1.8.0, `BaseChatModel.bind_tools`)."""


class CassetteError(ValueError):
    """A file that is not a cassette in its canonical form."""


# Canonical JSON ---------------------------------------------------------------------------


def plain(value: object, where: str = "value") -> JsonValue:
    """Copy mappings and sequences to plain JSON, preserving order and rejecting cycles."""
    root: list[JsonValue] = []
    pending: list[
        tuple[Iterator[tuple[object, object]], dict[str, JsonValue] | list[JsonValue], str, int]
    ] = [(iter([(0, value)]), root, where, id(root))]
    ancestors = {id(root)}
    while pending:
        items, target, path, identity = pending[-1]
        entry = next(items, None)
        if entry is None:
            pending.pop()
            ancestors.remove(identity)
            continue

        key, item = entry
        if isinstance(target, dict):
            if not isinstance(key, str):
                raise TypeError(f"{path} has a key that is not text: {key!r}")
            child_path = f"{path}.{key}"
        else:
            child_path = where if target is root else f"{path}[{key}]"

        if item is None or isinstance(item, str | bool | int):
            child: JsonValue = item
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{child_path} is {item}, which JSON cannot hold")
            child = item
        elif isinstance(item, Mapping | list | tuple):
            if id(item) in ancestors:
                raise ValueError(f"{child_path} contains a cycle, which JSON cannot hold")
            if isinstance(item, Mapping):
                child = {}
                children = iter(item.items())
            else:
                child = []
                children = enumerate(item)
            ancestors.add(id(item))
            pending.append((children, child, child_path, id(item)))
        else:
            raise TypeError(f"{child_path} is a {type(item).__name__}, which JSON cannot hold")
        if isinstance(target, dict):
            assert isinstance(key, str)
            target[key] = child
        else:
            target.append(child)
    return root[0]


def plain_object(value: Mapping[Any, Any], where: str = "value") -> dict[str, JsonValue]:
    """A mapping as a plain JSON object; see `plain`."""
    data = plain(value, where)
    assert isinstance(data, dict)
    return data


def canonical_json(value: object) -> str:
    """`value` as canonical JSON: sorted keys, no spaces, non-ASCII characters as they are."""
    try:
        return json.dumps(
            plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except RecursionError:
        raise CassetteError("the value exceeds the cassette JSON encoder's nesting limit") from None


def sha256(text: str) -> str:
    """The SHA-256 of `text` in UTF-8, as 64 lowercase hex digits."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Requests and responses -------------------------------------------------------------------


def message_json(message: BaseMessage) -> dict[str, JsonValue]:
    """What a model is sent of `message`, as plain JSON. Never its id, which is random."""
    data: dict[str, object] = {"type": message.type, "content": message.content}
    if message.name is not None:
        data["name"] = message.name
    if message.additional_kwargs:
        data["additional_kwargs"] = message.additional_kwargs
    if isinstance(message, AIMessage):
        if message.tool_calls:
            data["tool_calls"] = [
                {"name": call["name"], "args": call["args"], "id": call["id"]}
                for call in message.tool_calls
            ]
        if message.invalid_tool_calls:
            data["invalid_tool_calls"] = list(message.invalid_tool_calls)
    if isinstance(message, ToolMessage):
        data["tool_call_id"] = message.tool_call_id
        data["status"] = message.status
    if isinstance(message, ChatMessage):
        data["role"] = message.role
    return plain_object(data, f"a {message.type} message")


def tool_schemas(tools: Sequence[Tool]) -> list[JsonValue]:
    """The schemas of the tools bound to a model, in the OpenAI tool format."""
    return [
        plain(convert_to_openai_tool(tool), f"tool {index}") for index, tool in enumerate(tools)
    ]


def request_params(
    model: ModelInfo,
    bound: Mapping[str, object],
    stop: Sequence[str] | None,
    call: Mapping[str, object],
) -> dict[str, JsonValue]:
    """The parameters of one model call: the model's name and parameters, what was bound
    with its tools, and the call's own arguments, `stop` among them."""
    params: dict[str, object] = {"model": model.name, **model.params, **bound, **call}
    if stop is not None:
        params["stop"] = list(stop)
    return plain_object(params, "the call's parameters")


def request_json(
    messages: Sequence[BaseMessage],
    *,
    tools: Sequence[JsonValue],
    params: Mapping[str, JsonValue],
) -> str:
    """A request's canonical JSON: the messages, the tools and the params."""
    request = {
        "messages": [message_json(message) for message in messages],
        "params": dict(params),
        "tools": list(tools),
    }
    return canonical_json(request)


def request_hash(
    messages: Sequence[BaseMessage],
    *,
    tools: Sequence[JsonValue],
    params: Mapping[str, JsonValue],
) -> str:
    """The SHA-256 of a request's canonical JSON (`request_json`)."""
    return sha256(request_json(messages, tools=tools, params=params))


def response_json(reply: AIMessage) -> dict[str, JsonValue]:
    """A model's reply as a cassette keeps it: everything but its id, which is random.

    Fields that are empty are left out, so the file stays short.
    """
    data: dict[str, object] = {"content": reply.content}
    if reply.tool_calls:
        data["tool_calls"] = list(reply.tool_calls)
    if reply.invalid_tool_calls:
        data["invalid_tool_calls"] = list(reply.invalid_tool_calls)
    if reply.additional_kwargs:
        data["additional_kwargs"] = reply.additional_kwargs
    if reply.response_metadata:
        data["response_metadata"] = reply.response_metadata
    if reply.usage_metadata is not None:
        data["usage_metadata"] = dict(reply.usage_metadata)
    if reply.name is not None:
        data["name"] = reply.name
    return plain_object(data, "the reply")


def response_message(response: Mapping[str, JsonValue]) -> AIMessage:
    """The reply a cassette kept, as the model returned it, without its id."""
    return AIMessage.model_validate(dict(response))


# The cassette -----------------------------------------------------------------------------


class _Part(BaseModel):
    """Base of a cassette's parts: unknown fields are refused and values never change."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RunHeader(_Part):
    """The run a cassette was recorded from, as `harness.run` records it (0009)."""

    run_id: str
    scenario_id: str
    scenario_digest: str
    config: Literal["B0", "B1", "B2", "G1", "G2"]
    contract: Literal["C0", "C1", "C2", "C3"]
    seed: int
    topology: Literal["inproc", "split"]
    params: dict[str, JsonValue]
    """The values the run used: the scenario's parameters, the fault plan and the workers."""
    parent_run_id: str | None = None
    """For a branch, the run id of the run it branched from; None for any other run."""
    branch_point: int | None = Field(default=None, ge=0)
    """For a branch, K: how many of its parent's model calls it replayed before it
    switched model; None for any other run."""

    @model_validator(mode="after")
    def _parent_and_point_together(self) -> RunHeader:
        """A branch names both its parent and its branch point; any other run neither, as
        `harness.run` requires."""
        if (self.parent_run_id is None) != (self.branch_point is None):
            raise ValueError("a branch names both its parent_run_id and its branch_point")
        return self


class ModelInfo(_Part):
    """The model that answered the calls: its adapter, its name and its parameters."""

    adapter: Literal["scripted", "live"]
    name: str
    params: dict[str, JsonValue] = Field(default_factory=dict)


class Call(_Part):
    """One model call: the worker that asked, the request's hash and the response."""

    worker: str
    request: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: dict[str, JsonValue]


class Cassette(_Part):
    """The model calls of one run, in the order the run made them (0009)."""

    format: Literal["fivefold-cassette/1"] = FORMAT
    run: RunHeader
    model: ModelInfo
    calls: tuple[Call, ...]

    def text(self) -> str:
        """The cassette's file: JSON with sorted keys, two-space indents, non-ASCII
        characters as they are, and a final newline. A run that is no branch leaves out the
        header's branch fields, so its file is the same as before branches existed."""
        data = self.model_dump(mode="json")
        if self.run.parent_run_id is None:
            for name in BRANCH_FIELDS:
                del data["run"][name]
        return (
            json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        )

    def digest(self) -> str:
        """The SHA-256 of the cassette's file, as `harness.run.cassette_digest` records it."""
        return sha256(self.text())

    @classmethod
    def from_text(cls, text: str) -> Cassette:
        """Read a cassette's file; refuse one that is not a cassette in canonical form."""
        try:
            cassette = cls.model_validate_json(text)
        except ValidationError as error:
            raise CassetteError(f"not a cassette: {error}") from error
        try:
            canonical = cassette.text()
        except ValueError as error:
            raise CassetteError(f"not a cassette: {error}") from error
        if canonical != text:
            raise CassetteError(
                "the cassette is not in canonical form: sorted keys, two-space indents, "
                "non-ASCII characters as they are, and a final newline"
            )
        return cassette

    def save(self, path: Path) -> None:
        """Write the cassette's file, in UTF-8, with no line-ending translation."""
        path.write_bytes(self.text().encode("utf-8"))

    @classmethod
    def load(cls, path: Path) -> Cassette:
        """Read and check a cassette's file."""
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as error:
            raise CassetteError(f"{path} is not UTF-8 text") from error
        return cls.from_text(text)
