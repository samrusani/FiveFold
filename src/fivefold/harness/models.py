"""The run's model adapter (0009): the chat model each agent worker gets.

`RunSpec.model` chooses one:

- `Scripted()`: the scripted adapter, the policy of 0008 behind a chat model.
- `Scripted(record=True)`: the same, wrapped by the recorder, so every request and response
  goes into a cassette (`fivefold.recorder.recording`).
- `Replay(cassette)`: the replay adapter, which serves the cassette's responses in order
  (`fivefold.agents.replay`).

`harness.run` records the adapter (`scripted`, or `replay`), the model's name, and the
cassette's digest: for a replay, the cassette it serves, from the start; for a recording,
the cassette it made, once the run has stopped and the cassette is complete. A recording
run and its replays therefore name the same digest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal

from langchain_core.language_models.chat_models import BaseChatModel

from fivefold.agents.replay import CassettePlayer, ReplayModel
from fivefold.harness.log import RunRecord
from fivefold.recorder.cassette import Cassette, ModelInfo, RunHeader, plain_object
from fivefold.recorder.recording import CassetteRecorder, RecordingModel

SCRIPTED_MODEL: Final = ModelInfo(adapter="scripted", name="scripted")
"""The scripted adapter's model, as `harness.run` and a cassette name it."""


@dataclass(frozen=True)
class Scripted:
    """The scripted adapter (0009); with `record`, wrapped by the recorder."""

    record: bool = False


@dataclass(frozen=True)
class Replay:
    """The replay adapter (0009), serving `cassette`'s responses in order."""

    cassette: Cassette


ModelSpec = Scripted | Replay
"""Which model adapter a run uses."""

SCRIPTED: Final = Scripted()
"""The scripted adapter, without recording: a run's default."""


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


class RunModels:
    """The chat models of one run's agent workers, and the run's cassette."""

    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec
        self._recorder: CassetteRecorder | None = None
        self._player: CassettePlayer | None = None
        self.cassette: Cassette | None = None
        """The cassette a replay serves, or the one a recording made, once complete."""
        if isinstance(spec, Replay):
            self._player = CassettePlayer(spec.cassette)
            self.cassette = spec.cassette
        elif spec.record:
            self._recorder = CassetteRecorder(SCRIPTED_MODEL)

    @property
    def adapter(self) -> Literal["scripted", "replay"]:
        """The adapter, as `harness.run.model_adapter` records it."""
        return "replay" if self._player is not None else "scripted"

    @property
    def name(self) -> str:
        """The model's name, as `harness.run.model` records it: a replay names the
        recorded model."""
        if self._player is not None:
            return self._player.cassette.model.name
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
    ) -> BaseChatModel:
        """The chat model of one incarnation of `worker`.

        `scripted` builds the worker's scripted model, which takes `think_ms` itself;
        `think` takes `think_ms` of virtual time, for an adapter that has no model to.
        """
        if self._player is not None:
            return ReplayModel(self._player, worker=worker, think=think)
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
