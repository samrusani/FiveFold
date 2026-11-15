"""Recovery latency (0004, Metrics, as amended by 0014): for each task intent a fault
touched, from the first fault that touched it until the stack's evidence settles the
intent's outcome at the end.

It reads what the properties read, the simulator's and the harness's records and the
scenario, and never the gateway's tables. So it means the same in B0, G1 and G2.

- **Touched.** A fault touches the task intent its record names
  (`harness.fault_log.intent_id`): the intent of the message, action or tool call it
  acted on. F9 touches every task intent. F10 has a row for each status answer it
  changed, naming that answer's intent. A fault that names no intent, or an intent
  outside the task, touches none.
- **Start.** The first fault, by seq, that touched the intent: its seq and `vt`.
- **Outcome.** `committed` when the intent has an effect at the end, else
  `not_committed`.
- **End.** With `K_i(t)` as 0004's `K_i` at harness seq `t` instead of `t_r`, the first seq
  at or after the start from which `K_i(t)` equals the outcome until the run stops. For
  `committed`, that is the first commit evidence for one of the intent's effects: a write
  answer, a C1 status answer or an attestation. For `not_committed`, it is the moment the
  last of the intent's write requests, sent or possibly sent, is first shown never to
  commit, or the start when it sent none. Evidence held before the start makes the end
  the start. Evidence after the counted report counts too: the drain lets reconciliation
  settle (0009). A later duplicate does not move the end.
- **Latency.** The end's `vt` minus the start's: a delivery's `delivered_vt`, or an
  attestation's `vt`. By the wall clock, from the start fault's `wall_at` to the
  delivery's `delivered_wall_at` or the attestation's `wall_at`, and 0 when the end is
  the start; for metrics only.
- **Unresolved.** An intent with no end: no latency, counted apart, with `K_i` at the stop.

Readings the decision records leave open:

- "At harness seq `t`" counts the evidence the stack got at `t` itself, so the end is the
  seq of the delivery or attestation that settled the outcome.
- When one piece of evidence settles several requests last, the request sent first names
  the settlement. For a lost request and its rejected retry with the same key, that is
  `same_key_rejected`: how the stack learned the fate of the request the fault hit.
- A wall-clock latency is rounded to the nearest millisecond, half to even. It is the
  plain difference of two database times: negative only if the host's clock was set back
  during the run. When a wall-clock time it needs is missing, as in records built by
  hand, it is None.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction

from fivefold.evaluator.evidence import IntentKnowledge, commit_evidence, stack_knowledge
from fivefold.evaluator.records import TOUCHES_EVERY_INTENT, Fault, RecordsError, RunRecords
from fivefold.evaluator.result import Outcome, RecoveryEntry, SettledBy


@dataclass(frozen=True)
class _Event:
    """A piece of evidence as the stack got it: when, by both clocks."""

    vt: int
    wall_at: datetime | None


def recovery_entries(records: RunRecords) -> tuple[RecoveryEntry, ...]:
    """One entry per task intent a fault touched, in scenario order (0014).

    Raises `RecordsError` when the evidence that ends an intent's recovery is dated
    before the fault that started it: virtual time never goes back between two harness
    events.
    """
    starts = touched(records)
    if not starts:
        return ()
    after = _past_last_event(records)
    knowledge = stack_knowledge(records, after)
    first_commits = commit_evidence(records, after)
    events = _events(records)
    entries: list[RecoveryEntry] = []
    for intent in records.scenario.intents:
        start = starts.get(intent.id)
        if start is None:
            continue
        effects = [effect.effect_id for effect in records.effects if effect.intent_id == intent.id]
        outcome = Outcome.COMMITTED if effects else Outcome.NOT_COMMITTED
        if outcome is Outcome.COMMITTED:
            found = [first_commits[effect] for effect in effects if effect in first_commits]
            first = min(found, key=lambda evidence: evidence.seq) if found else None
            end = None if first is None else (first.seq, SettledBy(first.source))
        else:
            end = _last_settlement(knowledge[intent.id], start)
        entries.append(_entry(intent.id, start, outcome, end, knowledge[intent.id], events))
    return tuple(entries)


def touched(records: RunRecords) -> dict[str, Fault]:
    """The first fault, by seq, that touched each task intent it touched."""
    task = [intent.id for intent in records.scenario.intents]
    starts: dict[str, Fault] = {}
    for fault in sorted(records.faults, key=lambda fault: fault.seq):
        if fault.fault == TOUCHES_EVERY_INTENT:
            reached = task
        else:
            reached = [fault.intent_id] if fault.intent_id in task else []
        for intent_id in reached:
            starts.setdefault(intent_id, fault)
    return starts


def _last_settlement(known: IntentKnowledge, start: Fault) -> tuple[int, SettledBy] | None:
    """For a not_committed outcome: when the last of the intent's requests was first shown
    never to commit, and how; the start, when it sent none; None while one stays open."""
    if any(finding.settled_by is None for finding in known.requests):
        return None
    if not known.requests:
        return start.seq, SettledBy.NO_WRITE
    last = max(
        known.requests, key=lambda finding: (finding.evidence_seq or 0, -finding.request.seq)
    )
    if last.evidence_seq is None or last.settled_by is None:
        raise RecordsError(f"request {last.request.seq} is settled by no evidence")
    return last.evidence_seq, SettledBy(str(last.settled_by))


def _entry(
    intent_id: str,
    start: Fault,
    outcome: Outcome,
    end: tuple[int, SettledBy] | None,
    known: IntentKnowledge,
    events: Mapping[int, _Event],
) -> RecoveryEntry:
    entry = RecoveryEntry(
        intent_id=intent_id,
        fault_seq=start.seq,
        fault=start.fault,
        start_vt=start.vt,
        outcome=outcome,
        settled_by=None,
        end_seq=None,
        end_vt=None,
        latency_ms=None,
        latency_wall_ms=None,
        knowledge_at_stop=known.knowledge,
    )
    if end is None:
        return entry
    seq, settled_by = end
    if seq <= start.seq:
        # The stack held the evidence before the fault: the end is the start.
        return entry.model_copy(
            update={
                "settled_by": settled_by,
                "end_seq": start.seq,
                "end_vt": start.vt,
                "latency_ms": 0,
                "latency_wall_ms": 0,
                "knowledge_at_stop": None,
            }
        )
    event = events.get(seq)
    if event is None:
        raise RecordsError(f"no delivery or attestation at harness seq {seq}")
    if event.vt < start.vt:
        raise RecordsError(
            f"{intent_id}: the evidence at seq {seq} is dated vt {event.vt}, before the fault"
            f" at seq {start.seq}, vt {start.vt}"
        )
    return entry.model_copy(
        update={
            "settled_by": settled_by,
            "end_seq": seq,
            "end_vt": event.vt,
            "latency_ms": event.vt - start.vt,
            "latency_wall_ms": _wall_ms(start.wall_at, event.wall_at),
            "knowledge_at_stop": None,
        }
    )


def _events(records: RunRecords) -> dict[int, _Event]:
    """Every delivery and attestation, by the harness seq at which the stack got it."""
    events: dict[int, _Event] = {}
    for message in records.messages:
        if message.delivered_seq is not None and message.delivered_vt is not None:
            events[message.delivered_seq] = _Event(message.delivered_vt, message.delivered_wall_at)
    for action in records.actor_actions:
        events[action.seq] = _Event(action.vt, action.wall_at)
    return events


def _past_last_event(records: RunRecords) -> int:
    """A harness seq after every event of the run, so `K_i` there is `K_i` at the stop."""
    seqs = [0]
    for message in records.messages:
        seqs.append(message.seq)
        if message.delivered_seq is not None:
            seqs.append(message.delivered_seq)
    seqs.extend(fault.seq for fault in records.faults)
    seqs.extend(action.seq for action in records.actor_actions)
    seqs.extend(report.seq for report in records.reports)
    return max(seqs) + 1


def _wall_ms(start: datetime | None, end: datetime | None) -> int | None:
    """Wall-clock milliseconds from `start` to `end`, rounded half to even; None when
    either is missing."""
    if start is None or end is None:
        return None
    micros = (end - start) // timedelta(microseconds=1)
    return round(Fraction(micros, 1_000))
