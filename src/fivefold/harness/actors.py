"""Scripted humans: the approver (0002, rule 7).

The scripted approver follows the scenario's rules: the first rule whose `match` fields
equal the request's decides it, approve or deny, `after_ms` virtual ms after the request
was filed. It never sees the agent's messages: it learns of a request only when the
approvals service queues it, and it acts through the service's control interface. A
request no rule matches stays pending until the caller gives up. Every decision is
recorded in the harness's actor log.
"""

from __future__ import annotations

from fivefold.domain.api import ApiName, ApprovalAnswer
from fivefold.domain.authority import ApprovalStatus, Decision
from fivefold.domain.scenario import Authority
from fivefold.harness.log import ActorRecord, HarnessLog
from fivefold.harness.scheduler import Scheduler
from fivefold.sim import Control

APPROVER = "approver"


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
        now = self._scheduler.now
        answer = self._control.decide(approval_id, decision, actor=APPROVER, vt=now)
        self._log.actor_acted(
            ActorRecord(
                seq=self._log.next_seq(),
                actor="approver",
                action=str(decision),
                subject=approval_id,
                details={"status": str(answer.status), "intent_id": answer.intent_id},
                vt=now,
            )
        )
