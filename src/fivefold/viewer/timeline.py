"""A run's timeline: the harness's events and the gateway's action events, merged (0012).

The two sources number their events with separate counters. The harness numbers its
messages, faults, scripted humans' actions and reports with one counter (0004); the
gateway numbers its action events with `gw.run_state.last_seq` (0003). Neither counter
says how the other's events fall, so the timeline follows record 0012, decision 1:

- events go in order of virtual time;
- within one virtual time, harness events come before gateway events, and each source's
  events keep the order of their own `seq`;
- every event that shares its virtual time with another is marked, because at an equal
  virtual time the rule above does not say which happened first.

A message is one event, at its send: its `seq` and `sent_vt`. Its delivery or drop is
part of the message, as its fate (0004), not an event of its own.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

Source = Literal["harness", "gateway"]
Kind = Literal["message", "fault", "actor", "report", "action_event"]

SOURCE_ORDER: Final[Mapping[Source, int]] = {"harness": 0, "gateway": 1}
"""At an equal virtual time, harness events come before gateway events (0012)."""


@dataclass(frozen=True)
class Event:
    """One event of one source: its own `seq`, its virtual time, and what it records."""

    source: Source
    seq: int
    vt: int
    kind: Kind
    record: Mapping[str, object]


def merge(events: Iterable[Event]) -> list[dict[str, object]]:
    """The timeline of `events`, from both sources, as JSON-ready dicts.

    Each dict has `source`, `seq`, `vt`, `kind`, `shares_vt` (whether another event has
    the same virtual time) and the event's record under the name of its kind.
    """
    ordered = sorted(events, key=lambda event: (event.vt, SOURCE_ORDER[event.source], event.seq))
    per_vt = Counter(event.vt for event in ordered)
    return [
        {
            "source": event.source,
            "seq": event.seq,
            "vt": event.vt,
            "kind": event.kind,
            "shares_vt": per_vt[event.vt] > 1,
            event.kind: dict(event.record),
        }
        for event in ordered
    ]
