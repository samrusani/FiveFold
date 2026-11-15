"""The gateway's dispatcher: it moves each live action through its states (0002, 0003, 0005).

One driver task per live action runs the steps that need the world: the basis check and
the approval request (Proposed), approval polls and the approval timeout
(PendingApproval), the dispatch check and the first send (Authorized), and recovery
after a non-definitive answer (OutcomeUnknown): retries at C2 and C3, status probes at
C1, nothing at C0, where the action is escalated at once. Escalation ends automatic
recovery (0010, reading 7); only an attestation through the admin API moves the action.

The dispatcher follows the dispatch rule of 0003: it reads the world without holding any
lock, then the store commits a step only if no event came since the check began. A step
the store refuses, because the action moved meanwhile, commits nothing, and the driver
reads the action again. No network call happens inside a store transaction.

Drivers run as tasks of the gateway component, so a gateway crash stops them all. After
the restart, `start` runs startup recovery (0003) and starts a driver for every live
action again.

A G1 action (identity off) takes the same path with fewer steps (0008). It reads no fact:
it files its approval request at once, an approval authorizes it, and its one check
before the send asks only for the approval. Once its outcome is unknown, its driver
stops: G1 has no status probes, no retries and no escalation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from fivefold.domain.contracts import Capability
from fivefold.faults.plan import FaultInjector, Fired
from fivefold.gateway.destination import Destination, FactNow, Unreachable
from fivefold.gateway.records import (
    Action,
    ApprovalStatus,
    Attempt,
    BasisCheck,
    CheckRecord,
)
from fivefold.gateway.state_machine import FINAL_STATES, ActionState, CheckKind, Reason
from fivefold.gateway.store import ActionStore
from fivefold.harness.scheduler import Scheduler, SchedulerTimeout

_S = ActionState

_DEAD_AUTHORITY = frozenset({ApprovalStatus.DENIED, ApprovalStatus.REVOKED, ApprovalStatus.EXPIRED})
"""Approval answers that reject a pending action whatever its basis (0002, 0010)."""


FaultHandler = Callable[[Fired, str, Mapping[str, object]], None]
"""Applies a fault that fired at a dispatch point, given what it acts on."""


class Freshness(Protocol):
    """Checks an action's basis against facts now (0004, dependency rules)."""

    def keys(self, action: Action) -> Sequence[str]:
        """The fact keys of the action's dependencies."""

    def check(self, action: Action, now: Mapping[str, FactNow], *, full: bool) -> BasisCheck:
        """The basis check; `full` adds the sufficient rules to the exact ones (I3)."""


@dataclass(frozen=True)
class Timing:
    """The dispatcher's intervals, in virtual ms (0005)."""

    approval_poll_ms: int = 500
    probe_interval_ms: int = 1_000
    retry_check_after_ms: int = 500
    """How long to wait before trying a check again when the world did not answer."""


class Dispatcher:
    """Drives the gateway's live actions on the scheduler."""

    def __init__(
        self,
        scheduler: Scheduler,
        store: ActionStore,
        destination: Destination,
        freshness: Freshness,
        *,
        component: str = "gateway",
        timing: Timing | None = None,
        faults: FaultInjector | None = None,
        on_fault: FaultHandler | None = None,
        on_escalated: Callable[[str], None] | None = None,
    ) -> None:
        """`on_escalated` is called with an action's id when its driver stops because the
        action is escalated: only an attestation through the admin path can move it now
        (0010, reading 7). After a restart, it is called again for every such action."""
        self._scheduler = scheduler
        self._store = store
        self._destination = destination
        self._freshness = freshness
        self._component = component
        self._timing = timing if timing is not None else Timing()
        self._faults = faults if faults is not None else FaultInjector()
        self._on_fault = on_fault
        self._on_escalated = on_escalated
        self._driving: set[str] = set()
        self._changed: dict[str, asyncio.Event] = {}

    # Starting -----------------------------------------------------------------------------

    def start(self) -> None:
        """Startup recovery, then a driver for every live action (0003).

        Call it when the gateway starts and after every restart, before tools are served.
        """
        self._driving.clear()
        self._store.restart(self._scheduler.now)
        snapshot = self._store.snapshot()
        for action_id in sorted(snapshot.actions, key=_action_number):
            if snapshot.actions[action_id].state not in FINAL_STATES:
                self.drive(action_id)

    def drive(self, action_id: str) -> None:
        """Start the action's driver, unless one is running."""
        if action_id in self._driving:
            return
        self._driving.add(action_id)
        self._scheduler.spawn(self._component, self._drive(action_id), name=f"drive {action_id}")

    # Waiting for a state ------------------------------------------------------------------

    async def wait(self, action_id: str, until: Callable[[ActionState], bool], ms: int) -> None:
        """Wait at most `ms` virtual ms for the action to reach a state `until` accepts."""
        if until(self._store.view(action_id).action.state) or ms <= 0:
            return
        deadline = self._scheduler.now + ms
        while self._scheduler.now < deadline:
            event = self._changed.setdefault(action_id, asyncio.Event())
            event.clear()
            try:
                await self._scheduler.wait_for(event.wait(), deadline - self._scheduler.now)
            except SchedulerTimeout:
                return
            if until(self._store.view(action_id).action.state):
                return

    def wake(self, action_id: str) -> None:
        """End every `wait` on the action now, so that each sees its state at once.

        The admin path calls it after an attestation, a step no driver runs (0003).
        """
        self._notify(action_id)

    def _notify(self, action_id: str) -> None:
        event = self._changed.get(action_id)
        if event is not None:
            event.set()

    # The driver ---------------------------------------------------------------------------

    async def _drive(self, action_id: str) -> None:
        try:
            while True:
                view = self._store.view(action_id)
                state = view.action.state
                if state in FINAL_STATES:
                    return
                if state is _S.PROPOSED:
                    await self._proposed(view.action)
                elif state is _S.PENDING_APPROVAL:
                    await self._pending(view.action)
                elif state is _S.AUTHORIZED:
                    await self._authorized(view.action)
                elif state is _S.OUTCOME_UNKNOWN:
                    if not await self._unknown(view.action):
                        if view.escalation is not None and self._on_escalated is not None:
                            self._on_escalated(action_id)
                        return
                else:
                    raise RuntimeError(f"{action_id} is {state} with no send in flight")
                self._notify(action_id)
        finally:
            self._driving.discard(action_id)
            self._notify(action_id)

    def _now(self) -> int:
        return self._scheduler.now

    async def _facts(self, action: Action) -> Mapping[str, FactNow]:
        return await self._destination.facts(self._freshness.keys(action))

    async def _retry_later(self) -> None:
        await self._scheduler.sleep(self._timing.retry_check_after_ms)

    # Proposed: check the basis, file the approval request (T2, T3) -------------------------

    async def _proposed(self, action: Action) -> None:
        """Check the basis, then file the approval request if it is fresh. G1 checks no
        basis and files its request at once (0008)."""
        seen = self._store.view(action.action_id).last_seq
        basis: BasisCheck | None = None
        try:
            if not action.authorization_only:
                basis = self._freshness.check(action, await self._facts(action), full=True)
            fresh = basis is None or basis.fresh
            filed = await self._destination.request_approval(action) if fresh else None
        except Unreachable:
            await self._retry_later()
            return
        approval_id = filed.approval_id if filed is not None else None
        self._store.file_approval(action.action_id, basis, approval_id, self._now(), seen_seq=seen)
        if filed is None or filed.status is ApprovalStatus.PENDING:
            return
        view = self._store.view(action.action_id)
        if view.action.state is _S.PENDING_APPROVAL:
            # Policy decides most requests at once (0002): this is the first poll.
            self._store.poll_approval(
                action.action_id, filed.status, basis, self._now(), seen_seq=view.last_seq
            )

    # PendingApproval: poll, and time out after approval_wait_ms (T4, T5) -------------------

    async def _pending(self, action: Action) -> None:
        """One poll. A decision that needs no basis is recorded before any fact is read,
        so an unreachable fact cannot hold back a denial, a revocation, an expiry or the
        approval timeout. G1 reads no fact at all (0008)."""
        view = self._store.view(action.action_id)
        grant = view.grant
        if grant is None:
            raise RuntimeError(f"{action.action_id} waits for approval with no grant")
        due = grant.requested_vt + self._store.settings.approval_wait_ms
        seen = view.last_seq
        try:
            approval = await self._destination.approval(grant.approval_id)
        except Unreachable:
            await self._retry_later()
            return
        status = approval.status
        if status is ApprovalStatus.PENDING and self._now() >= due:
            self._store.time_out_approval(action.action_id, status, self._now(), seen_seq=seen)
            return
        if status in _DEAD_AUTHORITY:
            self._store.poll_approval(action.action_id, status, None, self._now(), seen_seq=seen)
            return
        basis: BasisCheck | None = None
        if not action.authorization_only:
            try:
                basis = self._freshness.check(action, await self._facts(action), full=True)
            except Unreachable:
                await self._retry_later()
                return
        self._store.poll_approval(action.action_id, status, basis, self._now(), seen_seq=seen)
        if self._store.view(action.action_id).action.state is _S.PENDING_APPROVAL:
            await self._scheduler.sleep(
                min(self._timing.approval_poll_ms, max(1, due - self._now()))
            )

    # Authorized: the dispatch check and the first send (T6, T7, then T8 to T10) ------------

    async def _authorized(self, action: Action) -> None:
        ticket = self._store.begin_check(action.action_id)
        self._hook("gw.dispatch.before_check", action)
        try:
            check = await self._check(action, ticket.kind, ticket.record)
        except Unreachable:
            await self._retry_later()
            return
        self._hook("gw.dispatch.after_check", action)
        attempt = self._store.dispatch(action.action_id, check, self._now())
        if attempt is not None:
            await self._send(action, attempt)

    async def _check(
        self,
        action: Action,
        kind: CheckKind,
        record: Callable[..., CheckRecord],
    ) -> CheckRecord:
        """Run the I3 check the ticket asks for, without any lock (0003, dispatch).

        G1's check, `authority_only`, asks for the approval and reads no fact (0008).
        """
        if kind is CheckKind.EXEMPT:
            return record(approval=None, basis=None, check_vt=self._now())
        view = self._store.view(action.action_id)
        if view.grant is None:
            raise RuntimeError(f"{action.action_id} has no grant to check")
        approval = await self._destination.approval(view.grant.approval_id)
        basis: BasisCheck | None = None
        if kind is not CheckKind.AUTHORITY_ONLY:
            facts = await self._facts(action)
            basis = self._freshness.check(action, facts, full=kind is CheckKind.FULL)
        expires_at = approval.expires_at if approval.status is ApprovalStatus.APPROVED else None
        return record(
            approval=approval.status, basis=basis, check_vt=self._now(), expires_at=expires_at
        )

    async def _send(self, action: Action, attempt: Attempt) -> None:
        self._hook("gw.dispatch.after_record", action)
        grant = self._store.view(action.action_id).grant
        if grant is None:
            raise RuntimeError(f"{action.action_id} sends with no grant")
        answer = await self._destination.send(action, attempt, grant.approval_id)
        if answer.reason is not Reason.TIMEOUT:
            # A timeout is no answer, so the point after the answer is reached only when
            # one arrived (0006: a crash there voids the answer just received).
            self._hook("gw.dispatch.after_answer", action)
        self._store.answer(action.action_id, attempt.attempt_no, answer, self._now())

    # OutcomeUnknown: recover by the contract (T11 to T13, escalation) ----------------------

    async def _unknown(self, action: Action) -> bool:
        """One recovery step. Returns False when only an attestation can move the action,
        or, in G1, which has no recovery, when nothing can (0008)."""
        view = self._store.view(action.action_id)
        if view.escalation is not None or action.authorization_only:
            return False
        contract = action.contract
        if contract.has(Capability.STATUS):
            await self._probe(action)
            return True
        if contract.has(Capability.IDEMPOTENCY_KEY):
            await self._retry(action)
            return True
        return False

    async def _probe(self, action: Action) -> None:
        await self._scheduler.sleep(self._timing.probe_interval_ms)
        if self._store.view(action.action_id).action.state is not _S.OUTCOME_UNKNOWN:
            return
        result = await self._destination.lookup(action)
        self._store.probe(action.action_id, result, self._now())

    async def _retry(self, action: Action) -> None:
        ticket = self._store.begin_check(action.action_id)
        if ticket.not_before_vt > self._now():
            await self._scheduler.sleep(ticket.not_before_vt - self._now())
            ticket = self._store.begin_check(action.action_id)
        try:
            check = await self._check(action, ticket.kind, ticket.record)
        except Unreachable:
            await self._retry_later()
            return
        attempt = self._store.retry(action.action_id, check, self._now())
        if attempt is not None:
            await self._send(action, attempt)

    # Fault points ---------------------------------------------------------------------------

    def _hook(self, point: str, action: Action) -> None:
        """A fault point (0006): faults that fire here take effect before the step goes on.

        What a fault acts on is the action: its intent, its business arguments, its
        operation, the idempotency key its request carries (none below C2, and none in
        G1, 0005 and 0008), and the approval its grant names. A fault that
        crashes the gateway (F1) stops the step at the point: nothing after it runs, so
        a crash after the check records no attempt, and one after the record sends
        nothing.
        """
        fired = self._faults.at(point, {"intent_id": action.intent_id, "api": action.operation})
        if not fired:
            return
        if self._on_fault is None:
            raise NotImplementedError(f"{[f.fault for f in fired]} at {point}: no fault handler")
        grant = self._store.view(action.action_id).grant
        context: dict[str, object] = {
            **dict(action.args),
            "intent_id": action.intent_id,
            "api": action.operation,
            "idempotency_key": action.request_key,
        }
        if grant is not None:
            context["approval_id"] = grant.approval_id
        for one in fired:
            self._on_fault(one, point, context)
            if not self._scheduler.is_up(self._component):
                raise asyncio.CancelledError


def _action_number(action_id: str) -> int:
    return int(action_id.split("-", 1)[1])
