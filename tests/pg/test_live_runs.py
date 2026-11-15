"""Whole runs through the live model adapter, with a fake provider (0009; CLAUDE.md).

No test reaches a model provider. The fake provider answers as a real model does: it is
asked through the live adapter with the worker's tools bound, it plays the scripted policy
of 0008 over the conversation, its tool calls have random ids, as Ollama's do, its replies
report token usage and carry wall-clock metadata, and each call awaits the network in wall
time, through I/O the event loop cannot see. The run must fire every event at the virtual
time the scripted run does, record every call into its cassette, and play back byte for
byte with no provider at all. The token budget and the spend cap end a worker as `budget`,
and their replays end it at the same call. The record command refuses a live run without
the flag, the spend cap and the price file, before it touches any database. A live study
saves each episode's result and cassette, and the cassettes play back; the spend cap stops
a study partway, and the study keeps what it ran.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from psycopg.conninfo import make_conninfo
from pydantic import JsonValue, PrivateAttr

from fivefold.agents.live import PROVIDERS, Provider, SpendMeter
from fivefold.agents.policy import Mode, Result, scripted_policy
from fivefold.agents.worker import SPEND_CAP_TEXT, TOKEN_BUDGET_TEXT, WorkerEnd
from fivefold.domain.prices import PriceFile, price_file_from_text
from fivefold.domain.scenario import load_scenario_file
from fivefold.evaluator.report import load_result, load_results
from fivefold.harness.cells import load_expected
from fivefold.harness.demo import DEV_001, demo_1_spec
from fivefold.harness.migrations import drop_database
from fivefold.harness.models import Live
from fivefold.harness.run import Config, InprocRun, RunSpec
from fivefold.harness.study import main as study_main
from fivefold.recorder.__main__ import main
from fivefold.recorder.cassette import Cassette, response_message
from fivefold.recorder.playback import play, record, records

pytestmark = pytest.mark.pg

SCENARIO = load_scenario_file(DEV_001)
THINK_MS = SCENARIO.params.think_ms
CALLS: LiteralString = (
    "SELECT call_no, worker, asked_vt, answered_vt, model, input_tokens, output_tokens,"
    " refused, wall_ms FROM harness.model_call ORDER BY call_no"
)


def price_file(input_usd: str = "1", output_usd: str = "2") -> PriceFile:
    return price_file_from_text(
        "price_date: 2026-11-15\nprices:\n  fake:\n    m:\n"
        f"      input_usd_per_million_tokens: {input_usd}\n"
        f"      output_usd_per_million_tokens: {output_usd}\n"
    )


async def network(delay_s: float) -> None:
    """Await the network: a socket another thread answers after `delay_s` seconds of wall
    time. The event loop sees neither a timer nor anything ready meanwhile."""
    loop = asyncio.get_running_loop()
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    sender = threading.Timer(delay_s, theirs.send, args=(b"x",))
    sender.start()
    try:
        await loop.sock_recv(ours, 1)
    finally:
        sender.join()
        ours.close()
        theirs.close()


def _result(message: ToolMessage) -> Result:
    """A tool result as the scripted policy reads it."""
    text = message.content if isinstance(message.content, str) else json.dumps(message.content)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"error": text}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


class PolicyProvider(BaseChatModel):
    """A fake provider's chat model that answers as a real model does (see the module)."""

    _usage: Callable[[Sequence[BaseMessage]], UsageMetadata] = PrivateAttr()
    _down: bool = PrivateAttr()

    def __init__(
        self, usage: Callable[[Sequence[BaseMessage]], UsageMetadata], *, down: bool = False
    ) -> None:
        """With `down`, every call fails after its wait, as when no server answers."""
        super().__init__()
        self._usage = usage
        self._down = down

    @property
    def _llm_type(self) -> str:
        return "fake-policy-provider"

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
        started = datetime.now(UTC)
        tools = {tool["function"]["name"] for tool in kwargs.get("tools", [])}
        mode = Mode.GATEWAY if "get_action_status" in tools else Mode.BASELINE
        policy = scripted_policy(SCENARIO.intents, mode=mode)
        try:
            call = next(policy)
            for message in messages:
                if isinstance(message, ToolMessage):
                    call = policy.send(_result(message))
        except StopIteration:
            call = None
        await network(0.002)
        await asyncio.sleep(0.001)
        if self._down:
            raise ConnectionError("Failed to connect to Ollama.")
        usage = self._usage(messages)
        metadata = {
            "model": "m",
            "created_at": started.isoformat(),
            "done": True,
            "done_reason": "stop",
            "total_duration": int((datetime.now(UTC) - started).total_seconds() * 1e9),
            "prompt_eval_count": usage["input_tokens"],
            "eval_count": usage["output_tokens"],
            "model_provider": "fake",
        }
        if call is None:
            reply = AIMessage("Done.", usage_metadata=usage, response_metadata=metadata)
        else:
            assert call.name in tools, f"{call.name} is not bound to the model"
            tool_call = {"name": call.name, "args": dict(call.args), "id": str(uuid.uuid4())}
            reply = AIMessage(
                "", tool_calls=[tool_call], usage_metadata=usage, response_metadata=metadata
            )
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)


def usage_by_length(messages: Sequence[BaseMessage]) -> UsageMetadata:
    """Tokens as a provider might count them: the request's length, and a short reply."""
    input_tokens = sum(len(str(message.content)) for message in messages) // 4 + 10
    return UsageMetadata(
        input_tokens=input_tokens, output_tokens=25, total_tokens=input_tokens + 25
    )


def fixed_usage(input_tokens: int, output_tokens: int) -> Callable[..., UsageMetadata]:
    def usage(messages: Sequence[BaseMessage]) -> UsageMetadata:
        return UsageMetadata(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )

    return usage


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Allow live runs, and register the fake provider `fake` with the usage given."""
    monkeypatch.setenv("FIVEFOLD_LIVE", "1")

    def register(usage: Callable[[Sequence[BaseMessage]], UsageMetadata] = usage_by_length) -> None:
        def build(model: str, params: Any) -> BaseChatModel:
            assert model == "m"
            return PolicyProvider(usage)

        monkeypatch.setitem(PROVIDERS, "fake", Provider(build, frozenset({"temperature"})))

    register()
    return register


def live(spec: RunSpec, meter: SpendMeter, params: dict[str, JsonValue] | None = None) -> RunSpec:
    return replace(spec, model=Live("fake", "m", params or {}, spend=meter))


def no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the fake provider away and turn live runs off, as on a machine with no model."""
    monkeypatch.delitem(PROVIDERS, "fake")
    monkeypatch.delenv("FIVEFOLD_LIVE")


def rows(dsn: str, query: LiteralString, role: str | None = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn if role is None else make_conninfo(dsn, user=role)) as conn:
        return conn.execute(query).fetchall()


def without_model(evaluation: str) -> dict[str, Any]:
    """An evaluation row, as `records` reads it, which leaves out the model adapter,
    without the model's name and the cassette's digest too."""
    data = json.loads(evaluation)
    for key in ("model", "cassette_digest"):
        data["result"]["run"].pop(key)
    return dict(data)


def timing(model_calls: list[str]) -> list[tuple[Any, ...]]:
    """Each model call's number, worker and virtual times."""
    calls = [json.loads(row) for row in model_calls]
    return [(c["call_no"], c["worker"], c["asked_vt"], c["answered_vt"]) for c in calls]


# A whole live run --------------------------------------------------------------------------


@pytest.mark.parametrize("config", ["B0", "G1", "G2"])
def test_a_live_run_does_what_the_scripted_run_does_and_plays_back_byte_for_byte(
    run_database: Callable[[str], str],
    provider: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
) -> None:
    """Demo 1 (F2 and F11) with the fake provider: every event fires at the virtual time
    of the scripted run, so the runs' records match but for the model's name and its
    tokens; the cassette then replays with no provider and no flag."""
    tag = config.lower()
    scripted_dsn = run_database(f"live_{tag}_scripted")
    live_dsn, replay_dsn = run_database(f"live_{tag}"), run_database(f"live_{tag}_replay")
    asyncio.run(InprocRun(demo_1_spec(config, 7), scripted_dsn).execute())
    meter = SpendMeter(Decimal(1), price_file())
    run, cassette = record(live(demo_1_spec(config, 7), meter, {"temperature": 0}), live_dsn)

    assert run.result.ends == {"agent-1": (WorkerEnd.FINISHED, "Done.")}
    scripted, recorded = records(scripted_dsn), records(live_dsn)
    for name in scripted:
        if name not in ("evaluation", "model calls"):
            assert recorded[name] == scripted[name], name
    assert [without_model(row) for row in recorded["evaluation"]] == [
        without_model(row) for row in scripted["evaluation"]
    ]
    assert timing(recorded["model calls"]) == timing(scripted["model calls"])

    assert (cassette.model.adapter, cassette.model.name) == ("live", "fake:m")
    assert cassette.model.params == {"temperature": 0}
    assert len(cassette.calls) == 7
    replies = [response_message(call.response) for call in cassette.calls]
    ids = [str(tool_call["id"]) for reply in replies for tool_call in reply.tool_calls]
    assert len(ids) == 6 and all(uuid.UUID(i).version == 4 for i in ids), "random, as Ollama's"
    assert all("usage_metadata" in c.response for c in cassette.calls)

    logged = rows(live_dsn, CALLS)
    assert [row[:5] for row in logged] == [
        (n, "agent-1", row[2], row[2] + THINK_MS, "fake:m") for n, row in enumerate(logged, 1)
    ]
    assert all(row[5] is not None and row[6] == 25 and row[7] is None for row in logged)
    spent = sum(Decimal(row[5]) + 2 * Decimal(row[6]) for row in logged) / 1_000_000
    assert (meter.spent_usd, meter.calls) == (spent, 7)
    assert rows(live_dsn, "SELECT model_adapter, model, cassette_digest FROM harness.run") == [
        ("live", "fake:m", cassette.digest())
    ]

    no_provider(monkeypatch)
    playback = play(cassette, SCENARIO, original=live_dsn, replay=replay_dsn)
    assert playback.differences == ()
    assert playback.calls == 7
    assert rows(replay_dsn, CALLS)[0][1:8] == logged[0][1:8], "the replay logs the tokens"
    assert rows(replay_dsn, "SELECT model_adapter, model FROM harness.run") == [
        ("replay", "fake:m")
    ]


def test_the_token_budget_ends_a_live_worker_and_its_replay_at_the_same_call(
    run_database: Callable[[str], str],
    provider: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dev-001's budget is 60,000 tokens a worker. At 20,000 tokens a call, the third
    reply leaves 60,000 used, so the fourth call would start past the budget."""
    provider(fixed_usage(19_000, 1_000))
    live_dsn, replay_dsn = run_database("tokens_live"), run_database("tokens_replay")
    spec = RunSpec(SCENARIO, "G2", demo_1_spec("G2", 7).contract, 7, plan=[])
    run, cassette = record(live(spec, SpendMeter(Decimal(1), price_file("0", "0"))), live_dsn)
    assert SCENARIO.budget.max_tokens == 60_000
    assert run.result.ends == {"agent-1": (WorkerEnd.BUDGET, TOKEN_BUDGET_TEXT)}
    assert len(cassette.calls) == 3
    assert [row[5:7] for row in rows(live_dsn, CALLS)] == [(19_000, 1_000)] * 3

    no_provider(monkeypatch)
    playback = play(cassette, SCENARIO, original=live_dsn, replay=replay_dsn)
    assert playback.matches and playback.calls == 3


def test_the_spend_cap_refuses_the_next_call_ends_the_worker_and_plays_back(
    run_database: Callable[[str], str],
    provider: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 10 USD per million input tokens and 10,000 a call, each call costs 0.10 USD,
    well inside the token budget. With a cap of 0.25 USD, the third call starts at 0.20
    and ends at 0.30, so the fourth is refused: the spend passes the cap by less than one
    call's cost."""
    provider(fixed_usage(10_000, 0))
    live_dsn, replay_dsn = run_database("cap_live"), run_database("cap_replay")
    meter = SpendMeter(Decimal("0.25"), price_file("10", "0"))
    spec = RunSpec(SCENARIO, "G2", demo_1_spec("G2", 7).contract, 7, plan=[])
    run, cassette = record(live(spec, meter), live_dsn)
    assert run.result.ends == {"agent-1": (WorkerEnd.BUDGET, SPEND_CAP_TEXT)}
    assert (meter.spent_usd, meter.calls) == (Decimal("0.3"), 3)
    assert meter.spent_usd - meter.cap_usd < Decimal("0.1")
    assert len(cassette.calls) == 4
    assert cassette.calls[-1].response == {
        "content": SPEND_CAP_TEXT,
        "response_metadata": {"fivefold_refused": "spend_cap"},
    }
    logged = rows(live_dsn, CALLS)
    assert [row[5:8] for row in logged] == [(10_000, 0, None)] * 3 + [(None, None, "spend_cap")]
    assert logged[3][3] - logged[3][2] == THINK_MS, "a refused call takes its think too"

    no_provider(monkeypatch)
    playback = play(cassette, SCENARIO, original=live_dsn, replay=replay_dsn)
    assert playback.matches and playback.calls == 4


def test_the_spend_cap_holds_across_a_command_s_runs(
    run_database: Callable[[str], str], provider: Callable[..., None]
) -> None:
    """One meter serves every run of a command: once the cap is reached, the next run's
    first call is refused."""
    provider(fixed_usage(10_000, 0))
    meter = SpendMeter(Decimal("0.15"), price_file("10", "0"))
    spec = RunSpec(SCENARIO, "G2", demo_1_spec("G2", 7).contract, 7, plan=[])
    first, _ = record(live(spec, meter), run_database("cap_first"))
    second, cassette = record(live(spec, meter), run_database("cap_second"))
    assert first.result.ends["agent-1"] == (WorkerEnd.BUDGET, SPEND_CAP_TEXT)
    assert second.result.ends["agent-1"] == (WorkerEnd.BUDGET, SPEND_CAP_TEXT)
    assert [call.response["content"] for call in cassette.calls] == [SPEND_CAP_TEXT]
    assert (meter.spent_usd, meter.calls) == (Decimal("0.2"), 2)


# The command ---------------------------------------------------------------------------------


@pytest.fixture
def live_cli(admin_dsn: str, db_prefix: str, tmp_path: Path) -> Iterator[tuple[list[str], Path]]:
    """The record command's arguments for demo 1 in G2 with the fake model, and its cassette."""
    names = (f"{db_prefix}_cli_live", f"{db_prefix}_cli_live_play")
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        "price_date: 2026-11-15\nprices:\n  fake:\n    m:\n"
        "      input_usd_per_million_tokens: 0\n      output_usd_per_million_tokens: 0\n",
        encoding="utf-8",
    )
    cassette = tmp_path / "live.json"
    args = ["--dsn", admin_dsn, "record", "--demo", "1", "--config", "G2"]
    args += ["--cassette", str(cassette), "--database", names[0], "--live", "fake:m"]
    args += ["--spend-cap-usd", "0.50", "--prices", str(prices), "--param", "temperature=0"]
    yield args, cassette
    for name in names:
        drop_database(admin_dsn, name)


def _exists(admin_dsn: str, name: str) -> bool:
    with psycopg.connect(admin_dsn) as conn:
        found = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return found is not None


def test_the_command_refuses_a_live_run_before_it_touches_a_database(
    admin_dsn: str,
    db_prefix: str,
    live_cli: tuple[list[str], Path],
    provider: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args, cassette = live_cli
    database = f"{db_prefix}_cli_live"

    def changed(old: str, new: str) -> list[str]:
        return [new if arg == old else arg for arg in args]

    refusals: list[tuple[str | None, list[str], str]] = [
        (None, args, "live runs are off: set FIVEFOLD_LIVE=1"),
        ("yes", args, "live runs are off"),
        ("1", changed("temperature=0", "base_url=http://x"), "temperature, not base_url"),
        ("1", changed("fake:m", "fake:other"), "no price for fake:other"),
        ("1", changed("0.50", "0"), "above 0 USD"),
        ("1", changed("fake:m", "fakem"), "PROVIDER:MODEL"),
        ("1", changed("temperature=0", "temperature=NaN"), "which JSON cannot hold"),
        ("1", [*args, "--call-limit-s", "0"], "above 0 seconds"),
    ]
    for flag, argv, message in refusals:
        if flag is None:
            monkeypatch.delenv("FIVEFOLD_LIVE")
        else:
            monkeypatch.setenv("FIVEFOLD_LIVE", flag)
        assert main(argv) == 1
        error = capsys.readouterr().err
        assert error.startswith("record failed: ") and message in error, error
        assert error.count("\n") == 1
        monkeypatch.setenv("FIVEFOLD_LIVE", "1")
    assert not cassette.exists()
    assert not _exists(admin_dsn, database)
    for missing in ("--prices", "--spend-cap-usd"):
        at = args.index(missing)
        with pytest.raises(SystemExit) as exit_:
            main([*args[:at], *args[at + 2 :]])
        assert exit_.value.code == 2
        assert "--live needs --spend-cap-usd and --prices" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main([*args[: args.index("--live")], "--prices", args[args.index("--prices") + 1]])
    assert "go with --live" in capsys.readouterr().err


def test_the_command_checks_a_parameter_s_value_before_it_touches_a_database(
    admin_dsn: str,
    db_prefix: str,
    live_cli: tuple[list[str], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A value the provider's model refuses is refused in one line, before the run's
    database is reset, so a bad parameter never costs the last run's records."""
    monkeypatch.setenv("FIVEFOLD_LIVE", "1")
    args, cassette = live_cli
    prices = tmp_path / "ollama-prices.yaml"
    prices.write_text(
        "price_date: 2026-11-15\nprices:\n  ollama:\n    m:\n"
        "      input_usd_per_million_tokens: 0\n      output_usd_per_million_tokens: 0\n",
        encoding="utf-8",
    )
    swap = {"fake:m": "ollama:m", "temperature=0": 'temperature="hot"'}
    swap[args[args.index("--prices") + 1]] = str(prices)
    assert main([swap.get(arg, arg) for arg in args]) == 1
    error = capsys.readouterr().err
    assert error.startswith("record failed: ollama refuses temperature"), error
    assert error.count("\n") == 1
    assert not cassette.exists()
    assert not _exists(admin_dsn, f"{db_prefix}_cli_live")


class StalledProvider(BaseChatModel):
    """A provider that accepts every call and never answers."""

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

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)


def test_a_provider_that_never_answers_ends_its_worker_and_the_run_goes_on(
    run_database: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With each call's time limit, a stalled provider fails the call: the worker ends
    with an error, and the run drains and stops instead of waiting forever."""
    monkeypatch.setenv("FIVEFOLD_LIVE", "1")
    monkeypatch.setitem(
        PROVIDERS, "fake", Provider(lambda model, params: StalledProvider(), frozenset())
    )
    meter = SpendMeter(Decimal(1), price_file())
    spec = replace(demo_1_spec("G2", 7), plan=[], model=Live("fake", "m", spend=meter, limit_s=0.2))
    run = InprocRun(spec, run_database("live_stalled"))
    # A wall-clock timer in the run's own loop would hold the scheduler (`settle`), so the
    # run goes in a thread of its own and the test waits for it from outside.
    done, errors = threading.Event(), list[BaseException]()

    def execute() -> None:
        try:
            asyncio.run(run.execute())
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    threading.Thread(target=execute, daemon=True).start()
    assert done.wait(60), "the run kept waiting for the stalled provider"
    assert errors == []
    ((end, text),) = run.result.ends.values()
    assert end is WorkerEnd.ERROR, text
    assert "fake:m did not answer within 0.2 s" in text
    assert meter.calls == 0


def test_the_command_records_a_live_run_and_plays_it_back_with_no_network(
    admin_dsn: str,
    db_prefix: str,
    live_cli: tuple[list[str], Path],
    provider: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args, cassette = live_cli
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Recorded dev-001-single-po.G2.C2.7 in" in out and ": 7 model calls." in out
    assert "model: fake:m; 0 USD spent of the 0.50 USD cap on 7 model calls" in out
    assert "at prices dated 2026-11-15" in out
    assert "  agent-1: finished\n" in out
    loaded = Cassette.load(cassette)
    assert (loaded.model.name, loaded.model.params) == ("fake:m", {"temperature": 0})

    no_provider(monkeypatch)
    play_args = ["--dsn", admin_dsn, "play", str(cassette), "--scenario", str(DEV_001)]
    play_args += ["--original", f"{db_prefix}_cli_live", "--database", f"{db_prefix}_cli_live_play"]
    assert main(play_args) == 0
    out = capsys.readouterr().out
    assert "model calls: identical" in out
    assert f"The replay matches the original run in {db_prefix}_cli_live." in out


def test_the_command_shows_a_provider_that_cannot_be_reached_as_a_worker_s_error(
    live_cli: tuple[list[str], Path],
    provider: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The worker ends with an error and the run goes on; nothing is spent or recorded."""

    def build(model: str, params: Any) -> BaseChatModel:
        return PolicyProvider(usage_by_length, down=True)

    monkeypatch.setitem(PROVIDERS, "fake", Provider(build, frozenset({"temperature"})))
    args, cassette = live_cli
    assert main(args) == 0
    out = capsys.readouterr().out
    assert ": 0 model calls." in out
    assert "0 USD spent of the 0.50 USD cap on 0 model calls" in out
    assert "  agent-1: error, ConnectionError: Failed to connect to Ollama.\n" in out
    assert Cassette.load(cassette).calls == ()


# A live study --------------------------------------------------------------------------------


def write_prices(path: Path, input_usd: str, output_usd: str) -> Path:
    """A price file for the fake model, at the prices given."""
    path.write_text(
        "price_date: 2026-11-15\nprices:\n  fake:\n    m:\n"
        f"      input_usd_per_million_tokens: {input_usd}\n"
        f"      output_usd_per_million_tokens: {output_usd}\n",
        encoding="utf-8",
    )
    return path


def study_databases(admin_dsn: str, prefix: str) -> list[str]:
    """The databases a study with `prefix` names: `prefix`, an underscore and a number."""
    with psycopg.connect(admin_dsn) as conn:
        found = conn.execute(
            "SELECT datname FROM pg_database WHERE datname ~ %s ORDER BY datname",
            (f"^{prefix}_[0-9]+$",),
        ).fetchall()
    return [str(row[0]) for row in found]


def drop_study_databases(admin_dsn: str, prefix: str) -> None:
    for name in study_databases(admin_dsn, prefix):
        drop_database(admin_dsn, name)


def test_a_live_study_saves_each_result_and_cassette_and_the_cassettes_play_back(
    admin_dsn: str,
    db_prefix: str,
    tmp_path: Path,
    run_database: Callable[[str], str],
    provider: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F2 at C2 in B0 and G2, seeds 7 and 8, with the fake model: four episodes, each with
    its result and its cassette. The databases are kept here, so that each cassette can
    play back against the run it was recorded from, with no provider and no flag."""
    prefix, out = f"{db_prefix}_live_study", tmp_path / "study"
    prices = write_prices(tmp_path / "prices.yaml", "1", "2")
    argv = ["--dsn", admin_dsn, "--out", str(out), "--database-prefix", prefix]
    argv += ["--cells", "F2", "--configs", "G2", "B0", "--contracts", "C2", "--episodes", "2"]
    argv += ["--live", "fake:m", "--spend-cap-usd", "1", "--prices", str(prices)]
    argv += ["--param", "temperature=0", "--keep-databases"]
    names = [f"F2-C2-{config}-seed-{seed}" for seed in (7, 8) for config in ("B0", "G2")]
    expected = {cell.name: cell for cell in load_expected().cells}
    try:
        assert study_main(argv) == 0
        printed = capsys.readouterr().out.splitlines()
        assert printed[0] == (
            "Study of dev-001-single-po with the live model fake:m: 4 episodes, 2 cells,"
            " seeds 7 to 8."
        )
        assert printed[1].startswith(
            "[1/4] F2-C2-B0-seed-7: fail (1, 2, 3), expected fail (1, 2, 3); agent-1 finished;"
            " 7 model calls, "
        )
        assert printed[5].startswith("Model fake:m; ")
        assert printed[5].endswith(" USD cap on 30 model calls, at prices dated 2026-11-15.")
        assert printed[6].startswith("Ran all 4 episodes.")
        assert sorted(path.name for path in out.iterdir()) == sorted(
            [*(f"{name}.json" for name in names), "cassettes"]
        )
        assert sorted(path.name for path in (out / "cassettes").iterdir()) == sorted(
            f"{name}.json" for name in names
        )

        no_provider(monkeypatch)
        for number, name in enumerate(names, start=1):
            result = load_result(out / f"{name}.json")
            cassette = Cassette.load(out / "cassettes" / f"{name}.json")
            assert (result.run.model_adapter, result.run.model) == ("live", "fake:m")
            assert result.run.cassette_digest == cassette.digest()
            assert cassette.model.params == {"temperature": 0}
            cell = expected[name.rpartition("-seed-")[0]]
            failing = tuple(p.property for p in result.properties if str(p.verdict) == "fail")
            assert (str(result.verdict), failing) == (cell.verdict, cell.failing), name
            original = make_conninfo(admin_dsn, dbname=f"{prefix}_{number:04d}")
            replay = run_database(f"study_replay_{number}")
            playback = play(cassette, SCENARIO, original=original, replay=replay)
            assert playback.differences == (), name
            assert playback.calls == len(cassette.calls) > 0
    finally:
        drop_study_databases(admin_dsn, prefix)


def test_the_spend_cap_stops_a_live_study_partway_and_the_study_keeps_what_it_ran(
    admin_dsn: str,
    db_prefix: str,
    tmp_path: Path,
    provider: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No fault at C2 in B0, G1 and G2: three episodes of 7 model calls, each call 1,000
    input tokens at 100 USD per million, so 0.10 USD, with a cap of 0.75 USD. The first
    episode ends at 0.70 USD. The second's first call takes the spend to 0.80 USD, so its
    next call is refused and the cap cuts it short: its result goes to `spend-cap/`, which
    the report over the directory leaves out, and its cassette ends with the refusal. The
    third never starts, since its first call would be refused."""
    provider(fixed_usage(1_000, 0))
    prefix, out = f"{db_prefix}_capped_study", tmp_path / "study"
    prices = write_prices(tmp_path / "prices.yaml", "100", "0")
    argv = ["--dsn", admin_dsn, "--out", str(out), "--database-prefix", prefix]
    argv += ["--cells", "none", "--contracts", "C2"]
    argv += ["--live", "fake:m", "--spend-cap-usd", "0.75", "--prices", str(prices)]
    try:
        assert study_main(argv) == 0
    finally:
        left = study_databases(admin_dsn, prefix)
        drop_study_databases(admin_dsn, prefix)
    assert left == [], "each episode's database is dropped once it ends"
    printed = capsys.readouterr().out.splitlines()
    assert printed[1:] == [
        "[1/3] none-C2-B0-seed-7: pass; agent-1 finished; 7 model calls, 0.7 USD spent so far",
        f"[2/3] none-C2-G1-seed-7: fail (4, 5); agent-1 budget ({SPEND_CAP_TEXT}); 2 model"
        " calls, 1 refused, 0.8 USD spent so far; cut short by the spend cap, so its result"
        " is in spend-cap/",
        "Model fake:m; 0.8 USD spent of the 0.75 USD cap on 8 model calls, at prices dated"
        " 2026-11-15.",
        f"The spend cap stopped the study after 2 of 3 episodes. Results are in {out}. Report"
        f" them with: python -m fivefold.evaluator report {out} --csv runs.csv --markdown"
        " report.md",
    ]
    files = sorted(str(path.relative_to(out)) for path in out.rglob("*") if path.is_file())
    assert files == [
        "cassettes/none-C2-B0-seed-7.json",
        "cassettes/none-C2-G1-seed-7.json",
        "none-C2-B0-seed-7.json",
        "spend-cap/none-C2-G1-seed-7.json",
    ]
    cut = Cassette.load(out / "cassettes" / "none-C2-G1-seed-7.json")
    assert cut.calls[-1].response == {
        "content": SPEND_CAP_TEXT,
        "response_metadata": {"fivefold_refused": "spend_cap"},
    }
    assert load_result(out / "spend-cap" / "none-C2-G1-seed-7.json").run.config == "G1"
    assert [result.run.config for result in load_results([out])] == ["B0"]
    assert (
        load_result(out / "none-C2-B0-seed-7.json").run.cassette_digest
        == Cassette.load(out / "cassettes" / "none-C2-B0-seed-7.json").digest()
    )


def test_a_study_whose_spend_reaches_the_cap_as_an_episode_ends_stops_before_the_next(
    admin_dsn: str,
    db_prefix: str,
    tmp_path: Path,
    provider: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With a cap of 0.70 USD, the first episode's seventh and last call takes the spend
    to the cap. No call of that episode was refused, so its result counts as any other;
    the next episode never starts."""
    provider(fixed_usage(1_000, 0))
    prefix, out = f"{db_prefix}_reached_study", tmp_path / "study"
    prices = write_prices(tmp_path / "prices.yaml", "100", "0")
    argv = ["--dsn", admin_dsn, "--out", str(out), "--database-prefix", prefix]
    argv += ["--cells", "none", "--contracts", "C2"]
    argv += ["--live", "fake:m", "--spend-cap-usd", "0.70", "--prices", str(prices)]
    try:
        assert study_main(argv) == 0
    finally:
        drop_study_databases(admin_dsn, prefix)
    printed = capsys.readouterr().out.splitlines()
    assert printed[1] == (
        "[1/3] none-C2-B0-seed-7: pass; agent-1 finished; 7 model calls, 0.7 USD spent so far"
    )
    assert printed[3].startswith("The spend cap stopped the study after 1 of 3 episodes.")
    files = sorted(str(path.relative_to(out)) for path in out.rglob("*") if path.is_file())
    assert files == ["cassettes/none-C2-B0-seed-7.json", "none-C2-B0-seed-7.json"]
