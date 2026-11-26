"""Scripted humans: the approver (0002, rule 7) and the resolver (0003, 0009).

The scripted approver follows the scenario's rules: the first rule whose `match` fields
equal the request's decides it, approve or deny, `after_ms` virtual ms after the request
was filed. It never sees the agent's messages: it learns of a request only when the
approvals service queues it, and it acts through the service's control interface. A
request no rule matches stays pending until the caller gives up. Every decision is
recorded in the harness's actor log.

The scripted resolver resolves the gateway's escalations, in G modes, when the scenario
has one (0009). It never sees the agent's messages either: it learns of an escalation
when the gateway leaves an escalated action to a human, and acts through the gateway's
admin path. It acts `after_ms` virtual ms after the escalation opened, but never before
S past the action's latest send: it "attests only after the settle time" (0009), when
every request of the action has committed or never will (0005). It then checks the
simulator's effects ledger out of band, as a human would (0008), for an effect that cites
the action's approval, which every attempt of the action cites and no other action's
attempt does (0002, rules 12 and 13), and tells the truth (0009): `committed`, naming the
effect (the first by commit order, if a duplicated request left more than one), or
`not_committed` if there is none.

While the gateway is down, it cannot attest; it tries again when the restarted gateway
leaves the action to a human again. Each attestation is recorded in the actor log, after
the gateway has recorded it, as the evaluator reads it (0004): actor `resolver`, action
`attest`, the escalation as subject, and details naming the resolution, the intent, the
action and, for `committed`, the effect.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from fivefold.domain.api import ApiName, ApprovalAnswer
from fivefold.domain.authority import ApprovalStatus, Decision
from fivefold.domain.scenario import Authority, Resolver
from fivefold.gateway.admin import GatewayAdmin, OpenEscalation
from fivefold.gateway.records import Resolution
from fivefold.harness.log import ActorRecord, Connection, HarnessLog
from fivefold.harness.scheduler import Scheduler
from fivefold.sim import Control

APPROVER = "approver"

RESOLVER = "resolver"
"""The resolver's name: its actor in the actor log, and on the escalations it resolves."""

ATTEST = "attest"
"""The actor-log action of the resolver's attestation (0004)."""


class ScriptedApprover:
    """Decides queued approval requests by the scenario's rules."""

    def __init__(
        self, scheduler: Scheduler, control: Control, log: HarnessLog, authority: Authority
    ) -> None:
        self._scheduler = scheduler
        self._control = control
        self._log = log
        self._authority = authority
        self._seen: set[str] = set()

    def served(self, api: ApiName, answer: object) -> None:
        """Listen to the simulator's answers; schedule a decision for each new queued one."""
        if api is not ApiName.REQUEST_APPROVAL or not isinstance(answer, ApprovalAnswer):
            return
        if answer.status is not ApprovalStatus.PENDING or answer.approval_id in self._seen:
            return
        self._seen.add(answer.approval_id)
        rule = self._authority.approver_rule(answer.request_fields())
        if rule is None:
            return
        at = max(self._scheduler.now, answer.requested_vt + rule.after_ms)
        approval_id = answer.approval_id
        self._scheduler.call_at(
            at,
            lambda: self._decide(approval_id, rule.decision),
            label=f"approver decides {approval_id}",
        )

    def _decide(self, approval_id: str, decision: Decision) -> None:
        with self._log.ordering():
            now = self._scheduler.now
            answer = self._control.decide(approval_id, decision, actor=APPROVER, vt=now)
            seq = self._log.next_seq()
        self._log.actor_acted(
            ActorRecord(
                seq=seq,
                actor="approver",
                action=str(decision),
                subject=approval_id,
                details={"status": str(answer.status), "intent_id": answer.intent_id},
                vt=now,
            )
        )


class EffectsLedger(Protocol):
    """The simulator's effects ledger, as the resolver reads it out of band (0008)."""

    def effects_citing(self, approval_id: str) -> tuple[str, ...]:
        """The ids of the effects that cite `approval_id`, in commit order."""
        ...


class PgEffectsLedger:
    """`sim.effect`, read as `ff_harness`, which may select from schema `sim` (0008)."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def effects_citing(self, approval_id: str) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT effect_id FROM sim.effect WHERE approval_id = %s ORDER BY commit_seq",
            (approval_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)


AdminPath = Callable[[], GatewayAdmin | None]
"""The gateway's admin path while the gateway is up, or None while it is down."""


class ScriptedResolver:
    """Resolves the gateway's escalations by the scenario's rule, telling the truth."""

    def __init__(
        self,
        scheduler: Scheduler,
        admin: AdminPath,
        ledger: EffectsLedger,
        log: HarnessLog,
        rule: Resolver,
        *,
        settle_ms: int,
    ) -> None:
        """`settle_ms` is S, from the destination's published promises (0005)."""
        self._scheduler = scheduler
        self._admin = admin
        self._ledger = ledger
        self._log = log
        self._rule = rule
        self._settle_ms = settle_ms
        self._seen: set[str] = set()
        # Escalations whose check found the gateway down.
        self._blocked: set[str] = set()

    def escalated(self, action_id: str) -> None:
        """The gateway left an escalated action to a human: look at the open escalations.

        The look is an event of its own at this virtual time, outside the gateway's step.
        """
        self._scheduler.call_at(
            self._scheduler.now, self._look, label=f"resolver hears of {action_id}"
        )

    def _look(self) -> None:
        """Plan a check of each new escalation: `after_ms` after it opened, and never
        before S past the action's latest send (0009)."""
        admin = self._admin()
        if admin is None:
            return
        now = self._scheduler.now
        for escalation in admin.open_escalations():
            escalation_id = escalation.escalation_id
            if escalation_id in self._blocked:
                self._blocked.discard(escalation_id)
                self._check_at(now, escalation_id)
            elif escalation_id not in self._seen:
                self._seen.add(escalation_id)
                due = max(
                    escalation.opened_vt + self._rule.after_ms,
                    escalation.last_sent_vt + self._settle_ms,
                )
                self._check_at(max(now, due), escalation_id)

    def _check_at(self, at: int, escalation_id: str) -> None:
        self._scheduler.call_at(
            at, lambda: self._check(escalation_id), label=f"resolver checks {escalation_id}"
        )

    def _check(self, escalation_id: str) -> None:
        """Check the ledger for the escalated action's effect, and attest the truth."""
        admin = self._admin()
        if admin is None:
            # The restarted gateway leaves the action to a human again, and the resolver
            # looks again then.
            self._blocked.add(escalation_id)
            return
        escalation = next(
            (e for e in admin.open_escalations() if e.escalation_id == escalation_id), None
        )
        if escalation is None:
            return
        effects = self._ledger.effects_citing(escalation.approval_id)
        if effects:
            self._attest(admin, escalation, Resolution.COMMITTED, effects[0])
        else:
            self._attest(admin, escalation, Resolution.NOT_COMMITTED, None)

    def _attest(
        self,
        admin: GatewayAdmin,
        escalation: OpenEscalation,
        resolution: Resolution,
        effect_id: str | None,
    ) -> None:
        """Resolve the escalation through the admin path, then log the attestation.

        Its seq is taken once the gateway has recorded it: that is when the stack has it
        (0004, evidence).
        """
        now = self._scheduler.now
        admin.resolve(escalation.escalation_id, resolution, effect_id, actor=RESOLVER)
        details: dict[str, object] = {
            "resolution": str(resolution),
            "intent_id": escalation.intent_id,
            "action_id": escalation.action_id,
        }
        if effect_id is not None:
            details["effect_id"] = effect_id
        self._log.actor_acted(
            ActorRecord(
                seq=self._log.next_seq(),
                actor="resolver",
                action=ATTEST,
                subject=escalation.escalation_id,
                details=details,
                vt=now,
            )
        )
