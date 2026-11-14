"""The gateway's operations, written once for every store (0001, 0002, 0003).

`ActionStore` runs each gateway operation as one transaction: it reads the records it
needs through a `Transaction`, asks a step function in `steps.py` what to change, writes
the change, checks every invariant on the result, and only then commits. A step that
raises, or a result that breaks an invariant, rolls the transaction back.

Two stores implement `Transaction`: `MemoryStore` (`memory.py`), which keeps records in
memory, and `PgStore` (`pg_store.py`), which keeps them in the `gw` schema. Because the
operations live here, the two stores differ only in how they read and write records, and
the in-memory store can serve as the oracle for the Postgres one.

The store also logs every response the agent receives, as the `tool_call` table does, so
I5 can be checked. It does not wait: `tool_wait_ms` and `get_action_status(wait_ms)`
belong to the tool layer, which calls `status` when its wait ends, with the name of the
tool that answers. A refused proposal is answered at once, so `propose` logs refusals; an
admitted one is answered later, by that `status` call.

Locks, for the Postgres store: a transaction takes intent rows in intent-id order, then
action rows in action-id order, then the run's state row, which numbers actions, events
and responses and guards the invariant check's reads. Every transaction takes the locks
it needs in that order, and its foreign key checks lock only rows it already holds, so
no two transactions can deadlock. A proposal locks its intent before it reads whether
the run is closed, and closing the run locks every intent first, so a proposal either
lands before the close, which then rejects it, or sees the run closed. A final report
locks every intent the same way, because an accepted report can close intents and the
run (0010). A store is used from one thread at a time; a gateway with several workers
gives each its own store.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager

from fivefold.domain.contracts import Contract
from fivefold.gateway import steps
from fivefold.gateway.invariants import InvariantViolation, check_invariants
from fivefold.gateway.records import (
    Action,
    ActionResult,
    Answer,
    ApprovalStatus,
    Attempt,
    BasisCheck,
    CheckRecord,
    CheckTicket,
    Claim,
    ProbeAnswer,
    Proposal,
    Refusal,
    RefusalReason,
    ReportAccepted,
    Resolution,
    Snapshot,
    check_text,
)
from fivefold.gateway.state_machine import GatewaySettings
from fivefold.gateway.steps import ActionView, Change, Match

StepFn = Callable[[ActionView], Change | None]


class UnknownAction(LookupError):
    """No action has this id in the run."""


class Transaction(ABC):
    """One transaction against a store's records. Writes are visible to later reads."""

    @abstractmethod
    def intents(self) -> Mapping[str, str]:
        """The task's intents: intent id to operation."""

    @abstractmethod
    def run_closed(self) -> bool:
        """Whether the run is closed to new proposals."""

    @abstractmethod
    def lock_intent(self, intent_id: str) -> None:
        """Lock one intent's row until the transaction ends: the anchor for proposals."""

    @abstractmethod
    def lock_intents(self) -> None:
        """Lock every intent's row, in intent-id order, until the transaction ends."""

    @abstractmethod
    def close_run(self) -> None:
        """Mark the run closed."""

    @abstractmethod
    def closed_intents(self) -> frozenset[str]:
        """The intents an accepted `not_done` claim closed (0010)."""

    @abstractmethod
    def close_intent(self, intent_id: str) -> None:
        """Mark one intent closed. Call it with the intent locked."""

    @abstractmethod
    def reports(self) -> Mapping[str, int]:
        """Each worker whose report was accepted, with that report's call number."""

    @abstractmethod
    def record_report(self, worker: str, call_no: int) -> None:
        """Record a worker's first accepted report, logged as call `call_no`."""

    @abstractmethod
    def view(self, action_id: str) -> ActionView:
        """One action's records. In a writing transaction, this locks the action's row.

        The lock holds until the transaction ends. The action's grant, attempts and
        escalation need no lock of their own: only a holder of the action's row changes
        them.

        Raises `UnknownAction` if there is no such action.
        """

    @abstractmethod
    def action_ids(self) -> list[str]:
        """Every action id in action-id order. In a writing transaction, all rows are locked."""

    @abstractmethod
    def holders(self, intent_id: str) -> list[Action]:
        """The intent's live or Confirmed actions with identity on, with their rows locked.

        Call it with the intent locked, so no new holder can appear.
        """

    @abstractmethod
    def next_action_number(self) -> int:
        """Reserve the next action's number in the run."""

    @abstractmethod
    def apply(self, change: Change, vt: int) -> None:
        """Write a step's change and append its events at `vt`."""

    @abstractmethod
    def log(
        self,
        tool: str,
        action_id: str | None,
        result: ActionResult | Refusal | ReportAccepted,
        vt: int,
    ) -> int:
        """Log a response the agent receives, with the event seq it reflects.

        Returns the response's call number.
        """

    @abstractmethod
    def snapshot(self) -> Snapshot:
        """Every record as this transaction sees it, in one consistent state."""


class ActionStore(ABC):
    """One run's gateway records, changed only through the operations below.

    `identity` is on for G2. With it off (G1), every proposal creates a new action
    (0008); the other G1 differences belong to the gateway, not to the records.
    """

    def __init__(
        self,
        *,
        task_id: str,
        contract: Contract,
        settings: GatewaySettings,
        identity: bool = True,
    ) -> None:
        self._task_id = task_id
        self._contract = contract
        self._settings = settings
        self._identity = identity
        self._vt = 0

    @abstractmethod
    def _transaction(self) -> AbstractContextManager[Transaction]:
        """A transaction that commits when the block ends normally, else rolls back."""

    @abstractmethod
    def _reading(self) -> AbstractContextManager[Transaction]:
        """A transaction for reads only, which takes no locks and writes nothing."""

    # Reading ---------------------------------------------------------------------------

    @property
    def vt(self) -> int:
        """The virtual time of the latest operation."""
        return self._vt

    @property
    def settings(self) -> GatewaySettings:
        """The settings every step runs with."""
        return self._settings

    def snapshot(self) -> Snapshot:
        """Every record as it is now."""
        with self._reading() as tx:
            return tx.snapshot()

    def view(self, action_id: str) -> ActionView:
        """One action's records, as steps read them."""
        with self._reading() as tx:
            return tx.view(action_id)

    def begin_check(self, action_id: str) -> CheckTicket:
        """What the dispatcher must check before the next send (0003, dispatch).

        It reads without a lock; the send commits only if no event came since.
        """
        return steps.begin_check(self.view(action_id), self._settings)

    # Tools ------------------------------------------------------------------------------

    def propose(self, proposal: Proposal, vt: int) -> ActionResult | Refusal:
        """A write tool call: admission's intent check, then the matching rule (0001).

        Returns the action's result as admission left it, or a refusal. Only a refusal is
        logged as the tool's response; see the module docstring.
        """
        self._clock(vt)
        with self._transaction() as tx:
            outcome, action_id = self._propose(tx, proposal, vt)
            if isinstance(outcome, Refusal):
                tx.log(proposal.operation, action_id, outcome, vt)
            self._check(tx)
        self._vt = vt
        return outcome

    def _propose(
        self, tx: Transaction, proposal: Proposal, vt: int
    ) -> tuple[ActionResult | Refusal, str | None]:
        operation = tx.intents().get(proposal.intent_id)
        if operation is not None:
            tx.lock_intent(proposal.intent_id)
        if tx.run_closed() or proposal.intent_id in tx.closed_intents():
            return Refusal(RefusalReason.RUN_CLOSED), None
        if operation is None:
            return Refusal(RefusalReason.UNKNOWN_INTENT), None
        if operation != proposal.operation:
            return Refusal(RefusalReason.WRONG_OPERATION), None
        holders = tx.holders(proposal.intent_id) if self._identity else []
        kind, holder = steps.match_proposal(holders, proposal)
        if holder is None:
            action_id = steps.action_id_for(tx.next_action_number())
            admitted = steps.admit(
                action_id, self._task_id, proposal, self._contract, identity=self._identity, vt=vt
            )
            tx.apply(admitted, vt)
            return steps.result_for(tx.view(action_id)), action_id
        action_id = holder.action_id
        if kind is Match.ATTACH:
            change = steps.attach(tx.view(action_id), proposal)
            if change is not None:
                tx.apply(change, vt)
            return steps.result_for(tx.view(action_id)), action_id
        if kind is Match.BUSY:
            refusal = Refusal(RefusalReason.INTENT_BUSY, action_id, holder.state, args=holder.args)
        else:
            refusal = Refusal(RefusalReason.INTENT_DONE, action_id, holder.state, holder.effect_id)
        return refusal, action_id

    def status(self, action_id: str, vt: int, *, tool: str = "get_action_status") -> ActionResult:
        """The action's result now, as `tool` answers once its wait ends (0003); logged."""
        self._clock(vt)
        with self._transaction() as tx:
            result = steps.result_for(tx.view(action_id))
            tx.log(tool, action_id, result, vt)
            self._check(tx)
        self._vt = vt
        return result

    def submit_report(
        self, claims: Sequence[Claim], vt: int, *, worker: str
    ) -> ReportAccepted | Refusal:
        """Check a worker's final report against the records (0003); refuse contradictions.

        An accepted report closes, in the same transaction, each intent it claims
        `not_done`, and the whole run if it is the last report the run expects (0010,
        item 6). It locks every intent first, as closing the run does.

        `worker` names the agent worker the report comes from: the name the harness gave
        the worker, which its connection carries and which stays the same when the
        worker resumes from its checkpoint (0009). Only G2 checks reports (0008), so a
        store with action identity off refuses this call.
        """
        if not self._identity:
            raise ValueError("G1 has no report check and no run closing (0008)")
        if not worker:
            raise ValueError("a report names the worker it comes from")
        check_text(worker, "worker")
        self._clock(vt)
        with self._transaction() as tx:
            tx.lock_intents()
            by_intent: dict[str, list[ActionView]] = {}
            for action_id in tx.action_ids():
                view = tx.view(action_id)
                by_intent.setdefault(view.action.intent_id, []).append(view)
            conflicts = steps.report_conflicts(claims, list(tx.intents()), by_intent)
            outcome: ReportAccepted | Refusal
            if conflicts:
                outcome = Refusal(RefusalReason.REPORT_INCONSISTENT, conflicts=conflicts)
                tx.log("submit_final_report", None, outcome, vt)
            else:
                outcome = ReportAccepted(tuple(claims))
                reported = tx.reports()
                intents, last = steps.report_closes(claims, reported, worker, self._settings)
                self._close(tx, intents, whole_run=last, vt=vt)
                call_no = tx.log("submit_final_report", None, outcome, vt)
                if worker not in reported:
                    tx.record_report(worker, call_no)
            self._check(tx)
        self._vt = vt
        return outcome

    # Gateway steps ----------------------------------------------------------------------

    def file_approval(
        self,
        action_id: str,
        basis: BasisCheck,
        approval_id: str | None,
        vt: int,
        *,
        seen_seq: int,
    ) -> None:
        """T2 or T3 (0002, 0003). `seen_seq`: the action's latest event when the check began."""
        self._step(
            action_id,
            vt,
            lambda v: steps.file_approval(v, basis, approval_id, vt, seen_seq=seen_seq),
        )

    def poll_approval(
        self,
        action_id: str,
        approval: ApprovalStatus,
        basis: BasisCheck | None,
        vt: int,
        *,
        seen_seq: int,
    ) -> None:
        """T4 or T5, or nothing while the request is pending and the basis holds."""
        self._step(
            action_id,
            vt,
            lambda v: steps.poll_approval(v, approval, basis, vt, seen_seq=seen_seq),
        )

    def time_out_approval(
        self, action_id: str, approval: ApprovalStatus, vt: int, *, seen_seq: int
    ) -> None:
        """T5 with reason approval_timeout, after a final poll that still reads pending."""
        self._step(
            action_id,
            vt,
            lambda v: steps.time_out_approval(v, approval, vt, self._settings, seen_seq=seen_seq),
        )

    def dispatch(self, action_id: str, check: CheckRecord, vt: int) -> Attempt | None:
        """T6 or T7. Returns the attempt to send, or None if nothing may be sent."""
        change = self._step(action_id, vt, lambda v: steps.dispatch(v, check, vt, self._settings))
        return change.attempt if change is not None else None

    def retry(self, action_id: str, check: CheckRecord, vt: int) -> Attempt | None:
        """T13 or an escalation. Returns the attempt to send, or None if nothing may be sent."""
        change = self._step(action_id, vt, lambda v: steps.retry(v, check, vt, self._settings))
        return change.attempt if change is not None else None

    def answer(self, action_id: str, attempt_no: int, answer: Answer, vt: int) -> None:
        """T8, T9 or T10."""
        self._step(action_id, vt, lambda v: steps.answer(v, attempt_no, answer, vt, self._settings))

    def probe(self, action_id: str, result: ProbeAnswer, vt: int) -> None:
        """T11, T12 or a probe note, at C1."""
        self._step(action_id, vt, lambda v: steps.probe(v, result, vt, self._settings))

    def attest(
        self, action_id: str, resolution: Resolution, effect_id: str | None, actor: str, vt: int
    ) -> None:
        """T11 or T12 with reason human_attested, through the admin API."""
        self._step(
            action_id,
            vt,
            lambda v: steps.attest(v, resolution, effect_id, actor, vt, self._settings),
        )

    def restart(self, vt: int) -> None:
        """Startup recovery in action-id order, before any tool is served (0003)."""
        self._clock(vt)
        with self._transaction() as tx:
            for action_id in tx.action_ids():
                change = steps.recover(tx.view(action_id), vt, self._settings)
                if change is not None:
                    tx.apply(change, vt)
            self._check(tx)
        self._vt = vt

    def close_run(self, vt: int) -> None:
        """Refuse new proposals and reject every action not yet dispatched (0003)."""
        self._clock(vt)
        with self._transaction() as tx:
            tx.lock_intents()
            self._close(tx, frozenset(), whole_run=True, vt=vt)
            self._check(tx)
        self._vt = vt

    # Internals --------------------------------------------------------------------------

    def _close(self, tx: Transaction, intents: frozenset[str], *, whole_run: bool, vt: int) -> None:
        """Close `intents`, or the whole run: reject their undispatched actions (0003, 0010).

        The caller holds every intent's lock, so no proposal for them can land meanwhile.
        """
        for action_id in tx.action_ids():
            view = tx.view(action_id)
            if whole_run or view.action.intent_id in intents:
                change = steps.close(view, vt)
                if change is not None:
                    tx.apply(change, vt)
        for intent_id in sorted(intents - tx.closed_intents()):
            tx.close_intent(intent_id)
        if whole_run:
            tx.close_run()

    def _clock(self, vt: int) -> None:
        if vt < self._vt:
            raise ValueError(f"virtual time went backwards: {vt} after {self._vt}")

    def _step(self, action_id: str, vt: int, step: StepFn) -> Change | None:
        self._clock(vt)
        with self._transaction() as tx:
            change = step(tx.view(action_id))
            if change is not None:
                tx.apply(change, vt)
                self._check(tx)
        self._vt = vt
        return change

    def _check(self, tx: Transaction) -> None:
        violations = check_invariants(tx.snapshot(), self._settings)
        if violations:
            raise InvariantViolation(violations)


def view_of(snapshot: Snapshot, action_id: str) -> ActionView:
    """One action's records, read from a snapshot."""
    action = snapshot.actions.get(action_id)
    if action is None:
        raise UnknownAction(action_id)
    grant = snapshot.grants.get(action.grant_id) if action.grant_id is not None else None
    return ActionView(
        action=action,
        grant=grant,
        attempts=snapshot.attempts.get(action_id, ()),
        escalation=snapshot.escalations.get(steps.escalation_id_for(action_id)),
        events=tuple(e for e in snapshot.events if e.action_id == action_id),
    )


def in_action_order(action_ids: Iterable[str]) -> list[str]:
    """Action ids in the order their numbers were given."""
    return sorted(action_ids, key=steps.action_number)
