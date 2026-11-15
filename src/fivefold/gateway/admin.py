"""The gateway's admin path, in process: open escalations and their resolution (0003).

Escalation hands an action whose outcome is unknown to a human: after the `escalated`
note, only an attestation moves the action (0010, reading 7). A human resolves an
escalation through the admin API by attesting that the action committed, naming the
effect, or that it did not (0003, escalation). `GatewayAdmin` is the core of that API,
in the gateway's process. In-process runs call it directly, as the scripted resolver
does; `admin_api.py` serves it over HTTP, with the gateway's records, for a gateway
served over HTTP. It reads time only from the run's virtual clock (0009), and it never
contacts the simulator: the human checks the world out of band, and the gateway records
what the human attests.

- `open_escalations` lists the escalations no one has resolved yet, in escalation-id
  order, with what a human needs to check each one: the action, the approval its
  attempts cite, when the escalation opened, when the action was last sent, and from
  when "not committed" can be attested.
- `resolve` attests one escalation through the store's `attest`: T11 or T12, with reason
  `human_attested`. The store refuses "not committed" before S past the action's latest
  send, and accepts an attestation that names an effect at any time (0010, reading 8).
  Every wait on the action then ends at once, as it does after a step of its driver. A
  refusal is named after the store refused: `EscalationResolved` for an escalation
  resolved already, `BeforeSettleTime` for "not committed" too early. Both are
  `StepRefused`, and the store's rules decide either way.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fivefold.gateway import steps
from fivefold.gateway.dispatcher import Dispatcher
from fivefold.gateway.records import ActionResult, Escalation, Resolution, check_text
from fivefold.gateway.steps import StepRefused
from fivefold.gateway.store import ActionStore, in_action_order, view_of
from fivefold.harness.scheduler import Scheduler


class UnknownEscalation(LookupError):
    """No action of the run has an escalation with this id."""


class EscalationResolved(StepRefused):
    """The escalation is resolved already: each one is resolved once (0003, escalation).

    `escalation` is the escalation as it was resolved: when, how and by whom.
    """

    def __init__(self, message: str, escalation: Escalation) -> None:
        super().__init__(message)
        self.escalation = escalation


class BeforeSettleTime(StepRefused):
    """ "Not committed" before S past the action's latest send (0010, reading 8).

    `not_committed_from_vt` is the first virtual time at which it can be attested.
    """

    def __init__(self, message: str, not_committed_from_vt: int) -> None:
        super().__init__(message)
        self.not_committed_from_vt = not_committed_from_vt


@dataclass(frozen=True)
class OpenEscalation:
    """An escalation no one has resolved yet, as the admin path lists it (0003)."""

    escalation_id: str
    action_id: str
    intent_id: str
    operation: str
    target: str
    args: Mapping[str, str | int]
    approval_id: str
    """The approval the action's grant names. Every attempt of the action cites it, and
    no attempt of another action does (0002, rules 12 and 13)."""
    opened_vt: int
    last_sent_vt: int
    """When the action's latest attempt was sent. "Not committed" can be attested only
    at or after S past it (0010, reading 8)."""
    not_committed_from_vt: int
    """`last_sent_vt` plus S: from then on, every request of the action has committed or
    never will (0005), and "not committed" can be attested."""


class GatewayAdmin:
    """The admin path of one gateway: its open escalations, and their resolution."""

    def __init__(self, scheduler: Scheduler, store: ActionStore, dispatcher: Dispatcher) -> None:
        self._scheduler = scheduler
        self._store = store
        self._dispatcher = dispatcher

    def open_escalations(self) -> tuple[OpenEscalation, ...]:
        """Every escalation no one has resolved yet, in escalation-id order."""
        snapshot = self._store.snapshot()
        escalated = [e.action_id for e in snapshot.escalations.values() if e.resolved_vt is None]
        settle_ms = self._store.settings.settle_ms
        found: list[OpenEscalation] = []
        for action_id in in_action_order(escalated):
            view = view_of(snapshot, action_id)
            escalation, grant = view.escalation, view.grant
            if escalation is None or grant is None or not view.attempts:
                raise AssertionError(f"{action_id} is escalated with no grant or no send")
            action = view.action
            last_sent_vt = max(attempt.sent_vt for attempt in view.attempts)
            found.append(
                OpenEscalation(
                    escalation_id=escalation.escalation_id,
                    action_id=action_id,
                    intent_id=action.intent_id,
                    operation=action.operation,
                    target=action.target,
                    args=action.args,
                    approval_id=grant.approval_id,
                    opened_vt=escalation.opened_vt,
                    last_sent_vt=last_sent_vt,
                    not_committed_from_vt=last_sent_vt + settle_ms,
                )
            )
        return tuple(found)

    def resolve(
        self,
        escalation_id: str,
        resolution: Resolution,
        effect_id: str | None = None,
        *,
        actor: str,
    ) -> ActionResult:
        """Attest an escalated action's outcome now: T11 or T12, `human_attested` (0003).

        `effect_id` names the effect for `committed` and is None for `not_committed`.
        `actor` names the human, as the escalation records it. Returns the action's
        result after the attestation.

        Raises `UnknownEscalation` for an id no action has; `EscalationResolved` for an
        escalation already resolved; `BeforeSettleTime` for "not committed" before S past
        the action's latest send; and `ValueError` for an effect id that does not fit the
        resolution. The two refusals are `StepRefused`, raised once the store refused.
        """
        if not actor:
            raise ValueError("an attestation names the human who made it")
        check_text(actor, "the actor")
        if effect_id is not None:
            check_text(effect_id, "the effect id")
        escalation = self._store.snapshot().escalations.get(escalation_id)
        if escalation is None:
            raise UnknownEscalation(escalation_id)
        action_id = escalation.action_id
        try:
            self._store.attest(action_id, resolution, effect_id, actor, self._scheduler.now)
        except StepRefused as refused:
            named = self._named(action_id, resolution, refused)
            if named is None:
                raise
            raise named from refused
        self._dispatcher.wake(action_id)
        return steps.result_for(self._store.view(action_id))

    def _named(
        self, action_id: str, resolution: Resolution, refused: StepRefused
    ) -> StepRefused | None:
        """Why the store refused an attestation, read from the records after it did."""
        view = self._store.view(action_id)
        escalation = view.escalation
        if escalation is not None and escalation.resolved_vt is not None:
            message = (
                f"{escalation.escalation_id} is resolved already: {escalation.resolution},"
                f" attested by {escalation.actor} at vt {escalation.resolved_vt}"
            )
            return EscalationResolved(message, escalation)
        if resolution is Resolution.NOT_COMMITTED and view.attempts:
            last_sent_vt = max(attempt.sent_vt for attempt in view.attempts)
            due = last_sent_vt + self._store.settings.settle_ms
            if self._scheduler.now < due:
                return BeforeSettleTime(str(refused), due)
        return None
