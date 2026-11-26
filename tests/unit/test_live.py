"""The live model adapter outside a run: price files, the spend cap, the flag and the
Ollama chat model (0004, 0009; CLAUDE.md, live runs).

No test reaches a model provider. The Ollama chat model is built and checked but never
invoked, and every live call goes to a fake chat model.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Sequence
from decimal import Decimal
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableBinding
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_ollama import ChatOllama
from pydantic import PrivateAttr

from fivefold.agents.live import (
    OLLAMA_PARAMS,
    PROVIDERS,
    SPEND_CAP,
    LiveError,
    LiveModel,
    Provider,
    SpendMeter,
    Think,
    parse_live_name,
    provider_model,
    refusal,
    spend_cap_reply,
)
from fivefold.agents.replay import CassettePlayer, ReplayModel
from fivefold.agents.worker import SPEND_CAP_TEXT
from fivefold.domain.prices import (
    ModelPrice,
    PriceFile,
    PriceFileError,
    price_file_from_text,
)
from fivefold.harness.clock import RealWaits
from fivefold.harness.log import RunRecord
from fivefold.harness.models import Live, LiveRefused, RunModels, check_live
from fivefold.recorder.cassette import ModelInfo, RunHeader
from fivefold.recorder.recording import CassetteRecorder, RecordingModel

FLAG = {"FIVEFOLD_LIVE": "1"}
HEADER = RunHeader(
    run_id="unit",
    scenario_id="dev-001-single-po",
    scenario_digest="00" * 32,
    config="G2",
    contract="C2",
    seed=7,
    topology="inproc",
    params={},
)


def prices(input_usd: str = "1", output_usd: str = "2", model: str = "m") -> PriceFile:
    """A price file for the fake provider's model `model`, in USD per million tokens."""
    return price_file_from_text(
        "price_date: 2026-11-15\n"
        "prices:\n"
        "  fake:\n"
        f'    "{model}":\n'
        f"      input_usd_per_million_tokens: {input_usd}\n"
        f"      output_usd_per_million_tokens: {output_usd}\n"
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


class FakeProvider(BaseChatModel):
    """A provider's chat model, as a fake: it answers each call with the next reply it was
    given, or raises it if it is an exception, binds tools as provider models do, and notes
    each call in `events`."""

    _replies: list[AIMessage | Exception] = PrivateAttr()
    _events: list[str] = PrivateAttr()
    _tools_seen: list[list[str]] = PrivateAttr()

    def __init__(self, replies: Sequence[AIMessage | Exception], events: list[str]) -> None:
        super().__init__()
        self._replies = list(replies)
        self._events = events
        self._tools_seen = []

    @property
    def tools_seen(self) -> list[list[str]]:
        """The names of the tools bound to each call."""
        return self._tools_seen

    @property
    def _llm_type(self) -> str:
        return "fake-provider"

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
        self._events.append("provider")
        self._tools_seen.append([tool["function"]["name"] for tool in kwargs.get("tools", [])])
        await asyncio.sleep(0.001)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)


def live_model(
    provider: BaseChatModel, meter: SpendMeter, events: list[str], *, model: str = "m"
) -> LiveModel:
    """The live adapter over `provider`, with a think and a hold that note when they run."""

    async def think() -> None:
        events.append("think")

    async def hold(awaitable: Awaitable[AIMessage]) -> AIMessage:
        events.append("hold")
        try:
            return await awaitable
        finally:
            events.append("held")

    return LiveModel(
        provider,
        name=f"fake:{model}",
        price=meter.prices.price("fake", model),
        meter=meter,
        think=think,
        hold=hold,
    )


# The spend cap -----------------------------------------------------------------------------


@pytest.mark.parametrize("cap", ["0", "-1", "NaN", "Infinity"])
def test_a_spend_cap_must_be_an_amount_above_zero(cap: str) -> None:
    with pytest.raises(LiveError, match="above 0 USD"):
        SpendMeter(Decimal(cap), prices())


def test_the_cap_is_reached_once_the_spend_so_far_is_at_it() -> None:
    meter = SpendMeter(Decimal("0.000010"), prices("1", "1"))
    price = meter.prices.price("fake", "m")
    meter.charge("fake:m", price, usage(4, 4))
    assert (meter.spent_usd, meter.reached()) == (Decimal("0.000008"), False)
    meter.charge("fake:m", price, usage(2, 0))
    assert (meter.spent_usd, meter.reached(), meter.calls) == (Decimal("0.00001"), True, 2)
    assert meter.summary() == (
        "0.000010 USD spent of the 0.000010 USD cap on 2 model calls, at prices dated 2026-11-15"
    )


def test_a_priced_reply_without_usage_makes_the_spend_unknown_and_reaches_the_cap() -> None:
    meter = SpendMeter(Decimal(100), prices("1", "1"))
    meter.charge("fake:m", meter.prices.price("fake", "m"), None)
    assert meter.reached()
    assert meter.spent_usd == 0
    assert "fake:m reported no token usage" in meter.summary()


def test_a_free_reply_without_usage_costs_nothing() -> None:
    meter = SpendMeter(Decimal(1), prices("0", "0"))
    meter.charge("fake:m", meter.prices.price("fake", "m"), None)
    meter.charge("fake:m", meter.prices.price("fake", "m"), usage(10**9, 10**9))
    assert (meter.spent_usd, meter.reached(), meter.unknown) == (Decimal(0), False, None)


# The flag and the refusals -------------------------------------------------------------------


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Register the provider `fake`, whose models answer "Done." and note their calls."""
    events: list[str] = []

    def build(model: str, params: Any) -> BaseChatModel:
        events.append(f"built {model} {dict(params)}")
        return FakeProvider([AIMessage("Done.")] * 10, events)

    monkeypatch.setitem(PROVIDERS, "fake", Provider(build, frozenset({"temperature"})))
    return events


@pytest.mark.parametrize("environ", [{}, {"FIVEFOLD_LIVE": "0"}, {"FIVEFOLD_LIVE": "true"}])
def test_a_live_run_is_refused_unless_the_flag_is_1(
    fake_provider: list[str], environ: dict[str, str]
) -> None:
    spec = Live("fake", "m", spend=SpendMeter(Decimal(1), prices()))
    with pytest.raises(LiveRefused, match="FIVEFOLD_LIVE=1"):
        RunModels(spec, environ=environ)
    assert fake_provider == [], "no model is built for a refused run"


def test_a_live_run_is_refused_without_a_spend_cap_and_a_price_file(
    fake_provider: list[str],
) -> None:
    with pytest.raises(LiveRefused, match="needs a spend cap in USD and a price file"):
        RunModels(Live("fake", "m"), environ=FLAG)
    with pytest.raises(PriceFileError, match="no price for fake:other"):
        RunModels(Live("fake", "other", spend=SpendMeter(Decimal(1), prices())), environ=FLAG)
    assert fake_provider == []


def test_an_unknown_provider_or_parameter_is_refused(fake_provider: list[str]) -> None:
    meter = SpendMeter(Decimal(1), prices())
    with pytest.raises(LiveError, match="accepts the parameters temperature, not base_url"):
        RunModels(Live("fake", "m", {"base_url": "http://x"}, spend=meter), environ=FLAG)
    with pytest.raises(LiveError, match="no live provider 'other'"):
        provider_model("other", "m", {})


def test_a_live_run_names_its_provider_and_model_and_always_records(
    fake_provider: list[str],
) -> None:
    meter = SpendMeter(Decimal(1), prices())
    models = RunModels(Live("fake", "m", {"temperature": 0}, spend=meter), environ=FLAG)
    assert (models.adapter, models.name, models.cassette_digest) == ("live", "fake:m", None)
    assert fake_provider == ["built m {'temperature': 0}"]

    async def think() -> None:
        pass

    def scripted(its_think: Think) -> BaseChatModel:
        raise AssertionError("a live run builds no scripted model")

    with pytest.raises(ValueError, match="needs the scheduler's hold"):
        models.chat_model("agent-1", scripted=scripted, think=think)

    async def hold(awaitable: Awaitable[AIMessage]) -> AIMessage:
        return await awaitable

    model = models.chat_model("agent-1", scripted=scripted, think=think, hold=hold)
    assert isinstance(model, RecordingModel)
    asyncio.run(model.ainvoke([HumanMessage("Order W-100.")]))
    digest = models.stopped(
        RunRecord(
            run_id="unit",
            scenario_id="dev-001-single-po",
            scenario_digest="00" * 32,
            config="G2",
            contract="C2",
            seed=7,
            model_adapter="live",
            model="fake:m",
            params={},
        )
    )
    assert models.cassette is not None and digest == models.cassette.digest()
    assert models.cassette.model == ModelInfo(
        adapter="live", name="fake:m", params={"temperature": 0}
    )
    assert len(models.cassette.calls) == 1


def test_a_live_model_is_named_provider_colon_model() -> None:
    assert parse_live_name("ollama:llama3.1:8b") == ("ollama", "llama3.1:8b")
    assert Live("ollama", "llama3.1:8b").name == "ollama:llama3.1:8b"
    for name in ("ollama", "ollama:", ":llama3.1"):
        with pytest.raises(LiveError, match="PROVIDER:MODEL"):
            parse_live_name(name)


# The adapter ---------------------------------------------------------------------------------


class Stalled(BaseChatModel):
    """A provider that accepts a call and never answers it."""

    @property
    def _llm_type(self) -> str:
        return "stalled-provider"

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
        await asyncio.Event().wait()
        raise AssertionError("never answered")


def test_a_call_past_its_time_limit_marks_its_cost_unknown_and_ends_its_wait() -> None:
    """A provider that never answers cannot hold the run: the call fails after its limit,
    the real wait ends, and the cap refuses the next call."""
    real = RealWaits()
    meter = SpendMeter(Decimal(1), prices())

    async def think() -> None:
        return None

    model = LiveModel(
        Stalled(),
        name="fake:m",
        price=meter.prices.price("fake", "m"),
        meter=meter,
        think=think,
        hold=real.hold,
        limit_s=0.05,
    )

    async def call() -> None:
        with pytest.raises(LiveError, match=r"fake:m did not answer within 0\.05 s"):
            await asyncio.wait_for(model.ainvoke([HumanMessage("Order W-100.")]), 5)
        assert not real.active
        assert meter.calls == 0
        assert meter.spent_usd == 0 and meter.unknown is not None
        reply = await model.ainvoke([HumanMessage("Order W-100.")])
        assert refusal(reply) == SPEND_CAP
        assert meter.calls == 0

    asyncio.run(call())


@pytest.mark.parametrize("priced", [True, False])
def test_a_provider_error_closes_the_cap_if_priced(priced: bool) -> None:
    events: list[str] = []
    rate = "1" if priced else "0"
    meter = SpendMeter(Decimal(1), prices(rate, rate))
    provider = FakeProvider([RuntimeError("provider failed"), AIMessage("Done.")], events)
    model = live_model(provider, meter, events)
    request = [HumanMessage("Order W-100.")]

    async def call() -> None:
        with pytest.raises(RuntimeError, match="provider failed"):
            await model.ainvoke(request)
        assert meter.calls == 0
        assert meter.spent_usd == 0 and meter.reached() is priced
        reply = await model.ainvoke(request)
        assert refusal(reply) == (SPEND_CAP if priced else None)
        assert events.count("provider") == (1 if priced else 2)
        assert meter.calls == (0 if priced else 1)

    asyncio.run(call())


def test_cancelling_a_priced_call_closes_the_cap() -> None:
    events: list[str] = []
    meter = SpendMeter(Decimal(1), prices("1", "1"))
    model = live_model(Stalled(), meter, events)

    async def call() -> None:
        pending = asyncio.create_task(model.ainvoke("Order W-100."))
        async with asyncio.timeout(5):
            while "hold" not in events:
                await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert meter.unknown is not None and meter.calls == 0
        assert refusal(await model.ainvoke("Order W-100.")) == SPEND_CAP
        assert events.count("hold") == 1

    asyncio.run(call())


@pytest.mark.parametrize("limit_s", [0.0, -1.0, float("inf"), float("nan")])
def test_a_live_run_needs_a_call_limit_above_zero(limit_s: float) -> None:
    meter = SpendMeter(Decimal(1), prices())
    with pytest.raises(LiveRefused, match="above 0 seconds"):
        check_live(Live("fake", "m", spend=meter, limit_s=limit_s), {"FIVEFOLD_LIVE": "1"})


def test_a_value_the_provider_refuses_is_refused_in_one_line() -> None:
    with pytest.raises(LiveError, match="ollama refuses temperature") as refused:
        provider_model("ollama", "m", {"temperature": "hot"})
    assert "\n" not in str(refused.value)


def test_a_live_call_thinks_then_awaits_the_provider_inside_the_hold_and_is_charged() -> None:
    events: list[str] = []
    reply = AIMessage("Done.", usage_metadata=usage(300, 50))
    meter = SpendMeter(Decimal(1), prices("1", "2"))
    model = live_model(FakeProvider([reply], events), meter, events)
    answered = asyncio.run(model.ainvoke([HumanMessage("Order W-100.")]))
    assert events == ["think", "hold", "provider", "held"]
    assert answered.content == "Done." and refusal(answered) is None
    assert meter.spent_usd == Decimal("0.0004") and meter.calls == 1


def test_a_call_at_the_cap_is_refused_after_its_think_and_never_sent() -> None:
    events: list[str] = []
    meter = SpendMeter(Decimal("0.0001"), prices("1", "1"))
    meter.charge("fake:m", meter.prices.price("fake", "m"), usage(100, 0))
    model = live_model(FakeProvider([], events), meter, events)
    reply = asyncio.run(model.ainvoke([HumanMessage("Order W-100.")]))
    assert events == ["think"], "a refused call takes its think, and never reaches the provider"
    assert (reply.content, refusal(reply), reply.tool_calls) == (SPEND_CAP_TEXT, SPEND_CAP, [])
    assert meter.calls == 1


def test_a_refused_call_is_recorded_and_its_replay_serves_the_same_refusal() -> None:
    """A run the spend cap cut short still plays back: the cassette keeps the refusal."""
    events: list[str] = []
    meter = SpendMeter(Decimal("0.0001"), prices("1", "1"))
    meter.charge("fake:m", meter.prices.price("fake", "m"), usage(100, 0))
    recorder = CassetteRecorder(ModelInfo(adapter="live", name="fake:m"))
    recording = RecordingModel(
        live_model(FakeProvider([], events), meter, events), recorder, worker="agent-1"
    )
    asked = [HumanMessage("Order W-100.")]
    asyncio.run(recording.bind_tools([quote_tool()]).ainvoke(asked))
    (call,) = recorder.calls
    assert call.response["response_metadata"] == {"fivefold_refused": "spend_cap"}

    async def no_time() -> None:
        pass

    player = CassettePlayer(recorder.cassette(HEADER))
    replay = ReplayModel(player, worker="agent-1", think=no_time).bind_tools([quote_tool()])
    replayed = asyncio.run(replay.ainvoke(asked))
    assert (replayed.content, refusal(replayed)) == (SPEND_CAP_TEXT, SPEND_CAP)
    player.finish()


def test_a_failed_free_call_ends_the_hold_and_charges_nothing() -> None:
    events: list[str] = []
    down = FakeProvider([ConnectionError("Failed to connect to Ollama.")], events)
    meter = SpendMeter(Decimal(1), prices("0", "0"))
    model = live_model(down, meter, events)
    with pytest.raises(ConnectionError, match="Failed to connect"):
        asyncio.run(model.ainvoke([HumanMessage("Order W-100.")]))
    assert events == ["think", "hold", "provider", "held"]
    assert (meter.spent_usd, meter.calls, meter.reached()) == (Decimal(0), 0, False)


def test_binding_tools_binds_them_to_the_provider_model() -> None:
    events: list[str] = []
    provider = FakeProvider([AIMessage("Done.")], events)
    meter = SpendMeter(Decimal(1), prices())
    model = live_model(provider, meter, events).bind_tools([quote_tool()])
    asyncio.run(model.ainvoke([HumanMessage("Quote W-100.")]))
    assert provider.tools_seen == [["get_quote"]]


def test_spend_cap_reply_ends_a_worker_as_budget() -> None:
    assert spend_cap_reply().content == SPEND_CAP_TEXT
    assert refusal(AIMessage("Done.")) is None


# The Ollama chat model -----------------------------------------------------------------------


def test_the_ollama_chat_model_is_built_with_the_expected_arguments_and_never_invoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no test may invoke a model provider")

    for method in ("_generate", "_agenerate", "_stream", "_astream"):
        monkeypatch.setattr(ChatOllama, method, never)
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama.invalid:11434")
    params = {"temperature": 0.0, "seed": 7, "num_predict": 256, "num_ctx": 8192}
    chat = provider_model("ollama", "llama3.1:8b", params)
    assert isinstance(chat, ChatOllama)
    assert (chat.model, chat.temperature, chat.seed, chat.num_predict, chat.num_ctx) == (
        "llama3.1:8b",
        0.0,
        7,
        256,
        8192,
    )
    assert chat.base_url is None, "the ollama package reads OLLAMA_HOST; nothing here does"
    assert chat.client_kwargs == {}, "no key or header comes from this code"
    assert chat.validate_model_on_init is False, "building the model sends no request"
    bound = chat.bind_tools([quote_tool()])
    assert isinstance(bound, RunnableBinding)
    assert [tool["function"]["name"] for tool in bound.kwargs["tools"]] == ["get_quote"]
    assert set(params) <= OLLAMA_PARAMS
    for setting in ("base_url", "client_kwargs", "validate_model_on_init", "keep_alive"):
        with pytest.raises(LiveError, match="accepts the parameters"):
            provider_model("ollama", "llama3.1:8b", {setting: None})


def test_check_live_returns_the_meter_and_the_model_s_price() -> None:
    meter = SpendMeter(Decimal(1), prices())
    assert check_live(Live("fake", "m", spend=meter), FLAG) == (
        meter,
        ModelPrice(
            input_usd_per_million_tokens=Decimal(1), output_usd_per_million_tokens=Decimal(2)
        ),
    )
