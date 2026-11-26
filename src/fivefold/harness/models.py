"""The run's model adapter (0009): the chat model each agent worker gets.

`RunSpec.model` chooses one:

- `Scripted()`: the scripted adapter, the policy of 0008 behind a chat model.
- `Scripted(record=True)`: the same, wrapped by the recorder, so every request and response
  goes into a cassette (`fivefold.recorder.recording`).
- `Replay(cassette)`: the replay adapter, which serves the cassette's responses in order
  (`fivefold.agents.replay`).
- `Live(provider, model, params, spend)`: the live adapter, a provider's chat model
  (`fivefold.agents.live`), always wrapped by the recorder (0009).
- `Branch(parent, point, model)`: an experimental branch (0009, Playback and branching).
  The branch adapter replays the first `point` calls of the cassette `parent`, then
  hands every later call to `model`, the scripted model or a live one
  (`fivefold.agents.branch`). A branch always records its own cassette, which holds all
  its calls, the replayed ones included.

`harness.run` records the adapter (`scripted`, `live` or `replay`), the model's name, and
the cassette's digest: for a replay, the cassette it serves, from the start; for a
recording, the cassette it made, once the run has stopped and the cassette is complete. A
recording run and its replays therefore name the same digest. A live model's name says the
provider and the model, such as `ollama:llama3.1:8b`, and its replays name it too.

A branch records the adapter and the name of the model it switches to, and the digest of
its own cassette. Its every model call goes into `harness.model_call` under that name, as
the run's model, and its cassette hashes every call with that model, the replayed calls
included, so that `play` replays a branch as any other recorded run. The branch's
`Parent`, its parent's run id and its branch point, goes into `harness.run` as
`parent_run_id` and `branch_point`, and into its cassette's header; a replay of the
branch's cassette records the same.

Live runs sit behind an explicit flag and a spend cap (CLAUDE.md). A live run is refused,
before anything runs, unless the environment sets `FIVEFOLD_LIVE=1`, the run has a spend
cap in USD with a price file (`Live.spend`), the price file prices the model, so the cap
can always be enforced, and the environment sets the provider's API key, for a provider
that needs one (`check_live`). The key's variable is checked for being set and nothing
more: the provider's package reads the key itself (`fivefold.agents.live.PROVIDERS`). A
live run is also refused while a variable that would send the provider's requests to
another backend is set, such as `GOOGLE_GENAI_USE_VERTEXAI`.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tracers.context import tracing_v2_callback_var
from langsmith.utils import tracing_is_enabled
from pydantic import JsonValue

from fivefold.agents.branch import BranchModel, BranchPlayer, check_branch_point
from fivefold.agents.live import (
    CALL_LIMIT_S,
    PROVIDERS,
    Hold,
    LiveModel,
    SpendMeter,
    Think,
    live_name,
    provider_model,
)
from fivefold.agents.replay import CassettePlayer, ReplayModel
from fivefold.domain.prices import ModelPrice
from fivefold.harness.log import RunRecord
from fivefold.recorder.cassette import Cassette, ModelInfo, RunHeader, plain_object
from fivefold.recorder.recording import CassetteRecorder, RecordingModel

SCRIPTED_MODEL: Final = ModelInfo(adapter="scripted", name="scripted")
"""The scripted adapter's model, as `harness.run` and a cassette name it."""

LIVE_FLAG: Final = "FIVEFOLD_LIVE"
"""The environment variable that must be `1` for a live run to start."""


async def _no_think() -> None:
    """The think of a model that answers after the branch adapter has thought for it."""


@dataclass(frozen=True)
class Scripted:
    """The scripted adapter (0009); with `record`, wrapped by the recorder."""

    record: bool = False


@dataclass(frozen=True)
class Replay:
    """The replay adapter (0009), serving `cassette`'s responses in order."""

    cassette: Cassette


@dataclass(frozen=True)
class Live:
    """The live adapter (0009): `provider`'s `model` with `params`, always recorded.

    `params` are the model's generation parameters, which the cassette records and every
    request hash covers. `spend` is the spend cap and price file of the command that runs
    it, shared by the command's runs; a live run is refused without one. `limit_s` is how
    long, in wall-clock seconds, each call may wait for the provider before it fails.
    """

    provider: str
    model: str
    params: Mapping[str, JsonValue] = field(default_factory=dict)
    spend: SpendMeter | None = None
    limit_s: float = CALL_LIMIT_S

    @property
    def name(self) -> str:
        """The model's name, as `harness.run` and the cassette record it."""
        return live_name(self.provider, self.model)


SCRIPTED: Final = Scripted()
"""The scripted adapter, without recording: a run's default."""


@dataclass(frozen=True)
class Branch:
    """An experimental branch (0009): the first `point` model calls of the run `parent`
    was recorded from, replayed from it, then `model`, the scripted model or a live one.

    `point` is the branch point, K: how many of the parent's calls the branch replays,
    from 0 to all of them. The branch always records its own cassette, whatever
    `model`'s `record` says.
    """

    parent: Cassette
    point: int
    model: Scripted | Live = SCRIPTED

    def __post_init__(self) -> None:
        check_branch_point(self.parent, self.point)


ModelSpec = Scripted | Replay | Live | Branch
"""Which model adapter a run uses."""


@dataclass(frozen=True)
class Parent:
    """The run a branch comes from (0009): the parent's run id, and the branch point, K,
    the number of the parent's model calls the branch replays."""

    run_id: str
    branch_point: int


def branch_run_id(parent: Parent) -> str:
    """A branch's run id: its parent's run id, `.branch-` and its branch point, such as
    `dev-001-single-po.G2.C2.7.branch-4`. A branch of a branch adds a second suffix."""
    return f"{parent.run_id}.branch-{parent.branch_point}"


def parent_of(spec: ModelSpec) -> Parent | None:
    """The parent of a run with the model `spec`: a branch's, or, for a replay of a
    branch's cassette, the one its header names. None for any other run."""
    if isinstance(spec, Branch):
        return Parent(spec.parent.run.run_id, spec.point)
    if isinstance(spec, Replay):
        header = spec.cassette.run
        if header.parent_run_id is not None and header.branch_point is not None:
            return Parent(header.parent_run_id, header.branch_point)
    return None


class LiveRefused(ValueError):
    """A live run that must not start: the flag, the spend cap, the model's price or the
    provider's API key is missing, or a variable would send the provider's requests to
    another backend."""


def check_live_flag(environ: Mapping[str, str]) -> None:
    """Refuse any live run unless `environ` sets `FIVEFOLD_LIVE=1`."""
    if environ.get(LIVE_FLAG) != "1":
        raise LiveRefused(
            f"live runs are off: set {LIVE_FLAG}=1 to allow one, with a spend cap and a price file"
        )


def check_live(spec: Live, environ: Mapping[str, str]) -> tuple[SpendMeter, ModelPrice]:
    """Refuse a live run unless `environ` sets `FIVEFOLD_LIVE=1`, the run has a spend cap
    with a price file that prices its model, and `environ` sets the provider's API key and
    none of the variables the provider refuses; return the meter and the price.

    Refuse LangSmith's gateway or enabled tracing before constructing a provider.
    A provider this build does not have is refused when its model is built.
    """
    check_live_flag(environ)
    if spec.spend is None:
        raise LiveRefused("a live run needs a spend cap in USD and a price file")
    if not (math.isfinite(spec.limit_s) and spec.limit_s > 0):
        raise LiveRefused(f"a live call's time limit must be above 0 seconds, not {spec.limit_s}")
    price = spec.spend.prices.price(spec.provider, spec.model)
    if environ.get("LANGSMITH_GATEWAY"):
        raise LiveRefused("a live run refuses LANGSMITH_GATEWAY: unset it")
    # LangSmith 0.14.17 checks V2 before TRACING, with LANGSMITH before LANGCHAIN.
    for name in (
        "LANGSMITH_TRACING_V2",
        "LANGCHAIN_TRACING_V2",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING",
    ):
        value = environ.get(name)
        if value is not None and value.strip():
            if value == "true":
                raise LiveRefused(f"a live run refuses enabled LangSmith tracing in {name}")
            break
    # The SDK can also enable tracing from a context or keep an earlier setting cached.
    if tracing_v2_callback_var.get() is not None or tracing_is_enabled():
        raise LiveRefused("a live run refuses enabled LangSmith tracing")
    entry = PROVIDERS.get(spec.provider)
    if entry is not None and entry.keys and not any(environ.get(key) for key in entry.keys):
        raise LiveRefused(
            f"a live run of {spec.provider} needs its API key in {' or '.join(entry.keys)}"
        )
    for name in entry.forbidden if entry is not None else ():
        if environ.get(name):
            raise LiveRefused(
                f"a live run of {spec.provider} refuses {name}, which can switch backends"
                " or enable SDK recording or replay: unset it"
            )
    return spec.spend, price


def run_header(record: RunRecord) -> RunHeader:
    """What a cassette says about the run it was recorded from: the run's record, without
    the model, which the cassette names on its own."""
    return RunHeader(
        run_id=record.run_id,
        scenario_id=record.scenario_id,
        scenario_digest=record.scenario_digest,
        config=record.config,
        contract=record.contract,
        seed=record.seed,
        topology=record.topology,
        params=plain_object(record.params, "the run's params"),
        parent_run_id=record.parent_run_id,
        branch_point=record.branch_point,
    )


@dataclass(frozen=True)
class _LiveSetup:
    """A live run's model, built once the run passed `check_live`."""

    spec: Live
    chat: BaseChatModel
    meter: SpendMeter
    price: ModelPrice


class RunModels:
    """The chat models of one run's agent workers, and the run's cassette."""

    def __init__(self, spec: ModelSpec, *, environ: Mapping[str, str] | None = None) -> None:
        """`environ` is where a live run looks for its flag: the process environment by
        default. A live run is refused here, before the run records anything, and so is a
        live branch."""
        self.spec = spec
        self._recorder: CassetteRecorder | None = None
        self._player: CassettePlayer | None = None
        self._branch: BranchPlayer | None = None
        self._live: _LiveSetup | None = None
        self.cassette: Cassette | None = None
        """The cassette a replay serves, or the one a recording made, once complete."""
        if isinstance(spec, Replay):
            self._player = CassettePlayer(spec.cassette)
            self.cassette = spec.cassette
        elif isinstance(spec, Branch):
            self._branch = BranchPlayer(spec.parent, spec.point)
            self._recorder = CassetteRecorder(self._answering(spec.model, environ))
        elif isinstance(spec, Live):
            self._recorder = CassetteRecorder(self._answering(spec, environ))
        elif spec.record:
            self._recorder = CassetteRecorder(SCRIPTED_MODEL)

    def _answering(self, spec: Scripted | Live, environ: Mapping[str, str] | None) -> ModelInfo:
        """Set up the model that answers the calls, live after `check_live`; return it as
        the cassette names it."""
        if isinstance(spec, Scripted):
            return SCRIPTED_MODEL
        meter, price = check_live(spec, os.environ if environ is None else environ)
        chat = provider_model(spec.provider, spec.model, spec.params)
        self._live = _LiveSetup(spec, chat, meter, price)
        params = plain_object(spec.params, "the model's parameters")
        return ModelInfo(adapter="live", name=spec.name, params=params)

    @property
    def adapter(self) -> Literal["scripted", "live", "replay"]:
        """The adapter, as `harness.run.model_adapter` records it: for a branch, the
        adapter of the model it switches to."""
        if self._player is not None:
            return "replay"
        return "live" if self._live is not None else "scripted"

    @property
    def name(self) -> str:
        """The model's name, as `harness.run.model` records it: a replay names the
        recorded model, and a branch the model it switches to."""
        if self._player is not None:
            return self._player.cassette.model.name
        if self._live is not None:
            return self._live.spec.name
        return SCRIPTED_MODEL.name

    @property
    def cassette_digest(self) -> str | None:
        """The digest `harness.run` records when the run starts: a replay's cassette."""
        return self._player.cassette.digest() if self._player is not None else None

    @property
    def served(self) -> int:
        """How many recorded calls a replay or a branch has answered from its cassette."""
        if self._player is not None:
            return self._player.served
        return self._branch.served if self._branch is not None else 0

    def chat_model(
        self,
        worker: str,
        *,
        scripted: Callable[[Think], BaseChatModel],
        think: Think,
        hold: Hold | None = None,
    ) -> BaseChatModel:
        """The chat model of one incarnation of `worker`.

        `scripted` builds the worker's scripted model, which takes the think it is given
        before each call; `think` takes `think_ms` of virtual time; `hold` awaits a live
        model's provider with virtual time held still (`Scheduler.hold`), which a live
        model needs. Every call takes `think` once: a branch takes it itself, before it
        decides where the call goes, so the model it switches to takes none.
        """
        if self._player is not None:
            return ReplayModel(self._player, worker=worker, think=think)
        if self._branch is not None:
            if self._recorder is None:
                raise ValueError("a branch records its cassette")
            then = self._answering_model(scripted, _no_think, hold)
            branch = BranchModel(self._branch, then, worker=worker, think=think)
            return RecordingModel(branch, self._recorder, worker=worker)
        model = self._answering_model(scripted, think, hold)
        if self._recorder is not None:
            return RecordingModel(model, self._recorder, worker=worker)
        return model

    def _answering_model(
        self, scripted: Callable[[Think], BaseChatModel], think: Think, hold: Hold | None
    ) -> BaseChatModel:
        """The model that answers the calls, taking `think` before each one: the live
        adapter, or the scripted model."""
        if self._live is None:
            return scripted(think)
        if hold is None or self._recorder is None:
            raise ValueError("a live model needs the scheduler's hold and the recorder")
        return LiveModel(
            self._live.chat,
            name=self._live.spec.name,
            price=self._live.price,
            meter=self._live.meter,
            think=think,
            hold=hold,
            limit_s=self._live.spec.limit_s,
        )

    def stopped(self, run: RunRecord) -> str | None:
        """The run has stopped, so no model call is left. A recording's cassette is then
        complete: keep it, and return its digest for `harness.run`."""
        if self._recorder is None:
            return None
        self.cassette = self._recorder.cassette(run_header(run))
        return self.cassette.digest()

    def check(self) -> None:
        """Fail the run, with `ReplayError`, if a replay did not serve exactly the
        recorded calls, or a branch did not replay its parent's first K calls (0009)."""
        if self._player is not None:
            self._player.finish()
        if self._branch is not None:
            self._branch.finish()
