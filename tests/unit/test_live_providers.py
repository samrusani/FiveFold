"""The live adapter's API providers, Anthropic and OpenAI, each through its LangChain
package (0009, Models; CLAUDE.md, live runs and secrets).

No test reaches a provider, or the network at all. Each chat model is built with a dummy
key from a monkeypatched environment, while every way to open a connection or look up a
host is closed, and it is never invoked. The tests check what building a model reads and
sends, which parameters a live run may set, what a run and its cassette record, that
every tool of both tool surfaces reaches the provider's format, and that a live run is
refused before its model is built.
"""

from __future__ import annotations

import asyncio
import re
import socket
from collections.abc import Awaitable, Sequence
from decimal import Decimal
from typing import Any, Final

import mcp_types as types
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableBinding
from langchain_core.tools import BaseTool, StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import JsonValue

from fivefold.agents.live import (
    ANTHROPIC_PARAMS,
    OPENAI_PARAMS,
    PROVIDERS,
    LiveError,
    Provider,
    SpendMeter,
    Think,
    check_provider,
    provider_model,
)
from fivefold.agents.tools import BASELINE_TOOLS, STATUS_TOOLS
from fivefold.domain.prices import PriceFile, PriceFileError, price_file_from_text
from fivefold.gateway.tools import TOOLS
from fivefold.harness.log import RunRecord
from fivefold.harness.models import Live, LiveRefused, RunModels
from fivefold.recorder.cassette import ModelInfo
from fivefold.recorder.recording import RecordingModel

API_PROVIDERS: Final = ("anthropic", "openai")
DUMMY_KEY: Final = "fivefold-dummy-key-never-sent"
KEYS: Final = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
CHAT_MODELS: Final[dict[str, type[BaseChatModel]]] = {
    "anthropic": ChatAnthropic,
    "openai": ChatOpenAI,
}
GENERATION: Final = {
    "anthropic": {"max_tokens", "temperature", "top_k", "top_p", "thinking", "reasoning_effort"},
    "openai": {
        "max_tokens",
        "temperature",
        "top_p",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "reasoning_effort",
    },
}
"""What each provider's parameters change: the sampling, the seed, the output limit and
the reasoning. Changing a list means changing this test too."""

SETTING: Final = re.compile(
    r"key|secret|url|base|header|proxy|client|kwargs|body|organization|project|timeout"
    r"|retries|query|socket|stream|mcp|betas|gateway"
)
"""Names of the settings a live run must never set: keys, URLs, headers, clients,
pass-through mappings and transport settings."""

ENVIRONMENT: Final = (
    "ANTHROPIC_API_URL",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROXY",
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT_ID",
    "OPENAI_PROXY",
    "OPENAI_CUSTOM_HEADERS",
    "LANGSMITH_GATEWAY",
)
"""Variables that would change where a provider's model sends its requests."""


@pytest.fixture(autouse=True)
def sealed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close every way to the network, stop every provider model from answering, and give
    each provider a dummy key and no other setting from the environment."""

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no test may reach the network")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr("socket.socket." + name, no_network)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr("socket." + name, no_network)

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no test may invoke a model provider")

    for chat_model in CHAT_MODELS.values():
        for method in ("_generate", "_agenerate", "_stream", "_astream"):
            monkeypatch.setattr(chat_model, method, never)
    for name in ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name in KEYS.values():
        monkeypatch.setenv(name, DUMMY_KEY)


def price_file(provider: str) -> PriceFile:
    """A price file for `provider`'s model `m`, a name no provider uses, at test prices."""
    return price_file_from_text(
        "price_date: 2026-11-15\n"
        "prices:\n"
        f"  {provider}:\n"
        "    m:\n"
        "      input_usd_per_million_tokens: 1\n"
        "      output_usd_per_million_tokens: 2\n"
    )


def quote_tool() -> BaseTool:
    async def get_quote(**arguments: Any) -> str:
        return ""

    return StructuredTool.from_function(
        coroutine=get_quote,
        name="get_quote",
        description="Read a supplier's quote.",
        args_schema={"type": "object", "properties": {"sku": {"type": "string"}}},
    )


def bound_tool_names(provider: str, chat: BaseChatModel, tools: Sequence[BaseTool]) -> list[str]:
    """The names of `tools` as `chat` binds them, in its provider's tool format."""
    bound = chat.bind_tools(tools)
    assert isinstance(bound, RunnableBinding)
    formatted: list[dict[str, Any]] = bound.kwargs["tools"]
    if provider == "anthropic":
        return [tool["name"] for tool in formatted]
    return [tool["function"]["name"] for tool in formatted]


# Building each provider's model --------------------------------------------------------------


def test_no_test_here_can_reach_the_network() -> None:
    """The seal holds: looking up a host and opening a connection fail, in plain code and
    on an event loop alike, so a model that sent anything while it was built would fail
    its test."""
    with pytest.raises(AssertionError, match="no test may reach the network"):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(AssertionError, match="no test may reach the network"):
        socket.create_connection(("127.0.0.1", 9))
    with (
        socket.socket() as sock,
        pytest.raises(AssertionError, match="no test may reach the network"),
    ):
        sock.connect(("127.0.0.1", 9))
    with pytest.raises(AssertionError, match="no test may reach the network"):
        asyncio.run(asyncio.open_connection("127.0.0.1", 9))


def test_the_anthropic_chat_model_is_built_from_its_environment_and_sends_nothing() -> None:
    params: dict[str, JsonValue] = {
        "max_tokens": 1024,
        "temperature": 0.0,
        "top_k": 40,
        "top_p": 0.9,
        "thinking": {"type": "disabled"},
        "reasoning_effort": "low",
    }
    assert set(params) == ANTHROPIC_PARAMS
    chat = provider_model("anthropic", "m", params)
    assert isinstance(chat, ChatAnthropic)
    assert chat.model == "m"
    assert {name: getattr(chat, name) for name in params} == params
    assert chat.model_kwargs == {}, "every parameter sets a field; none passes through"
    assert chat.anthropic_api_key.get_secret_value() == DUMMY_KEY, "read from the environment"
    assert chat.anthropic_api_url == "https://api.anthropic.com"
    assert chat.default_headers == {"Connection": "close"}, "no connection outlives its run"
    assert chat.anthropic_proxy is None
    assert DUMMY_KEY not in repr(chat) and DUMMY_KEY not in str(chat.model_dump())
    assert bound_tool_names("anthropic", chat, [quote_tool()]) == ["get_quote"]


def test_the_openai_chat_model_is_built_from_its_environment_and_sends_nothing() -> None:
    params: dict[str, JsonValue] = {
        "max_tokens": 1024,
        "temperature": 0.0,
        "top_p": 0.9,
        "seed": 7,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "reasoning_effort": "low",
    }
    assert set(params) == OPENAI_PARAMS
    chat = provider_model("openai", "m", params)
    assert isinstance(chat, ChatOpenAI)
    assert chat.model_name == "m"
    assert {name: getattr(chat, name) for name in params} == params
    assert chat.model_kwargs == {}, "every parameter sets a field; none passes through"
    key = chat.openai_api_key
    assert key is not None and not callable(key) and key.get_secret_value() == DUMMY_KEY
    assert chat.openai_api_base is None, "the openai client picks OpenAI's API"
    assert chat.default_headers == {"Connection": "close"}, "no connection outlives its run"
    assert chat.root_async_client.default_headers["Connection"] == "close"
    assert chat.extra_body is None
    assert chat.include_response_headers is False, "no response header reaches a cassette"
    assert DUMMY_KEY not in repr(chat) and DUMMY_KEY not in str(chat.model_dump())
    assert bound_tool_names("openai", chat, [quote_tool()]) == ["get_quote"]


@pytest.mark.parametrize("provider", API_PROVIDERS)
def test_a_value_a_provider_refuses_is_refused_in_one_line(provider: str) -> None:
    with pytest.raises(LiveError, match=f"{provider} refuses temperature") as refused:
        provider_model(provider, "m", {"temperature": "hot"})
    assert "\n" not in str(refused.value)


# The parameters ------------------------------------------------------------------------------


@pytest.mark.parametrize("provider", API_PROVIDERS)
def test_each_provider_accepts_only_generation_parameters(provider: str) -> None:
    """A live run may set what the model generates and nothing else: no key, base URL,
    header, client or pass-through mapping, so none can reach a cassette, which records
    every parameter, `harness.run` or a printed line."""
    entry, chat_model = PROVIDERS[provider], CHAT_MODELS[provider]
    assert entry.params == GENERATION[provider]
    assert entry.keys == (KEYS[provider],)
    fields = chat_model.model_fields
    assert entry.params <= set(fields), "each is a field of the chat model, which it applies"
    spellings = set(fields) | {field.alias for field in fields.values() if field.alias}
    settings = sorted(name for name in spellings if SETTING.search(name))
    assert {"api_key", "base_url", "default_headers", "model_kwargs"} <= set(settings)
    secrets = set(provider_model(provider, "m", {}).lc_secrets)
    assert secrets and secrets <= set(settings)
    assert not entry.params & set(settings)
    for name in settings:
        with pytest.raises(LiveError, match=f"accepts the parameters .*, not {name}$"):
            check_provider(provider, {name: None})


@pytest.mark.parametrize("provider", API_PROVIDERS)
@pytest.mark.parametrize("surface", ["gateway", "baseline"])
def test_each_provider_takes_every_tool_of_each_tool_surface(provider: str, surface: str) -> None:
    """The tools the agent's graph binds, with their real schemas, all reach the provider's
    format: the gateway's, as MCP lists them in G1 and G2, and B0's, at C1 too. Anthropic's
    model would drop a tool whose schema has a top-level `anyOf` or `oneOf`."""

    async def call(**arguments: Any) -> str:
        return ""

    if surface == "gateway":
        listed = [
            types.Tool(
                name=tool.name, description=tool.description, input_schema=tool.input_schema()
            )
            for tool in TOOLS
        ]
        tools = [
            StructuredTool.from_function(
                coroutine=call,
                name=tool.name,
                description=tool.description or tool.name,
                args_schema=dict(tool.input_schema),
            )
            for tool in listed
        ]
    else:
        tools = [
            StructuredTool.from_function(
                coroutine=call, name=spec.name, description=spec.description, args_schema=spec.model
            )
            for spec in BASELINE_TOOLS + STATUS_TOOLS
        ]
    chat = provider_model(provider, "m", {})
    assert bound_tool_names(provider, chat, tools) == [tool.name for tool in tools]


# A live run ----------------------------------------------------------------------------------


async def no_time() -> None:
    """These tests have no scheduler."""


async def pass_through(awaitable: Awaitable[AIMessage]) -> AIMessage:
    return await awaitable


def never_scripted(its_think: Think) -> BaseChatModel:
    raise AssertionError("a live run builds no scripted model")


@pytest.mark.parametrize("provider", API_PROVIDERS)
def test_a_live_run_of_each_provider_records_its_parameters_and_never_its_key(
    provider: str,
) -> None:
    params: dict[str, JsonValue] = {"temperature": 0, "max_tokens": 256}
    flag_and_key = {"FIVEFOLD_LIVE": "1", KEYS[provider]: DUMMY_KEY}
    meter = SpendMeter(Decimal(1), price_file(provider))
    models = RunModels(Live(provider, "m", params, spend=meter), environ=flag_and_key)
    assert (models.adapter, models.name) == ("live", f"{provider}:m")
    model = models.chat_model("agent-1", scripted=never_scripted, think=no_time, hold=pass_through)
    assert isinstance(model, RecordingModel)
    model.bind_tools([quote_tool()])
    run = RunRecord(
        run_id="unit",
        scenario_id="dev-001-single-po",
        scenario_digest="00" * 32,
        config="G2",
        contract="C2",
        seed=7,
        model_adapter=models.adapter,
        model=models.name,
        params={},
    )
    digest = models.stopped(run)
    assert models.cassette is not None and digest == models.cassette.digest()
    assert models.cassette.model == ModelInfo(adapter="live", name=f"{provider}:m", params=params)
    assert DUMMY_KEY not in models.cassette.text()
    assert DUMMY_KEY not in repr(run)
    assert meter.calls == 0


@pytest.mark.parametrize("provider", API_PROVIDERS)
def test_a_live_run_of_each_provider_is_refused_before_its_model_is_built(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No flag, no spend cap, an unpriced model, no key and a setting as a parameter are
    each refused before the provider's model is built, so nothing runs."""
    entry = PROVIDERS[provider]
    built: list[str] = []

    def spy(model: str, params: Any) -> BaseChatModel:
        built.append(model)
        return entry.build(model, params)

    monkeypatch.setitem(PROVIDERS, provider, Provider(spy, entry.params, entry.keys))
    key = KEYS[provider]
    meter = SpendMeter(Decimal(1), price_file(provider))
    flag, flag_and_key = {"FIVEFOLD_LIVE": "1"}, {"FIVEFOLD_LIVE": "1", key: DUMMY_KEY}
    refusals: list[tuple[Live, dict[str, str], type[ValueError], str]] = [
        (Live(provider, "m", spend=meter), {key: DUMMY_KEY}, LiveRefused, "FIVEFOLD_LIVE=1"),
        (Live(provider, "m"), flag_and_key, LiveRefused, "needs a spend cap in USD"),
        (
            Live(provider, "other", spend=meter),
            flag_and_key,
            PriceFileError,
            f"no price for {provider}:other",
        ),
        (
            Live(provider, "m", spend=meter),
            flag,
            LiveRefused,
            f"a live run of {provider} needs its API key in {key}",
        ),
        (Live(provider, "m", spend=meter), {**flag, key: ""}, LiveRefused, f"key in {key}"),
        (
            Live(provider, "m", {"base_url": "http://x"}, spend=meter),
            flag_and_key,
            LiveError,
            "not base_url",
        ),
        (
            Live(provider, "m", {"api_key": DUMMY_KEY}, spend=meter),
            flag_and_key,
            LiveError,
            "not api_key",
        ),
    ]
    for spec, environ, error, message in refusals:
        with pytest.raises(error, match=re.escape(message)) as refused:
            RunModels(spec, environ=environ)
        assert DUMMY_KEY not in str(refused.value)
    assert built == []
    RunModels(Live(provider, "m", spend=meter), environ=flag_and_key)
    assert built == ["m"], "with everything in place, the model is built"


def test_a_provider_with_no_key_variables_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama needs no key, nor does a provider registered without key variables."""
    events: list[str] = []

    def build(model: str, params: Any) -> BaseChatModel:
        events.append(model)
        return ChatAnthropic(model=model)

    monkeypatch.setitem(PROVIDERS, "keyless", Provider(build, frozenset()))
    meter = SpendMeter(Decimal(1), price_file("keyless"))
    RunModels(Live("keyless", "m", spend=meter), environ={"FIVEFOLD_LIVE": "1"})
    assert events == ["m"]
    assert PROVIDERS["ollama"].keys == ()
