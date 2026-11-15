"""The run's model adapter (0009): the chat model each agent worker gets.

`RunSpec.model` chooses one:

- `Scripted()`: the scripted adapter, the policy of 0008 behind a chat model.
- `Scripted(record=True)`: the same, wrapped by the recorder, so every request and response
  goes into a cassette (`fivefold.recorder.recording`).
- `Replay(cassette)`: the replay adapter, which serves the cassette's responses in order
  (`fivefold.agents.replay`).
- `Live(provider, model, params, spend)`: the live adapter, a provider's chat model
  (`fivefold.agents.live`), always wrapped by the recorder (0009).

`harness.run` records the adapter (`scripted`, `live` or `replay`), the model's name, and
the cassette's digest: for a replay, the cassette it serves, from the start; for a
recording, the cassette it made, once the run has stopped and the cassette is complete. A
recording run and its replays therefore name the same digest. A live model's name says the
provider and the model, such as `ollama:llama3.1:8b`, and its replays name it too.

Live runs sit behind an explicit flag and a spend cap (CLAUDE.md). A live run is refused,
before anything runs, unless the environment sets `FIVEFOLD_LIVE=1`, the run has a spend
cap in USD with a price file (`Live.spend`), and the price file prices the model, so the
cap can always be enforced (`check_live`).
"""

from __future__ import annotations

import math
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import JsonValue

from fivefold.agents.live import (
    CALL_LIMIT_S,
    Hold,
    LiveModel,
    SpendMeter,
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


ModelSpec = Scripted | Replay | Live
"""Which model adapter a run uses."""

SCRIPTED: Final = Scripted()
"""The scripted adapter, without recording: a run's default."""


class LiveRefused(ValueError):
    """A live run that must not start: the flag, the spend cap or the model's price is
    missing."""


def check_live_flag(environ: Mapping[str, str]) -> None:
    """Refuse any live run unless `environ` sets `FIVEFOLD_LIVE=1`."""
    if environ.get(LIVE_FLAG) != "1":
        raise LiveRefused(
            f"live runs are off: set {LIVE_FLAG}=1 to allow one, with a spend cap and a price file"
        )


def check_live(spec: Live, environ: Mapping[str, str]) -> tuple[SpendMeter, ModelPrice]:
    """Refuse a live run unless `environ` sets `FIVEFOLD_LIVE=1` and the run has a spend
    cap with a price file that prices its model; return the meter and the price."""
    check_live_flag(environ)
    if spec.spend is None:
        raise LiveRefused("a live run needs a spend cap in USD and a price file")
    if not (math.isfinite(spec.limit_s) and spec.limit_s > 0):
        raise LiveRefused(f"a live call's time limit must be above 0 seconds, not {spec.limit_s}")
    return spec.spend, spec.spend.prices.price(spec.provider, spec.model)


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
        default. A live run is refused here, before the run records anything."""
        self.spec = spec
        self._recorder: CassetteRecorder | None = None
        self._player: CassettePlayer | None = None
        self._live: _LiveSetup | None = None
        self.cassette: Cassette | None = None
        """The cassette a replay serves, or the one a recording made, once complete."""
        if isinstance(spec, Replay):
            self._player = CassettePlayer(spec.cassette)
            self.cassette = spec.cassette
        elif isinstance(spec, Live):
            meter, price = check_live(spec, os.environ if environ is None else environ)
            chat = provider_model(spec.provider, spec.model, spec.params)
            self._live = _LiveSetup(spec, chat, meter, price)
            params = plain_object(spec.params, "the model's parameters")
            self._recorder = CassetteRecorder(
                ModelInfo(adapter="live", name=spec.name, params=params)
            )
        elif spec.record:
            self._recorder = CassetteRecorder(SCRIPTED_MODEL)

    @property
    def adapter(self) -> Literal["scripted", "live", "replay"]:
        """The adapter, as `harness.run.model_adapter` records it."""
        if self._player is not None:
            return "replay"
        return "live" if self._live is not None else "scripted"

    @property
    def name(self) -> str:
        """The model's name, as `harness.run.model` records it: a replay names the
        recorded model."""
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
        """How many recorded calls a replay has answered."""
        return self._player.served if self._player is not None else 0

    def chat_model(
        self,
        worker: str,
        *,
        scripted: Callable[[], BaseChatModel],
        think: Callable[[], Awaitable[None]],
        hold: Hold | None = None,
    ) -> BaseChatModel:
        """The chat model of one incarnation of `worker`.

        `scripted` builds the worker's scripted model, which takes `think_ms` itself;
        `think` takes `think_ms` of virtual time, for an adapter that has no model to;
        `hold` awaits a live model's provider with virtual time held still
        (`Scheduler.hold`), which a live model needs.
        """
        if self._player is not None:
            return ReplayModel(self._player, worker=worker, think=think)
        if self._live is not None:
            if hold is None or self._recorder is None:
                raise ValueError("a live model needs the scheduler's hold and the recorder")
            live = LiveModel(
                self._live.chat,
                name=self._live.spec.name,
                price=self._live.price,
                meter=self._live.meter,
                think=think,
                hold=hold,
                limit_s=self._live.spec.limit_s,
            )
            return RecordingModel(live, self._recorder, worker=worker)
        model = scripted()
        if self._recorder is not None:
            return RecordingModel(model, self._recorder, worker=worker)
        return model

    def stopped(self, run: RunRecord) -> str | None:
        """The run has stopped, so no model call is left. A recording's cassette is then
        complete: keep it, and return its digest for `harness.run`."""
        if self._recorder is None:
            return None
        self.cassette = self._recorder.cassette(run_header(run))
        return self.cassette.digest()

    def check(self) -> None:
        """Fail the run, with `ReplayError`, if a replay did not serve exactly the
        recorded calls (0009)."""
        if self._player is not None:
            self._player.finish()
