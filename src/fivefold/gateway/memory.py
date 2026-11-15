"""The gateway's records for one run, held in memory (0003, testing plan).

`MemoryStore` runs the operations of `ActionStore` (`store.py`) against an immutable
snapshot. A transaction works on its own copy and replaces the store's snapshot only when
it commits, so a step that raises, or a result that breaks an invariant, leaves the store
unchanged.

It serves two purposes. The in-memory Hypothesis suite tests the steps through it, and
the Postgres suite uses it as the oracle: the same operations against both stores must
produce the same records.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace

from fivefold.domain.contracts import Contract
from fivefold.gateway.codec import plain_from_json
from fivefold.gateway.records import (
    Action,
    ActionEvent,
    ActionResult,
    Admission,
    ProposalRecord,
    ReadRecord,
    Refusal,
    RefusalReason,
    ReportAccepted,
    Snapshot,
    ToolCall,
    frozen,
)
from fivefold.gateway.state_machine import HOLDING_STATES, GatewaySettings
from fivefold.gateway.steps import ActionView, Change, proposal_id_for, read_id_for
from fivefold.gateway.store import (
    ActionStore,
    Transaction,
    UnknownAction,
    in_action_order,
    view_of,
)

__all__ = ["MemoryStore", "UnknownAction"]


class MemoryStore(ActionStore):
    """One run's gateway records, in memory."""

    def __init__(
        self,
        *,
        task_id: str,
        intents: Mapping[str, str],
        contract: Contract,
        settings: GatewaySettings,
        identity: bool = True,
    ) -> None:
        super().__init__(task_id=task_id, contract=contract, settings=settings, identity=identity)
        self._snapshot = Snapshot(
            intents=frozen(intents),
            actions=frozen({}),
            grants=frozen({}),
            attempts=frozen({}),
            events=(),
            escalations=frozen({}),
            tool_calls=(),
            run_closed=False,
            closed_intents=frozenset(),
            reports=frozen({}),
        )

    def snapshot(self) -> Snapshot:
        """Every record as it is now. Snapshots are immutable and never change."""
        return self._snapshot

    @contextmanager
    def _transaction(self) -> Iterator[Transaction]:
        tx = _MemoryTransaction(self._snapshot)
        yield tx
        self._snapshot = tx.snapshot()

    @contextmanager
    def _reading(self) -> Iterator[Transaction]:
        yield _MemoryTransaction(self._snapshot)


class _MemoryTransaction(Transaction):
    """A working copy of the snapshot. Locks are not needed: one thread uses it."""

    def __init__(self, snapshot: Snapshot) -> None:
        self._working = snapshot

    def intents(self) -> Mapping[str, str]:
        return self._working.intents

    def run_closed(self) -> bool:
        return self._working.run_closed

    def close_run(self) -> None:
        self._working = replace(self._working, run_closed=True)

    def closed_intents(self) -> frozenset[str]:
        return self._working.closed_intents

    def close_intent(self, intent_id: str) -> None:
        closed = self._working.closed_intents | {intent_id}
        self._working = replace(self._working, closed_intents=closed)

    def reports(self) -> Mapping[str, int]:
        return self._working.reports

    def record_report(self, worker: str, call_no: int) -> None:
        reports = {**self._working.reports, worker: call_no}
        self._working = replace(self._working, reports=frozen(reports))

    def lock_intent(self, intent_id: str) -> None:
        pass

    def lock_intents(self) -> None:
        pass

    def view(self, action_id: str) -> ActionView:
        return view_of(self._working, action_id)

    def action_ids(self) -> list[str]:
        return in_action_order(self._working.actions)

    def holders(self, intent_id: str) -> list[Action]:
        return [
            action
            for action in self._working.actions.values()
            if action.identity and action.intent_id == intent_id and action.state in HOLDING_STATES
        ]

    def next_action_number(self) -> int:
        return len(self._working.actions) + 1

    def record_read(
        self, key: str, version: int, data: Mapping[str, object], vt: int
    ) -> ReadRecord:
        snapshot = self._working
        number = len(snapshot.reads) + 1
        read = ReadRecord(read_id_for(number), key, version, _object(plain_from_json(data)), vt)
        self._working = replace(snapshot, reads=frozen({**snapshot.reads, read.read_id: read}))
        return read

    def read_of(self, key: str, version: int) -> ReadRecord | None:
        found = [r for r in self._working.reads.values() if (r.key, r.version) == (key, version)]
        return min(found, key=lambda r: int(r.read_id[2:])) if found else None

    def reads(self, read_ids: Iterable[str]) -> Mapping[str, ReadRecord]:
        known = self._working.reads
        return frozen({r: known[r] for r in read_ids if r in known})

    def record_proposal(
        self,
        *,
        operation: str,
        intent_id: str | None,
        args: Mapping[str, object],
        based_on: tuple[str, ...],
        admission: Admission,
        reason: RefusalReason | None,
        action_id: str | None,
        vt: int,
    ) -> str:
        snapshot = self._working
        proposal_id = proposal_id_for(len(snapshot.proposals) + 1)
        record = ProposalRecord(
            proposal_id,
            operation,
            intent_id,
            _object(plain_from_json(args)),
            based_on,
            admission,
            reason,
            action_id,
            vt,
        )
        self._working = replace(snapshot, proposals=(*snapshot.proposals, record))
        return proposal_id

    def apply(self, change: Change, vt: int) -> None:
        snapshot = self._working
        action = change.action
        actions = dict(snapshot.actions)
        actions[action.action_id] = action
        grants = dict(snapshot.grants)
        if change.grant is not None:
            grants[change.grant.grant_id] = change.grant
        attempts = dict(snapshot.attempts)
        if change.attempt is not None:
            attempt = change.attempt
            kept = [
                t for t in attempts.get(action.action_id, ()) if t.attempt_no != attempt.attempt_no
            ]
            attempts[action.action_id] = tuple(sorted([*kept, attempt], key=lambda t: t.attempt_no))
        escalations = dict(snapshot.escalations)
        if change.escalation is not None:
            escalations[change.escalation.escalation_id] = change.escalation
        seq = _last_seq(snapshot)
        events = list(snapshot.events)
        for new in change.events:
            seq += 1
            events.append(
                ActionEvent(
                    seq,
                    action.action_id,
                    new.kind,
                    new.from_state,
                    new.to_state,
                    new.reason,
                    new.details,
                    vt,
                )
            )
        self._working = replace(
            snapshot,
            actions=frozen(actions),
            grants=frozen(grants),
            attempts=frozen(attempts),
            events=tuple(events),
            escalations=frozen(escalations),
        )

    def log(
        self,
        tool: str,
        action_id: str | None,
        result: ActionResult | Refusal | ReportAccepted,
        vt: int,
    ) -> int:
        snapshot = self._working
        call_no = len(snapshot.tool_calls) + 1
        call = ToolCall(call_no, tool, action_id, result, _last_seq(snapshot), vt)
        self._working = replace(snapshot, tool_calls=(*snapshot.tool_calls, call))
        return call_no

    def snapshot(self) -> Snapshot:
        return self._working


def _last_seq(snapshot: Snapshot) -> int:
    return snapshot.events[-1].seq if snapshot.events else 0


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("a write call's arguments are a JSON object")
    return value
