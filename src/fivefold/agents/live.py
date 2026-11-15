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
its output tokens at the output price. The provider's output limit bounds the output:
`num_predict` for Ollama, `max_tokens` for Anthropic and OpenAI, `max_output_tokens` for
Google. A reply that reports no usage, from a model that is not free, makes the spend
unknown, and the cap then counts as reached.

The harness records every live call into the run's cassette (0009), a refused one too, so
the replay adapter serves the same replies and the run plays back with no network. A call
the provider fails, for example because no server answers, raises: the worker ends with an
error, as for any exception in its graph (0008), and nothing is charged or recorded. Its
cassette then ends before that call, so a replay of the run stops there with `ReplayError`.

The providers (`PROVIDERS`), each through its LangChain package, which reads the key and
the base URL from its own environment variables:

- `ollama`, through langchain-ollama and the `ollama` package: no key, or
  `OLLAMA_API_KEY` for a server that needs one; the base URL from `OLLAMA_HOST`;
- `anthropic`, through langchain-anthropic: the key from `ANTHROPIC_API_KEY`; the base URL
  from `ANTHROPIC_API_URL` or `ANTHROPIC_BASE_URL`, or else Anthropic's API;
- `openai`, through langchain-openai: the key from `OPENAI_API_KEY`; the base URL from
  `OPENAI_API_BASE` or `OPENAI_BASE_URL`, or else OpenAI's API;
- `google_genai`, through langchain-google-genai and the `google-genai` package: the key
  from `GOOGLE_API_KEY` or `GEMINI_API_KEY`; the base URL from `GOOGLE_GEMINI_BASE_URL`, or
  else Google's Gemini API.

Nothing here passes, prints or records a key or a base URL. A live run of a provider that
needs a key is refused unless one of its key variables is set, which is all that the
check reads (`fivefold.harness.models.check_live`). A live run of `google_genai` is also
refused while `GOOGLE_GENAI_USE_VERTEXAI` or `GOOGLE_GENAI_USE_ENTERPRISE` is set: either
one sends its requests to Vertex AI instead, with Google's default credentials
(`Provider.forbidden`).

Each run executes on an event loop of its own (`asyncio.run`), but langchain-anthropic
1.7.10 and langchain-openai 1.8.0 keep one async HTTP client for the whole process: each
caches it with `functools.lru_cache` (`_client_utils.py`). A connection that client keeps
open after a reply belongs to the loop that opened it, and a later run's call on it fails.
Observed with each package against a local server, one `asyncio.run` per call: the
second call failed, with Anthropic's `APIConnectionError` after its request had reached
the server, so the package's retry would send it again, or with OpenAI's `RuntimeError:
Event loop is closed`. So both models send `Connection: close` with every request
(`CLOSE_AFTER_REPLY`), and each connection ends with its reply, on the loop that opened
it. Checked in h11 0.16.0 (`_connection.py`, `_keep_alive`): a request with that header
turns keep-alive off; and in httpcore2 2.16.0 (`_async/http11.py`, `_response_closed`):
the client then closes the connection once the reply ends, whatever the server does.

langchain-google-genai 4.5.0 builds a google-genai client for each model, so each run's
client is new. But google-genai 3.3.0 closes a collected async client on whatever event
loop is running at the time (`AsyncHttpx2Client.__del__`, `_api_client.py`). Observed
against a local server: an earlier run's client, collected during a later run, failed
there on the connections it kept, and one model called from a second loop failed with
`RuntimeError: Event loop is closed`. So the Google model sends the same header,
through `additional_headers`, and keeps no connection either.

To add a provider:

1. add its LangChain chat model package as an exact dependency, in a commit of its own
   that changes no other locked version;
2. add an entry to `PROVIDERS`: a function that builds the chat model from the model's
   name and parameters, the names of the generation parameters it accepts, the
   variables its package reads its key from, and any variable that would send its
   requests elsewhere;
3. add the provider's prices, with their date, to the price file the runs use.

Checked in langchain-ollama 1.1.0 (`chat_models.py`): `ChatOllama(model=...)` takes
`temperature`, `seed`, `top_k`, `top_p`, `num_ctx`, `num_predict`, `repeat_penalty` and
`reasoning`; with `base_url` None the `ollama` client picks the host; it asks the server
for its models only when `validate_model_on_init` is set (default False), so building one
sends nothing; `bind_tools` binds the tools' schemas (`convert_to_openai_tool`) as
`tools`; a reply's `usage_metadata` holds `input_tokens` (Ollama's `prompt_eval_count`)
and `output_tokens` (`eval_count`), and is None unless Ollama reported both. Checked in
ollama 0.6.3 (`_client.py`, `BaseClient`): it reads `OLLAMA_HOST` when given no host and
sends `OLLAMA_API_KEY` as a bearer token; its httpx timeout defaults to None.

Checked in langchain-anthropic 1.7.10 (`chat_models.py`): `ChatAnthropic(model=...)` takes
`max_tokens`, `temperature`, `top_k`, `top_p`, `thinking` and `reasoning_effort`, and puts
any argument it does not know into `model_kwargs`, which every request carries
(`build_extra`); a validator reads the key and the base URL from the environment through
langchain-core 1.8.0's `_apply_gateway_config` (`utils/_gateway.py`), which can route the
requests through LangSmith's gateway when `LANGSMITH_GATEWAY` is set; the clients are
built at the first call (`cached_property`), so building one sends nothing; `max_tokens`
defaults to the model's maximum output in the package's own profiles, or 4,096;
`bind_tools` converts each tool with `convert_to_anthropic_tool`, and drops with a warning
any tool whose `input_schema` has a top-level `anyOf` or `oneOf`; `_agenerate` sends one
`messages.create`, and the reply's `usage_metadata` holds `input_tokens` (the input, cache
reads and cache writes) and `output_tokens` (`_create_usage_metadata`).

Checked in langchain-openai 1.8.0 (`chat_models/base.py`): `ChatOpenAI(model_name=...)`
takes `max_tokens` (sent as `max_completion_tokens`), `temperature`, `top_p`, `seed`,
`presence_penalty`, `frequency_penalty` and `reasoning_effort`, and puts any argument it
does not know into `model_kwargs` (`build_extra`); `validate_environment` reads the key
and the base URL through `_resolve_gateway_config`, and builds the `openai` clients, which
send nothing; openai 3.51.0 refuses to build a client without a key
(`lib/_client_auth.py`); `bind_tools` converts each tool with `convert_to_openai_tool`;
`_agenerate` sends one request, to Chat Completions or, for the models and settings that
need it, to the Responses API (`_use_responses_api`), and the reply's `usage_metadata`
holds `input_tokens` and `output_tokens` from either (`_create_usage_metadata`,
`_create_usage_metadata_responses`).

Checked in langchain-google-genai 4.5.0 (`_common.py`, `chat_models.py`):
`ChatGoogleGenerativeAI(model=...)` takes `max_output_tokens`, `temperature`, `top_k`,
`top_p`, `seed`, `presence_penalty`, `frequency_penalty`, `thinking_budget` and
`reasoning_effort`, and puts any argument it does not know into `model_kwargs`
(`build_extra`); the key comes from `GOOGLE_API_KEY` or `GEMINI_API_KEY`
(`secret_from_env`, then `_apply_gateway_config`); `_determine_backend` picks Vertex AI
when `GOOGLE_GENAI_USE_VERTEXAI` is true, and google-genai 3.3.0's client does so again
when it or `GOOGLE_GENAI_USE_ENTERPRISE` is true (`BaseApiClient.__init__`,
`_api_client.py`); for the Gemini API, `validate_environment` builds a `Client` with the
key, which loads no default credentials and sends nothing until the first call;
`bind_tools` binds the tools in OpenAI's format, and each request converts them with
`convert_to_genai_function_declarations` (`_format_tools`), which drops
`additionalProperties` from a schema; `_agenerate` sends one `generate_content`, which
google-genai tries up to `max_retries` times (default 6, `HttpRetryOptions`, through
tenacity); the reply's `usage_metadata` holds `input_tokens` (the prompt's and tool-use
prompt's tokens) and `output_tokens` (the candidates' and thoughts' tokens), and is None
when the reply reports none (`_response_to_result`).

Checked in langchain-core 1.8.0: `UsageMetadata` (`messages/ai.py`); `ainvoke` streams
only when asked to, or when a streaming callback is attached (`_should_stream`,
`language_models/chat_models.py`). Checked in langgraph 1.2.22 (`pregel/main.py`): a graph
attaches its message streaming callback only for the stream mode `messages`, and the
agent's graph runs with `ainvoke`, whose mode is `values`. So each live call is one
request and one reply.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from pydantic import JsonValue, PrivateAttr, ValidationError

from fivefold.agents.worker import SPEND_CAP_TEXT
from fivefold.domain.prices import ModelPrice, PriceFile
from fivefold.harness.clock import within
from fivefold.recorder.cassette import Tool

Think = Callable[[], Awaitable[None]]
"""Takes `think_ms` of virtual time."""

Hold = Callable[[Awaitable[AIMessage]], Awaitable[AIMessage]]
"""Awaits a real wait with virtual time held still (`Scheduler.hold`)."""

REFUSED: Final = "fivefold_refused"
"""The reply metadata that marks a reply the harness gave instead of the model, and why."""

SPEND_CAP: Final = "spend_cap"
"""Why the harness refused a call: the spend cap was reached."""

CALL_LIMIT_S: Final = 600.0
"""How long, in wall-clock seconds, a live call may wait for its provider by default. A
call still unanswered then fails, and its worker ends with an error, so a provider that
accepts a request and never answers cannot hold the run forever."""


class LiveError(ValueError):
    """A live model that cannot be built, such as one with an unknown provider or
    parameter, or a live call that failed because its provider did not answer in time."""


# Providers ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    """A live provider: how to build its chat model, the parameters it accepts, where its
    package reads its API key, and the variables it refuses.

    Only generation parameters are accepted. A key, a base URL, a header or a client
    setting never is: each comes from the provider's own environment variables, so no
    parameter can put one in the repository, in a cassette, which records every
    parameter, or in `harness.run`.
    """

    build: Callable[[str, Mapping[str, JsonValue]], BaseChatModel]
    params: frozenset[str]
    keys: tuple[str, ...] = ()
    """The environment variables the provider's package reads its API key from, any one of
    which is enough; none for a provider that needs no key."""
    forbidden: tuple[str, ...] = ()
    """Environment variables that would send the provider's requests to another backend,
    with other credentials; a live run is refused while one is set."""


def _ollama(model: str, params: Mapping[str, JsonValue]) -> BaseChatModel:
    arguments: dict[str, Any] = dict(params)
    return ChatOllama(model=model, **arguments)


CLOSE_AFTER_REPLY: Final = {"Connection": "close"}
"""The header the API providers' models send with every request, so that no connection
outlives the run's event loop (see the module)."""


def _anthropic(model: str, params: Mapping[str, JsonValue]) -> BaseChatModel:
    arguments: dict[str, Any] = dict(params)
    return ChatAnthropic(model=model, default_headers=dict(CLOSE_AFTER_REPLY), **arguments)


def _openai(model: str, params: Mapping[str, JsonValue]) -> BaseChatModel:
    arguments: dict[str, Any] = dict(params)
    return ChatOpenAI(model_name=model, default_headers=dict(CLOSE_AFTER_REPLY), **arguments)


def _google_genai(model: str, params: Mapping[str, JsonValue]) -> BaseChatModel:
    arguments: dict[str, Any] = dict(params)
    return ChatGoogleGenerativeAI(
        model=model, additional_headers=dict(CLOSE_AFTER_REPLY), **arguments
    )


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

ANTHROPIC_PARAMS: Final = frozenset(
    {"max_tokens", "temperature", "top_k", "top_p", "thinking", "reasoning_effort"}
)
"""The `ChatAnthropic` parameters a live run may set; each changes what the model
generates."""

OPENAI_PARAMS: Final = frozenset(
    {
        "max_tokens",
        "temperature",
        "top_p",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "reasoning_effort",
    }
)
"""The `ChatOpenAI` parameters a live run may set; each changes what the model generates."""

GOOGLE_GENAI_PARAMS: Final = frozenset(
    {
        "max_output_tokens",
        "temperature",
        "top_k",
        "top_p",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "thinking_budget",
        "reasoning_effort",
    }
)
"""The `ChatGoogleGenerativeAI` parameters a live run may set; each changes what the model
generates."""

PROVIDERS: dict[str, Provider] = {
    "ollama": Provider(_ollama, OLLAMA_PARAMS),
    "anthropic": Provider(_anthropic, ANTHROPIC_PARAMS, ("ANTHROPIC_API_KEY",)),
    "openai": Provider(_openai, OPENAI_PARAMS, ("OPENAI_API_KEY",)),
    "google_genai": Provider(
        _google_genai,
        GOOGLE_GENAI_PARAMS,
        ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE"),
    ),
}
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
    """`provider`'s chat model for `model`, with `params`. Building it sends nothing.

    A value the provider's model refuses raises `LiveError`, with the first problem in
    one line.
    """
    entry = check_provider(provider, params)
    try:
        return entry.build(model, params)
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "a parameter"
        raise LiveError(f"{provider} refuses {where}: {first['msg']}") from None


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
    _limit_s: float = PrivateAttr()

    def __init__(
        self,
        chat: BaseChatModel,
        *,
        name: str,
        price: ModelPrice,
        meter: SpendMeter,
        think: Think,
        hold: Hold,
        limit_s: float = CALL_LIMIT_S,
    ) -> None:
        """Answer with `chat`, the provider's model named `name`, charged at `price`. A
        call that waits more than `limit_s` wall-clock seconds for the provider fails."""
        super().__init__()
        self._chat = chat
        self._bound = chat
        self._name = name
        self._price = price
        self._meter = meter
        self._think = think
        self._hold = hold
        self._limit_s = limit_s

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
        call = self._bound.ainvoke(messages, stop=stop, **kwargs)
        try:
            reply = await self._hold(within(call, self._limit_s))
        except TimeoutError:
            raise LiveError(
                f"the live model {self._name} did not answer within {self._limit_s:g} s"
            ) from None
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
            limit_s=self._limit_s,
        )
        bound._bound = self._chat.bind_tools(tools, **kwargs)
        return bound
