"""The live model adapter (0009, Models): a provider's chat model, run on the scheduler.

`LiveModel` wraps the chat model of one provider (`PROVIDERS`). Each call:

1. takes `think_ms` of virtual time first, as every adapter's call does (0009);
2. checks the spend cap (`SpendMeter`). Once the spend so far has reached the cap, the call
   is not sent: the reply is the harness's refusal (`spend_cap_reply`), and the worker
   ends as `budget` (`fivefold.agents.worker`);
3. sends the request and awaits the answer in wall time, inside the scheduler's real wait
   (`Scheduler.hold`): virtual time stands still and no event fires until the provider
   answers, so the call takes exactly `think_ms` of virtual time;
4. charges the reply's token usage at the model's price in the price file.

The cost of the call in flight is bounded. Calls run one at a time: while one is in
flight, no event fires, so no other worker's think can end and send another. Each call
is checked just before it is sent, once every earlier call has been charged. So the
spend passes the cap by at most one call's cost: its input tokens at the input price plus
its output tokens at the output price. The provider's output limit (`num_predict` for
Ollama) bounds the output. A reply that reports no usage, from a model that is not free,
makes the spend unknown, and the cap then counts as reached.

The harness records every live call into the run's cassette (0009), a refused one too, so
the replay adapter serves the same replies and the run plays back with no network. A call
the provider fails, for example because no server answers, raises: the worker ends with an
error, as for any exception in its graph (0008), and nothing is charged or recorded. Its
cassette then ends before that call, so a replay of the run stops there with `ReplayError`.

Ollama is the one provider (spec, Stack). Its chat model reads its base URL from
`OLLAMA_HOST` and, for a server that needs one, a key from `OLLAMA_API_KEY`, through the
`ollama` package. Nothing here reads, passes or records either. To add an API provider:

1. add its LangChain chat model package as a dependency, in a pull request of its own;
2. add an entry to `PROVIDERS`: a function that builds the chat model from the model's
   name and parameters, and the names of the parameters it accepts. The package reads its
   key and base URL from its own environment variables;
3. add the provider's prices, with their date, to the price file the runs use.

Checked in langchain-ollama 1.1.0 (`chat_models.py`): `ChatOllama(model=...)` takes
`temperature`, `seed`, `top_k`, `top_p`, `num_ctx`, `num_predict`, `repeat_penalty` and
`reasoning`; with `base_url` None the `ollama` client picks the host; it asks the server
for its models only when `validate_model_on_init` is set (default False), so building one
sends nothing; `bind_tools` binds the tools' schemas (`convert_to_openai_tool`) as
`tools`; a reply's `usage_metadata` holds `input_tokens` (Ollama's `prompt_eval_count`)
and `output_tokens` (`eval_count`), and is None unless Ollama reported both. Checked in
ollama 0.6.3 (`_client.py`, `BaseClient`): it reads `OLLAMA_HOST` when given no host and
sends `OLLAMA_API_KEY` as a bearer token; its httpx timeout defaults to None. Checked in
langchain-core 1.8.0: `UsageMetadata` (`messages/ai.py`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_ollama import ChatOllama
from pydantic import JsonValue, PrivateAttr

from fivefold.agents.worker import SPEND_CAP_TEXT
from fivefold.domain.prices import ModelPrice, PriceFile
from fivefold.recorder.cassette import Tool

Think = Callable[[], Awaitable[None]]
"""Takes `think_ms` of virtual time."""

Hold = Callable[[Awaitable[AIMessage]], Awaitable[AIMessage]]
"""Awaits a real wait with virtual time held still (`Scheduler.hold`)."""

REFUSED: Final = "fivefold_refused"
"""The reply metadata that marks a reply the harness gave instead of the model, and why."""

SPEND_CAP: Final = "spend_cap"
"""Why the harness refused a call: the spend cap was reached."""


class LiveError(ValueError):
    """A live model that cannot be built: an unknown provider or parameter."""


# Providers ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    """A live provider: how to build its chat model, and the parameters it accepts.

    Only generation parameters are accepted. A base URL, a key or client settings never
    are: they come from the provider's own environment variables, so no parameter can put
    one in the repository or in a cassette, which records every parameter.
    """

    build: Callable[[str, Mapping[str, JsonValue]], BaseChatModel]
    params: frozenset[str]


def _ollama(model: str, params: Mapping[str, JsonValue]) -> BaseChatModel:
    arguments: dict[str, Any] = dict(params)
    return ChatOllama(model=model, **arguments)


OLLAMA_PARAMS: Final = frozenset(
    {
        "temperature",
        "seed",
        "top_k",
        "top_p",
        "num_ctx",
        "num_predict",
        "repeat_penalty",
        "reasoning",
    }
)
"""The `ChatOllama` parameters a live run may set; each changes what the model generates."""

PROVIDERS: dict[str, Provider] = {"ollama": Provider(_ollama, OLLAMA_PARAMS)}
"""The live providers, by name."""


def live_name(provider: str, model: str) -> str:
    """The name of a live model, as `harness.run` and its cassette record it: the provider
    and the model, such as `ollama:llama3.1:8b`."""
    return f"{provider}:{model}"


def parse_live_name(name: str) -> tuple[str, str]:
    """The provider and the model of a live model's name: the text before the first colon,
    and the rest."""
    provider, _, model = name.partition(":")
    if not provider or not model:
        raise LiveError(f"a live model is named PROVIDER:MODEL, such as ollama:MODEL, not {name!r}")
    return provider, model


def check_provider(provider: str, params: Mapping[str, JsonValue]) -> Provider:
    """`provider`'s entry, if it exists and accepts every parameter in `params`."""
    entry = PROVIDERS.get(provider)
    if entry is None:
        raise LiveError(
            f"no live provider {provider!r}; this build has {', '.join(sorted(PROVIDERS))}"
        )
    unknown = sorted(set(params) - entry.params)
    if unknown:
        raise LiveError(
            f"{provider} accepts the parameters {', '.join(sorted(entry.params))},"
            f" not {', '.join(unknown)}"
        )
    return entry


def provider_model(provider: str, model: str, params: Mapping[str, JsonValue]) -> BaseChatModel:
    """`provider`'s chat model for `model`, with `params`. Building it sends nothing."""
    return check_provider(provider, params).build(model, params)


# The spend cap -----------------------------------------------------------------------------


class SpendMeter:
    """The spend cap of one command's live runs, and what their model calls spent (0004).

    One meter serves every run of a command, so the cap holds across them. Each call is
    charged at its model's price in the price file, from its reply's token usage.
    """

    def __init__(self, cap_usd: Decimal, prices: PriceFile) -> None:
        if not cap_usd.is_finite() or cap_usd <= 0:
            raise LiveError(
                f"the spend cap must be an amount above 0 USD, not {cap_usd}:"
                " a cap of 0 would refuse every call"
            )
        self.cap_usd = cap_usd
        self.prices = prices
        self.spent_usd = Decimal(0)
        """What the calls charged so far cost, in USD, exactly."""
        self.calls = 0
        """The calls charged so far."""
        self.unknown: str | None = None
        """Why the spend is no longer known, if it is not."""

    def reached(self) -> bool:
        """Whether the spend so far reached the cap, or is no longer known."""
        return self.unknown is not None or self.spent_usd >= self.cap_usd

    def charge(self, model: str, price: ModelPrice, usage: UsageMetadata | None) -> None:
        """Charge one call of `model` at `price`, from its reply's usage."""
        self.calls += 1
        if usage is not None:
            self.spent_usd += price.cost_usd(usage["input_tokens"], usage["output_tokens"])
        elif not price.free:
            self.unknown = f"a reply of {model} reported no token usage, so its cost is unknown"

    def summary(self) -> str:
        """The spend so far against the cap, and the price file's date, in one line."""
        line = (
            f"{self.spent_usd} USD spent of the {self.cap_usd} USD cap on {self.calls} model"
            f" calls, at prices dated {self.prices.price_date}"
        )
        return line if self.unknown is None else f"{line}; {self.unknown}"


def spend_cap_reply() -> AIMessage:
    """The reply to a call the spend cap refused. It ends the worker as `budget`."""
    return AIMessage(content=SPEND_CAP_TEXT, response_metadata={REFUSED: SPEND_CAP})


def refusal(reply: AIMessage) -> str | None:
    """Why the harness refused the call that `reply` answers, or None if the model answered."""
    reason = reply.response_metadata.get(REFUSED)
    return reason if isinstance(reason, str) else None


# The adapter --------------------------------------------------------------------------------


class LiveModel(BaseChatModel):
    """A provider's chat model behind the spend cap, on the scheduler (0009)."""

    _chat: BaseChatModel = PrivateAttr()
    _bound: Runnable[LanguageModelInput, AIMessage] = PrivateAttr()
    _name: str = PrivateAttr()
    _price: ModelPrice = PrivateAttr()
    _meter: SpendMeter = PrivateAttr()
    _think: Think = PrivateAttr()
    _hold: Hold = PrivateAttr()

    def __init__(
        self,
        chat: BaseChatModel,
        *,
        name: str,
        price: ModelPrice,
        meter: SpendMeter,
        think: Think,
        hold: Hold,
    ) -> None:
        """Answer with `chat`, the provider's model named `name`, charged at `price`."""
        super().__init__()
        self._chat = chat
        self._bound = chat
        self._name = name
        self._price = price
        self._meter = meter
        self._think = think
        self._hold = hold

    @property
    def _llm_type(self) -> str:
        return "fivefold-live"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("the live model runs on the scheduler's loop only")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        await self._think()
        if self._meter.reached():
            return ChatResult(generations=[ChatGeneration(message=spend_cap_reply())])
        reply = await self._hold(self._bound.ainvoke(messages, stop=stop, **kwargs))
        self._meter.charge(self._name, self._price, reply.usage_metadata)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Tool], **kwargs: Any) -> LiveModel:
        """Bind `tools` to the provider's model, which sends their schemas with each call."""
        bound = LiveModel(
            self._chat,
            name=self._name,
            price=self._price,
            meter=self._meter,
            think=self._think,
            hold=self._hold,
        )
        bound._bound = self._chat.bind_tools(tools, **kwargs)
        return bound
