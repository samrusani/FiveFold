"""The cassette format and the request hash (0009, Models; `fivefold.recorder.cassette`).

A cassette is one stable file: sorted keys, one canonical encoding, and a digest that is
the SHA-256 of the file. The request hash covers what a model is asked: the messages, the
tools bound to it and its parameters, but never the random ids LangChain and LangGraph give
messages. It is the same in every process, and any change to what the model is asked
changes it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ChatMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_to_dict,
)
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import JsonValue

from fivefold.recorder.cassette import (
    Call,
    Cassette,
    CassetteError,
    ModelInfo,
    RunHeader,
    canonical_json,
    request_hash,
    request_json,
    request_params,
    response_json,
    response_message,
    tool_schemas,
)

SCRIPTED = ModelInfo(adapter="scripted", name="scripted")

EXAMPLE = Cassette(
    run=RunHeader(
        run_id="dev-001-single-po.G2.C2.7",
        scenario_id="dev-001-single-po",
        scenario_digest="ab" * 32,
        config="G2",
        contract="C2",
        seed=7,
        topology="inproc",
        params={"plan": [], "workers": 1},
    ),
    model=SCRIPTED,
    calls=(
        Call(
            worker="agent-1",
            request="cd" * 32,
            response={
                "content": "",
                "tool_calls": [
                    {
                        "name": "get_quote",
                        "args": {"supplier_id": "S-1", "sku": "W-100"},
                        "id": "call-0001",
                        "type": "tool_call",
                    }
                ],
            },
        ),
        Call(worker="agent-1", request="ef" * 32, response={"content": "Réglé."}),
    ),
)

EXAMPLE_TEXT = """\
{
  "calls": [
    {
      "request": "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd",
      "response": {
        "content": "",
        "tool_calls": [
          {
            "args": {
              "sku": "W-100",
              "supplier_id": "S-1"
            },
            "id": "call-0001",
            "name": "get_quote",
            "type": "tool_call"
          }
        ]
      },
      "worker": "agent-1"
    },
    {
      "request": "efefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefef",
      "response": {
        "content": "Réglé."
      },
      "worker": "agent-1"
    }
  ],
  "format": "fivefold-cassette/1",
  "model": {
    "adapter": "scripted",
    "name": "scripted",
    "params": {}
  },
  "run": {
    "config": "G2",
    "contract": "C2",
    "params": {
      "plan": [],
      "workers": 1
    },
    "run_id": "dev-001-single-po.G2.C2.7",
    "scenario_digest": "abababababababababababababababababababababababababababababababab",
    "scenario_id": "dev-001-single-po",
    "seed": 7,
    "topology": "inproc"
  }
}
"""


# The file ---------------------------------------------------------------------------------


def test_a_cassette_is_one_canonical_text() -> None:
    """Sorted keys, two-space indents, non-ASCII as it is, and a final newline."""
    assert EXAMPLE.text() == EXAMPLE_TEXT


def test_the_digest_is_the_sha256_of_the_file(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    EXAMPLE.save(path)
    assert path.read_bytes() == EXAMPLE_TEXT.encode("utf-8")
    assert EXAMPLE.digest() == hashlib.sha256(path.read_bytes()).hexdigest()
    loaded = Cassette.load(path)
    assert loaded == EXAMPLE
    assert loaded.digest() == EXAMPLE.digest()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(json.dumps(json.loads(EXAMPLE_TEXT)), id="compact"),
        pytest.param(EXAMPLE_TEXT.rstrip("\n"), id="no final newline"),
        pytest.param(json.dumps(json.loads(EXAMPLE_TEXT), indent=2) + "\n", id="unsorted"),
        pytest.param(
            json.dumps(json.loads(EXAMPLE_TEXT), indent=2, sort_keys=True) + "\n",
            id="non-ASCII escaped",
        ),
        pytest.param(EXAMPLE_TEXT.replace("\n", "\r\n"), id="CRLF"),
    ],
)
def test_loading_refuses_any_other_form_of_the_same_cassette(text: str) -> None:
    """So the file's SHA-256 is always the cassette's digest."""
    assert Cassette.model_validate_json(text) == EXAMPLE
    with pytest.raises(CassetteError, match="canonical form"):
        Cassette.from_text(text)


def _changed(change: dict[str, Any]) -> str:
    data = json.loads(EXAMPLE_TEXT)
    data.update(change)
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_changed({"format": "fivefold-cassette/2"}), id="another format"),
        pytest.param(_changed({"extra": 1}), id="an unknown field"),
        pytest.param(
            _changed({"calls": [{"worker": "agent-1", "request": "x"}]}), id="no response"
        ),
        pytest.param(
            _changed({"calls": [{"worker": "agent-1", "request": "AB" * 32, "response": {}}]}),
            id="a hash that is not lowercase hex",
        ),
        pytest.param(_changed({"model": {"adapter": "replay", "name": "x"}}), id="a replay"),
        pytest.param("[]\n", id="not an object"),
    ],
)
def test_loading_refuses_what_is_not_a_cassette(text: str) -> None:
    with pytest.raises(CassetteError, match="not a cassette"):
        Cassette.from_text(text)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"x": math.nan}, id="NaN"),
        pytest.param({"x": math.inf}, id="infinity"),
        pytest.param({"x": {1, 2}}, id="a set"),
        pytest.param({1: "x"}, id="a key that is not text"),
        pytest.param({"x": object()}, id="an object"),
    ],
)
def test_canonical_json_refuses_what_json_cannot_hold(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json(value)


def test_canonical_json_sorts_keys_and_keeps_non_ascii() -> None:
    assert (
        canonical_json({"b": [1, 2.5, None, True], "a": "é"}) == '{"a":"é","b":[1,2.5,null,true]}'
    )


# The request hash ------------------------------------------------------------------------


def conversation() -> list[BaseMessage]:
    """A short conversation, with ids as LangChain and LangGraph would give them."""
    return [
        HumanMessage("Order 100 units of W-100.", id="9f6b2c1e-8d1c-4d1a-a1f5-6b8f3c2d1e0a"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "get_quote", "args": {"supplier_id": "S-1", "sku": "W-100"}, "id": "c-1"}
            ],
            id="lc_run--0001-0",
        ),
        ToolMessage(
            content='{"read_id": "R-0001", "version": 1}',
            tool_call_id="c-1",
            name="get_quote",
            id="2b7e9f1a-0c3d-4e5f-8a9b-1c2d3e4f5a6b",
        ),
    ]


def quote_tool(description: str = "Read a supplier's quote.") -> BaseTool:
    async def get_quote(**arguments: Any) -> str:
        return json.dumps(arguments)

    return StructuredTool.from_function(
        coroutine=get_quote,
        name="get_quote",
        description=description,
        args_schema={
            "type": "object",
            "properties": {"supplier_id": {"type": "string"}, "sku": {"type": "string"}},
            "required": ["supplier_id", "sku"],
        },
    )


def scripted_hash(messages: list[BaseMessage]) -> str:
    return request_hash(messages, tools=[], params=request_params(SCRIPTED, {}, None, {}))


PINNED = "a343c014926eacf998800a7ae60622204c99d1347696f92fb14651c75aa4d714"
"""The hash of `conversation()` for the scripted model. It pins the canonical form: when
it changes, every recorded cassette stops replaying."""


PINNED_JSON = (
    '{"messages":[{"content":"Order 100 units of W-100.","type":"human"},'
    '{"content":"","tool_calls":[{"args":{"sku":"W-100","supplier_id":"S-1"},"id":"c-1",'
    '"name":"get_quote"}],"type":"ai"},'
    '{"content":"{\\"read_id\\": \\"R-0001\\", \\"version\\": 1}","name":"get_quote",'
    '"status":"success","tool_call_id":"c-1","type":"tool"}],'
    '"params":{"model":"scripted"},"tools":[]}'
)
"""The canonical JSON that `PINNED` hashes: no ids, sorted keys, no spaces."""


def test_the_request_hash_is_pinned() -> None:
    params = request_params(SCRIPTED, {}, None, {})
    assert request_json(conversation(), tools=[], params=params) == PINNED_JSON
    assert hashlib.sha256(PINNED_JSON.encode("utf-8")).hexdigest() == PINNED
    assert scripted_hash(conversation()) == PINNED


def test_the_request_hash_leaves_out_message_ids() -> None:
    renamed = [
        message.model_copy(update={"id": f"other-{n}"}) for n, message in enumerate(conversation())
    ]
    unnamed = [message.model_copy(update={"id": None}) for message in conversation()]
    assert scripted_hash(renamed) == scripted_hash(unnamed) == scripted_hash(conversation())


HASH_FROM_STDIN = """\
import json, sys
from langchain_core.messages import messages_from_dict
from fivefold.recorder.cassette import ModelInfo, request_hash, request_params

messages = messages_from_dict(json.load(sys.stdin))
params = request_params(ModelInfo(adapter="scripted", name="scripted"), {}, None, {})
print(request_hash(messages, tools=[], params=params))
"""


@pytest.mark.parametrize("hash_seed", ["0", "1", "4242"])
def test_the_request_hash_is_the_same_in_another_process(hash_seed: str) -> None:
    """Whatever the process's string hashing, as `PYTHONHASHSEED` sets it."""
    answer = subprocess.run(
        [sys.executable, "-c", HASH_FROM_STDIN],
        input=json.dumps(messages_to_dict(conversation())),
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
    )
    assert answer.stdout.strip() == PINNED


def _replace(index: int, message: BaseMessage) -> list[BaseMessage]:
    messages = conversation()
    messages[index] = message
    return messages


Request = tuple[list[BaseMessage], list[JsonValue], dict[str, JsonValue]]
"""A request's messages, tools and parameters."""


def _variants() -> dict[str, Request]:
    """Requests that each differ from `conversation()` for the scripted model in one way."""
    base = conversation()
    params = request_params(SCRIPTED, {}, None, {})
    human, ai, tool = base
    assert isinstance(ai, AIMessage) and isinstance(tool, ToolMessage)
    call = ai.tool_calls[0]
    messages: dict[str, list[BaseMessage]] = {
        "the same": base,
        "the task's text": _replace(0, HumanMessage("Order 101 units of W-100.")),
        "a system message, not a human one": _replace(0, SystemMessage(str(human.content))),
        "a chat message's role": _replace(0, ChatMessage(content=human.content, role="user")),
        "a message's name": _replace(0, HumanMessage(human.content, name="buyer")),
        "a provider field": _replace(
            0, HumanMessage(human.content, additional_kwargs={"cache": "ephemeral"})
        ),
        "a tool call's arguments": _replace(
            1,
            AIMessage(
                content="", tool_calls=[{**call, "args": {"supplier_id": "S-2", "sku": "W-100"}}]
            ),
        ),
        "a tool call's name": _replace(
            1, AIMessage(content="", tool_calls=[{**call, "name": "get_budget"}])
        ),
        "a tool call's id": _replace(1, AIMessage(content="", tool_calls=[{**call, "id": "c-2"}])),
        "the model's text": _replace(1, AIMessage(content="Reading.", tool_calls=[call])),
        "an invalid tool call": _replace(
            1,
            AIMessage(
                content="",
                tool_calls=[call],
                invalid_tool_calls=[{"name": "x", "args": "{", "id": "c-9", "error": "bad JSON"}],
            ),
        ),
        "a tool result": _replace(
            2,
            ToolMessage(
                content='{"read_id": "R-0002", "version": 1}', tool_call_id="c-1", name="get_quote"
            ),
        ),
        "a tool result's status": _replace(
            2,
            ToolMessage(content=tool.content, tool_call_id="c-1", name="get_quote", status="error"),
        ),
        "a tool result's call id": _replace(
            2, ToolMessage(content=tool.content, tool_call_id="c-2", name="get_quote")
        ),
        "one message fewer": base[:2],
        "one message more": [*base, HumanMessage("And hurry.")],
        "the order of the messages": [base[0], base[2], base[1]],
    }
    variants: dict[str, Request] = {name: (value, [], params) for name, value in messages.items()}
    variants["a tool bound"] = (base, tool_schemas([quote_tool()]), params)
    variants["a tool's description"] = (base, tool_schemas([quote_tool("Read a quote.")]), params)
    variants["two tools bound"] = (base, tool_schemas([quote_tool(), quote_tool()]), params)
    variants["another model"] = (
        base,
        [],
        request_params(SCRIPTED.model_copy(update={"name": "other"}), {}, None, {}),
    )
    variants["a model parameter"] = (
        base,
        [],
        request_params(SCRIPTED.model_copy(update={"params": {"temperature": 0.0}}), {}, None, {}),
    )
    variants["a stop sequence"] = (base, [], request_params(SCRIPTED, {}, ["END"], {}))
    variants["a tool choice"] = (
        base,
        [],
        request_params(SCRIPTED, {"tool_choice": "any"}, None, {}),
    )
    variants["a call argument"] = (base, [], request_params(SCRIPTED, {}, None, {"max_tokens": 10}))
    return variants


def test_any_change_to_the_messages_tools_or_parameters_changes_the_hash() -> None:
    hashes = {
        name: request_hash(messages, tools=tools, params=params)
        for name, (messages, tools, params) in _variants().items()
    }
    assert hashes["the same"] == PINNED
    assert len(set(hashes.values())) == len(hashes), "two different requests share a hash"


def test_tools_are_hashed_in_the_openai_format() -> None:
    (schema,) = tool_schemas([quote_tool()])
    assert schema == {
        "type": "function",
        "function": {
            "name": "get_quote",
            "description": "Read a supplier's quote.",
            "parameters": {
                "type": "object",
                "properties": {"supplier_id": {"type": "string"}, "sku": {"type": "string"}},
                "required": ["supplier_id", "sku"],
            },
        },
    }


# Responses ------------------------------------------------------------------------------


def test_a_reply_is_kept_whole_but_for_its_random_id() -> None:
    reply = AIMessage(
        content="Ordering.",
        tool_calls=[{"name": "submit_purchase_order", "args": {"quantity": 100}, "id": "c-4"}],
        additional_kwargs={"provider": {"x": 1}},
        response_metadata={"stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        name="buyer",
        id="lc_run--0004-0",
    )
    kept = response_json(reply)
    assert "id" not in kept
    assert response_message(kept) == reply.model_copy(update={"id": None})
    assert response_message(json.loads(json.dumps(kept))) == reply.model_copy(update={"id": None})


def test_a_short_reply_keeps_only_its_content() -> None:
    assert response_json(AIMessage(content="Done.", id="lc_run--0007-0")) == {"content": "Done."}
