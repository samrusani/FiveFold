"""How a run gets its chat models, and what `harness.run` records of them (0009, Models).

The scripted adapter gives each worker the scripted model; with recording, the model is
wrapped and the cassette is complete when the run stops; a replay gives each worker the
replay adapter, and names the recorded model and the cassette from the start.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from fivefold.agents.live import Think
from fivefold.agents.policy import Mode, scripted_policy
from fivefold.agents.replay import ReplayError, ReplayModel
from fivefold.agents.scripted import ScriptedModel
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.demo import DEV_001
from fivefold.harness.log import MemoryHarnessLog, RunRecord
from fivefold.harness.models import (
    SCRIPTED,
    SCRIPTED_MODEL,
    Replay,
    RunModels,
    Scripted,
    run_header,
)
from fivefold.recorder.cassette import Cassette, ModelInfo
from fivefold.recorder.recording import RecordingModel

SCENARIO = load_scenario_file(DEV_001)
RUN = RunRecord(
    run_id="dev-001-single-po.G2.C2.7",
    scenario_id=SCENARIO.id,
    scenario_digest="ab" * 32,
    config="G2",
    contract="C2",
    seed=7,
    model_adapter="scripted",
    model="scripted",
    params={"plan": [], "workers": 1, "agent_workers": 1},
)
TASK = [HumanMessage(SCENARIO.task)]


async def no_time() -> None:
    """These tests have no scheduler."""


def scripted(think: Think = no_time) -> BaseChatModel:
    """The worker's scripted model, which takes `think` before each call."""
    return ScriptedModel(lambda: scripted_policy(SCENARIO.intents, mode=Mode.GATEWAY), think)


def never(think: Think) -> BaseChatModel:
    raise AssertionError("a replay builds no scripted model")


def recorded_cassette() -> Cassette:
    models = RunModels(Scripted(record=True))
    model = models.chat_model("agent-1", scripted=scripted, think=no_time)
    asyncio.run(model.ainvoke(TASK))
    models.stopped(RUN)
    assert models.cassette is not None
    return models.cassette


def test_a_run_is_scripted_unless_it_says_otherwise() -> None:
    models = RunModels(SCRIPTED)
    assert (models.adapter, models.name, models.cassette_digest) == ("scripted", "scripted", None)
    assert isinstance(models.chat_model("agent-1", scripted=scripted, think=no_time), ScriptedModel)
    assert models.stopped(RUN) is None
    assert models.cassette is None
    models.check()


def test_recording_wraps_the_scripted_model_and_keeps_the_cassette_when_the_run_stops() -> None:
    models = RunModels(Scripted(record=True))
    assert (models.adapter, models.name, models.cassette_digest) == ("scripted", "scripted", None)
    model = models.chat_model("agent-1", scripted=scripted, think=no_time)
    assert isinstance(model, RecordingModel)
    asyncio.run(model.ainvoke(TASK))
    before = models.cassette
    digest = models.stopped(RUN)
    cassette = models.cassette
    assert before is None, "the cassette is complete only when the run stops"
    assert cassette is not None
    assert digest == cassette.digest()
    assert cassette.run == run_header(RUN)
    assert cassette.model == SCRIPTED_MODEL
    assert [call.worker for call in cassette.calls] == ["agent-1"]


def test_a_replay_names_the_recorded_model_and_its_cassette_from_the_start() -> None:
    cassette = recorded_cassette()
    live = cassette.model_copy(update={"model": ModelInfo(adapter="live", name="model-x")})
    named = RunModels(Replay(live))
    assert (named.adapter, named.name, named.cassette_digest) == (
        "replay",
        "model-x",
        live.digest(),
    )
    assert named.cassette == live

    models = RunModels(Replay(cassette))
    assert (models.adapter, models.name) == ("replay", "scripted")
    model = models.chat_model("agent-1", scripted=never, think=no_time)
    assert isinstance(model, ReplayModel)
    with pytest.raises(ReplayError, match="the run made 0 model calls"):
        models.check()
    asyncio.run(model.ainvoke(TASK))
    assert models.served == 1
    assert models.stopped(RUN) is None
    models.check()


def test_the_model_s_name_is_part_of_every_request() -> None:
    """A cassette that names another model no longer replays: its hashes were made with
    the recorded model's name."""
    cassette = recorded_cassette()
    renamed = cassette.model_copy(update={"model": ModelInfo(adapter="live", name="model-x")})
    models = RunModels(Replay(renamed))
    model = models.chat_model("agent-1", scripted=never, think=no_time)
    with pytest.raises(ReplayError, match="model call 1"):
        asyncio.run(model.ainvoke(TASK))


def test_the_cassette_names_the_run_as_its_record_does_without_the_model() -> None:
    header = run_header(RUN)
    assert header.model_dump() == {
        "run_id": RUN.run_id,
        "scenario_id": RUN.scenario_id,
        "scenario_digest": RUN.scenario_digest,
        "config": "G2",
        "contract": "C2",
        "seed": 7,
        "topology": "inproc",
        "params": {"plan": [], "workers": 1, "agent_workers": 1},
        "parent_run_id": None,
        "branch_point": None,
    }
    branch = replace(RUN, run_id=f"{RUN.run_id}.branch-3", parent_run_id=RUN.run_id, branch_point=3)
    assert (run_header(branch).parent_run_id, run_header(branch).branch_point) == (RUN.run_id, 3)


def test_the_memory_log_records_a_cassette_once() -> None:
    log = MemoryHarnessLog()
    with pytest.raises(ValueError, match="never recorded"):
        log.cassette_recorded("ab" * 32)
    log.run_recorded(RUN)
    log.cassette_recorded("ab" * 32)
    assert log.run is not None
    assert log.run.cassette_digest == "ab" * 32
    with pytest.raises(ValueError, match="already names its cassette"):
        log.cassette_recorded("cd" * 32)
