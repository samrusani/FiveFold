"""Experimental branches of whole runs (0009, Playback and branching).

Demo 1 is recorded with the scripted model, then branched: its first K model calls are
replayed from its cassette, and the scripted model or a live one answers the rest. Branched
to the scripted model at any K, from none of the calls to all of them, the branch is the
parent's run again: its records are the parent's but for the run's identity (its run id,
parent, branch point, model adapter and cassette digest). Branched to a live model, here a
fake provider that answers as a real model does, it records a cassette of its own that
`play` replays with no difference, with no provider and no flag. A branch is a new run:
`harness.run` names its parent and its branch point, its run id is the parent's with
`.branch-K` added, and a branch of a branch works the same way. The command refuses, in
one line and before it touches any database, a K beyond the cassette, a scenario other
than the parent's, a live model without its flag, with a cap of 0 or with no price, and
outputs that would replace the parent's cassette or each other; `--live` without a spend
cap or a price file is a usage error, as for `record`. A branch that asks another request
than its parent within its first K calls fails as a replay does. The evaluator scores a
branch like any run, and its report shows a branch apart from the parent's cell. No test
reaches a model provider.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
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
from psycopg import sql
from psycopg.conninfo import make_conninfo

from fivefold.agents.live import PROVIDERS, Provider, SpendMeter
from fivefold.agents.policy import Mode, Result, scripted_policy
from fivefold.agents.worker import WorkerEnd
from fivefold.domain.contracts import Contract
from fivefold.domain.prices import PriceFile, price_file_from_text
from fivefold.domain.scenario import load_scenario_file, scenario_from_data
from fivefold.evaluator import result_json
from fivefold.evaluator.__main__ import main as report
from fivefold.evaluator.report import load_result
from fivefold.harness.demo import DEV_001, DEV_002, demo_1_plan, demo_1_spec
from fivefold.harness.migrations import drop_database
from fivefold.harness.models import Live
from fivefold.harness.run import Config, InprocRun, RunSpec
from fivefold.recorder.__main__ import main
from fivefold.recorder.cassette import Cassette, ModelInfo
from fivefold.recorder.playback import branch_spec, play, record, records

pytestmark = pytest.mark.pg

SCENARIO = load_scenario_file(DEV_001)
PARENT_ID = "dev-001-single-po.G2.C2.7"
CALLS = 7
"""Demo 1's model calls in G2: two quotes, the budget, the order, its status, the report
and the closing "Done."."""

RUN_ROW: LiteralString = (
    "SELECT run_id, parent_run_id, branch_point, model_adapter, model, cassette_digest"
    " FROM harness.run"
)
REST_OF_RUN: LiteralString = (
    "SELECT scenario_id, scenario_digest, config, contract, seed, topology, params FROM harness.run"
)
MODEL_CALLS: LiteralString = (
    "SELECT call_no, worker, asked_vt, answered_vt, model, input_tokens, output_tokens, refused"
    " FROM harness.model_call ORDER BY call_no"
)
IDENTITY = ("run_id", "parent_run_id", "branch_point", "model_adapter", "cassette_digest")
"""What a branch's records may say differently from its parent's: the run's identity."""


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


def without_identity(dsn: str) -> dict[str, list[str]]:
    """The records `play` compares, without the run's identity: the run id that the stop
    time and the evaluation name, and the evaluation's parent, branch point, model adapter
    and cassette digest."""
    found: dict[str, list[str]] = {}
    for name, lines in records(dsn).items():
        kept = []
        for line in lines:
            row = json.loads(line)
            row.pop("run_id", None)
            if name == "evaluation":
                for field in IDENTITY:
                    row["result"]["run"].pop(field, None)
            kept.append(json.dumps(row, sort_keys=True))
        found[name] = kept
    return found


@dataclass(frozen=True)
class Recorded:
    """A recorded parent: its database, its cassette and the cassette's file."""

    dsn: str
    cassette: Cassette
    path: Path
    result: str
    """Its result JSON."""


@pytest.fixture(scope="module")
def parents(
    admin_dsn: str, sim_template: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[dict[Config, Recorded]]:
    """Demo 1 recorded with the scripted model in G2 and B0, once for the module."""
    made: list[str] = []
    found: dict[Config, Recorded] = {}
    directory = tmp_path_factory.mktemp("parents")
    try:
        for config in ("G2", "B0"):
            name = f"{sim_template}_parent_{config.lower()}"
            drop_database(admin_dsn, name)
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                        sql.Identifier(name), sql.Identifier(sim_template)
                    )
                )
            made.append(name)
            dsn = make_conninfo(admin_dsn, dbname=name)
            run, cassette = record(demo_1_spec(config, 7), dsn)
            assert run.result.evaluation is not None
            path = directory / f"demo-1-{config.lower()}.json"
            cassette.save(path)
            found[config] = Recorded(dsn, cassette, path, result_json(run.result.evaluation))
        yield found
    finally:
        for name in made:
            drop_database(admin_dsn, name)


# Branches to the scripted model ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "point"),
    [("G2", 0), ("G2", 1), ("G2", 4), ("G2", CALLS), ("B0", 4)],
)
def test_a_scripted_branch_of_demo_1_has_the_parent_s_records_but_for_its_identity(
    parents: dict[Config, Recorded], run_database: Callable[[str], str], config: Config, point: int
) -> None:
    """K = 0 replays nothing; K = 7, the cassette's length, replays every call, and the
    scripted model is never asked. Either way the scripted model answers as the parent's
    did, so the branch is the parent's run again, and its own cassette plays back."""
    parent = parents[config]
    parent_id = parent.cassette.run.run_id
    tag = f"{config.lower()}_{point}"
    dsn = run_database(f"branch_{tag}")
    run, cassette = record(branch_spec(parent.cassette, SCENARIO, point), dsn)

    assert run.models.served == point
    assert run.result.ends == {"agent-1": (WorkerEnd.FINISHED, "Done.")}
    assert without_identity(dsn) == without_identity(parent.dsn)
    assert rows(dsn, REST_OF_RUN) == rows(parent.dsn, REST_OF_RUN)
    assert rows(dsn, RUN_ROW) == [
        (f"{parent_id}.branch-{point}", parent_id, point, "scripted", "scripted", cassette.digest())
    ]
    assert rows(parent.dsn, RUN_ROW) == [
        (parent_id, None, None, "scripted", "scripted", parent.cassette.digest())
    ]
    assert run.result.evaluation is not None
    summary = run.result.evaluation.run
    assert (summary.run_id, summary.parent_run_id, summary.branch_point) == (
        f"{parent_id}.branch-{point}",
        parent_id,
        point,
    )

    assert cassette.calls == parent.cassette.calls, "the same model: the same hashes too"
    assert cassette.model == parent.cassette.model
    assert cassette.run == parent.cassette.run.model_copy(
        update={
            "run_id": f"{parent_id}.branch-{point}",
            "parent_run_id": parent_id,
            "branch_point": point,
        }
    )
    playback = play(cassette, SCENARIO, original=dsn, replay=run_database(f"branch_{tag}_play"))
    assert playback.differences == ()
    assert (playback.run_id, playback.calls) == (f"{parent_id}.branch-{point}", len(cassette.calls))


def test_a_branch_of_a_branch_names_its_own_parent(
    parents: dict[Config, Recorded], run_database: Callable[[str], str]
) -> None:
    """A branch's cassette is a parent like any other; its branch points count its calls."""
    parent = parents["G2"]
    _, first = record(branch_spec(parent.cassette, SCENARIO, 4), run_database("twice_first"))
    dsn = run_database("twice_second")
    run, second = record(branch_spec(first, SCENARIO, 6), dsn)
    assert run.models.served == 6
    assert rows(dsn, RUN_ROW) == [
        (
            f"{PARENT_ID}.branch-4.branch-6",
            f"{PARENT_ID}.branch-4",
            6,
            "scripted",
            "scripted",
            second.digest(),
        )
    ]
    assert without_identity(dsn) == without_identity(parent.dsn)
    assert play(second, SCENARIO, original=dsn, replay=run_database("twice_play")).matches


def test_two_workers_branch_where_their_calls_interleave(
    run_database: Callable[[str], str],
) -> None:
    """F9 starts a second worker; the branch point falls just after the second worker's
    first call, so both workers' calls switch model at once, in the order answered."""
    f9 = {
        "fault": "F9",
        "variant": "different_args",
        "at": {"point": "agent.step", "match": {"worker": "agent-1"}, "occurrence": 2},
    }
    spec = RunSpec(SCENARIO, "G2", Contract.C2, 7, plan=[f9])
    parent_dsn, dsn = run_database("f9_parent"), run_database("f9_branch")
    _, cassette = record(spec, parent_dsn)
    workers = [call.worker for call in cassette.calls]
    point = workers.index("agent-2") + 1
    assert set(workers[point:]) == {"agent-1", "agent-2"}, "both workers call after K"
    run, branched = record(branch_spec(cassette, SCENARIO, point), dsn)
    assert run.models.served == point
    assert [call.worker for call in branched.calls] == workers
    assert without_identity(dsn) == without_identity(parent_dsn)


def test_a_branch_must_run_its_parent_s_run(
    parents: dict[Config, Recorded], dev_data: dict[str, Any]
) -> None:
    """The same scenario, configuration, contract level, seed, fault plan and workers;
    the run refuses before it touches its database."""
    spec = branch_spec(parents["G2"].cassette, SCENARIO, 4)
    dev_data["facts"]["quote/S-1/W-100"]["unit_price_minor"] = 545
    for changed, fields in [
        (replace(spec, seed=8), "seed"),
        (replace(spec, plan=demo_1_plan("G2")[:1]), "params"),
        (replace(spec, scenario=scenario_from_data(dev_data)), "scenario_digest"),
    ]:
        with pytest.raises(ValueError, match=f"it differs from {PARENT_ID} in {fields}$"):
            InprocRun(changed, "postgresql://nowhere.invalid/none")


# A branch to a live model -----------------------------------------------------------------------


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
    """A fake provider's chat model that answers as a real model does, as in
    `tests/pg/test_live_runs.py`: it plays the scripted policy over the conversation, with
    the worker's tools bound, gives its tool calls random ids, reports token usage, and
    awaits the network in wall time."""

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
        input_tokens = sum(len(str(message.content)) for message in messages) // 4 + 10
        usage = UsageMetadata(
            input_tokens=input_tokens, output_tokens=25, total_tokens=input_tokens + 25
        )
        if call is None:
            reply = AIMessage("Done.", usage_metadata=usage)
        else:
            assert call.name in tools, f"{call.name} is not bound to the model"
            tool_call = {"name": call.name, "args": dict(call.args), "id": str(uuid.uuid4())}
            reply = AIMessage("", tool_calls=[tool_call], usage_metadata=usage)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)


def price_file(input_usd: str, output_usd: str) -> PriceFile:
    return price_file_from_text(price_text(input_usd, output_usd))


def price_text(input_usd: str, output_usd: str) -> str:
    return (
        "price_date: 2026-11-15\nprices:\n  fake:\n    m:\n"
        f"      input_usd_per_million_tokens: {input_usd}\n"
        f"      output_usd_per_million_tokens: {output_usd}\n"
    )


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow live runs, and register the fake provider `fake`."""
    monkeypatch.setenv("FIVEFOLD_LIVE", "1")

    def build(model: str, params: Any) -> BaseChatModel:
        assert model == "m"
        return PolicyProvider()

    monkeypatch.setitem(PROVIDERS, "fake", Provider(build, frozenset({"temperature"})))


def no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the fake provider away and turn live runs off, as on a machine with no model."""
    monkeypatch.delitem(PROVIDERS, "fake")
    monkeypatch.delenv("FIVEFOLD_LIVE")


def test_a_branch_to_a_live_model_records_a_cassette_that_plays_back(
    parents: dict[Config, Recorded],
    run_database: Callable[[str], str],
    provider: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Demo 1 in G2, the scripted model's first 4 calls replayed, then the fake provider:
    only its 3 calls are sent, charged and given tokens. The provider plays the scripted
    policy, so every event fires as in the parent. The branch's cassette names the live
    model and hashes every call with it; it replays with no provider and no flag."""
    parent = parents["G2"]
    dsn = run_database("live_branch")
    meter = SpendMeter(Decimal(1), price_file("1", "2"))
    model = Live("fake", "m", {"temperature": 0}, spend=meter)
    run, cassette = record(branch_spec(parent.cassette, SCENARIO, 4, model), dsn)

    assert run.result.ends == {"agent-1": (WorkerEnd.FINISHED, "Done.")}
    assert run.models.served == 4
    assert meter.calls == 3, "the replayed calls are never sent"
    assert cassette.model == ModelInfo(adapter="live", name="fake:m", params={"temperature": 0})
    assert (cassette.run.run_id, cassette.run.parent_run_id, cassette.run.branch_point) == (
        f"{PARENT_ID}.branch-4",
        PARENT_ID,
        4,
    )
    assert len(cassette.calls) == CALLS
    assert [call.response for call in cassette.calls[:4]] == [
        call.response for call in parent.cassette.calls[:4]
    ]
    assert all("usage_metadata" in call.response for call in cassette.calls[4:])
    assert all(
        mine.request != theirs.request
        for mine, theirs in zip(cassette.calls, parent.cassette.calls, strict=True)
    ), "every call is hashed with the live model"

    logged = rows(dsn, MODEL_CALLS)
    assert [row[4] for row in logged] == ["fake:m"] * CALLS
    assert [row[5] is None for row in logged] == [True] * 4 + [False] * 3
    assert [row[1:4] for row in logged] == [row[1:4] for row in rows(parent.dsn, MODEL_CALLS)]
    mine, theirs = without_identity(dsn), without_identity(parent.dsn)
    for name in theirs:
        if name not in ("evaluation", "model calls"):
            assert mine[name] == theirs[name], name
    assert rows(dsn, RUN_ROW) == [
        (f"{PARENT_ID}.branch-4", PARENT_ID, 4, "live", "fake:m", cassette.digest())
    ]
    assert run.result.evaluation is not None
    assert run.result.evaluation.metrics["tokens_out"] == 3 * 25

    no_provider(monkeypatch)
    replay = run_database("live_branch_play")
    playback = play(cassette, SCENARIO, original=dsn, replay=replay)
    assert playback.differences == ()
    assert playback.calls == CALLS
    assert rows(replay, RUN_ROW) == [
        (f"{PARENT_ID}.branch-4", PARENT_ID, 4, "replay", "fake:m", cassette.digest())
    ]


# The command -------------------------------------------------------------------------------------


@pytest.fixture
def cli(admin_dsn: str, db_prefix: str) -> Iterator[tuple[str, str]]:
    """Names for the command's databases, which it creates; dropped afterwards."""
    names = (f"{db_prefix}_cli_branched", f"{db_prefix}_cli_branch_play")
    yield names
    for name in names:
        drop_database(admin_dsn, name)


def _exists(admin_dsn: str, name: str) -> bool:
    with psycopg.connect(admin_dsn) as conn:
        found = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return found is not None


def test_the_command_branches_a_run_and_play_replays_the_branch(
    parents: dict[Config, Recorded],
    admin_dsn: str,
    cli: tuple[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    branched, replayed = cli
    cassette, result = tmp_path / "branch.json", tmp_path / "branch-result.json"
    args = ["--dsn", admin_dsn, "branch", str(parents["G2"].path), "--scenario", str(DEV_001)]
    args += ["--at", "4", "--cassette", str(cassette), "--result", str(result)]
    assert main([*args, "--database", branched]) == 0
    out = capsys.readouterr().out
    saved = Cassette.load(cassette)
    assert out.splitlines() == [
        f"Branched {PARENT_ID}.branch-4 from {PARENT_ID} at branch point 4 in {branched}:"
        " 7 model calls, the first 4 replayed from the parent's cassette.",
        "  model after the branch point: scripted",
        "  verdict: pass  [properties evaluated: 1, 2, 3, 4, 5]",
        f"  cassette: {cassette}, sha256 {saved.digest()}",
        f"  result: {result}",
        "  agent-1: finished",
    ]
    loaded = load_result(result)
    assert (loaded.run.run_id, loaded.run.parent_run_id, loaded.run.branch_point) == (
        f"{PARENT_ID}.branch-4",
        PARENT_ID,
        4,
    )
    assert loaded.run.cassette_digest == saved.digest()

    play_args = ["--dsn", admin_dsn, "play", str(cassette), "--scenario", str(DEV_001)]
    assert main([*play_args, "--original", branched, "--database", replayed]) == 0
    assert f"The replay matches the original run in {branched}." in capsys.readouterr().out

    results = tmp_path / "results"
    results.mkdir()
    (results / "parent.json").write_text(parents["G2"].result, encoding="utf-8")
    (results / "branch.json").write_bytes(result.read_bytes())
    markdown = tmp_path / "report.md"
    assert report(["report", str(results), "--markdown", str(markdown)]) == 0
    lines = markdown.read_text(encoding="utf-8").splitlines()
    assert "| G2 | C2 | 1/1 | 0/1 | 0/1 |" in lines, "the parent's cell holds the parent only"
    assert "| G2, branch point 4 | C2 | 1/1 | 0/1 | 0/1 |" in lines


def test_the_command_branches_to_a_live_model(
    parents: dict[Config, Recorded],
    admin_dsn: str,
    cli: tuple[str, str],
    tmp_path: Path,
    provider: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    branched, _ = cli
    prices = tmp_path / "prices.yaml"
    prices.write_text(price_text("0", "0"), encoding="utf-8")
    cassette, result = tmp_path / "live.json", tmp_path / "live-result.json"
    args = ["--dsn", admin_dsn, "branch", str(parents["G2"].path), "--scenario", str(DEV_001)]
    args += ["--at", "4", "--cassette", str(cassette), "--result", str(result)]
    args += ["--database", branched, "--live", "fake:m", "--spend-cap-usd", "0.50"]
    args += ["--prices", str(prices), "--param", "temperature=0"]
    assert main(args) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "  model after the branch point: fake:m"
    assert out[5] == (
        "  spend: 0 USD spent of the 0.50 USD cap on 3 model calls, at prices dated 2026-11-15"
    )
    assert Cassette.load(cassette).model.name == "fake:m"
    assert load_result(result).run.model_adapter == "live"


def test_a_branch_that_asks_another_request_before_its_branch_point_fails(
    parents: dict[Config, Recorded],
    admin_dsn: str,
    cli: tuple[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The parent's second reply, edited, asks for S-1's quote again: the third request
    then carries another result than the parent's did. Within the first K calls, that
    fails the branch as it fails a replay, and the command writes nothing."""
    branched, _ = cli
    data = json.loads(parents["G2"].path.read_text(encoding="utf-8"))
    data["calls"][1]["response"]["tool_calls"][0]["args"]["supplier_id"] = "S-1"
    edited = tmp_path / "edited.json"
    edited.write_text(
        json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    cassette, result = tmp_path / "branch.json", tmp_path / "branch-result.json"
    args = ["--dsn", admin_dsn, "branch", str(edited), "--scenario", str(DEV_001), "--at", "5"]
    args += ["--cassette", str(cassette), "--result", str(result), "--database", branched]
    assert main(args) == 1
    error = capsys.readouterr().err
    recorded = parents["G2"].cassette.calls[2].request
    assert error.startswith("branch failed: model call 3: agent-1 asked a request with hash")
    assert error.endswith(f"but the cassette recorded agent-1 asking one with hash {recorded}\n")
    assert not cassette.exists() and not result.exists()


def test_the_command_refuses_in_one_line_before_it_touches_a_database(
    parents: dict[Config, Recorded],
    admin_dsn: str,
    cli: tuple[str, str],
    tmp_path: Path,
    provider: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    branched, _ = cli
    parent = str(parents["G2"].path)
    changed = tmp_path / "changed.yaml"
    changed.write_text(
        DEV_001.read_text(encoding="utf-8").replace(
            "unit_price_minor: 540", "unit_price_minor: 545"
        ),
        encoding="utf-8",
    )
    prices = tmp_path / "prices.yaml"
    prices.write_text(price_text("0", "0"), encoding="utf-8")
    cassette, result = tmp_path / "branch.json", tmp_path / "branch-result.json"

    def command(*change: str, scenario: Path = DEV_001, at: str = "4") -> list[str]:
        args = ["--dsn", admin_dsn, "branch", parent, "--scenario", str(scenario), "--at", at]
        args += ["--cassette", str(cassette), "--result", str(result), "--database", branched]
        return [*args, *change]

    live = ["--live", "fake:m", "--spend-cap-usd", "0.50", "--prices", str(prices)]
    digest = parents["G2"].cassette.run.scenario_digest
    refusals: list[tuple[str | None, list[str], str]] = [
        ("1", command(at="8"), "the branch point must be from 0 to 7, the model calls in the"),
        ("1", command(at="-1"), "from 0 to 7, the model calls in the parent's cassette, not -1"),
        ("1", command(scenario=changed), "changed since the recording: its digest is"),
        ("1", command(scenario=changed), f"and the cassette's is {digest}"),
        ("1", command(scenario=DEV_002), "recorded on dev-001-single-po, not dev-002"),
        (None, command(*live), "live runs are off: set FIVEFOLD_LIVE=1"),
        ("1", command(*live[:1], "fake:other", *live[2:]), "no price for fake:other"),
        ("1", command(*live[:3], "0", *live[4:]), "above 0 USD"),
        ("1", command("--cassette", parent), "would replace the parent's cassette"),
        ("1", command("--result", str(cassette)), "--cassette and --result name the same file"),
    ]
    for flag, argv, message in refusals:
        if flag is None:
            monkeypatch.delenv("FIVEFOLD_LIVE")
        assert main(argv) == 1, message
        error = capsys.readouterr().err
        assert error.startswith("branch failed: ") and message in error, error
        assert error.count("\n") == 1
        monkeypatch.setenv("FIVEFOLD_LIVE", "1")
    assert not _exists(admin_dsn, branched)
    assert not cassette.exists() and not result.exists()
    for incomplete in (live[:4], ["--spend-cap-usd", "1"]):
        with pytest.raises(SystemExit) as exit_:
            main(command(*incomplete))
        assert exit_.value.code == 2
    capsys.readouterr()
