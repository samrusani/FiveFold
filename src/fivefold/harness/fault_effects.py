"""Faults that change the simulator's state: F6 revokes, F7 and F8 change facts.

A fault takes effect at its point, before the scheduler delivers any further message. These
faults apply directly through the simulator's control interface at that moment; they are
not sent as messages, so they cannot arrive late (0006, Fault points).

What a fault acts on comes from where it fires: the action at the gateway's dispatch
check, or the message on the wire (the write request, or B0's approval answer).

- F6 revokes the approval the action or message cites, as the scripted approver would
  (0002, rule 8; 0006). The revocation is recorded in the actor log.
- F7 changes the quote the action or message cites. The default variant, `price_up`,
  raises the unit price by `percent` (default 5), rounding up, but no higher than the
  intent's maximum, so the order stays acceptable. The `harmless` variant adds a day
  to the lead time, which no dependency rule reads (0004).
- F8 corrects the order's memory. Relevant variants set the channel to `portal`.
  Harmless variants change only the notes (0023).
"""

from __future__ import annotations

from collections.abc import Mapping

from fivefold.domain.rules import memory_key, quote_key
from fivefold.domain.scenario import PurchaseOrderAccept, Scenario
from fivefold.faults.catalog import Action
from fivefold.faults.plan import Fired
from fivefold.harness.actors import APPROVER
from fivefold.harness.log import ActorRecord, HarnessLog
from fivefold.harness.scheduler import Scheduler
from fivefold.sim import Control

FAULT_ACTOR = "fault-plan"
"""The actor a fact version names when a fault plan caused it."""


class FaultEffectError(RuntimeError):
    """A fault fired where it has nothing to act on: the plan is wrong for the scenario."""


class SimulatorFaults:
    """Applies F6, F7 and F8 through the simulator's control interface."""

    def __init__(
        self, scheduler: Scheduler, control: Control, log: HarnessLog, scenario: Scenario
    ) -> None:
        self._scheduler = scheduler
        self._control = control
        self._log = log
        self._scenario = scenario

    def apply(self, fired: Fired, context: Mapping[str, object]) -> None:
        """Apply a fault that fired with what it acts on in `context`."""
        action = fired.planned.kind.action
        if action is Action.REVOKE:
            self._revoke(fired, context)
        elif action is Action.CHANGE_QUOTE:
            self._change_quote(fired, context)
        elif action is Action.CHANGE_MEMORY:
            self._change_memory(fired, context)
        else:
            raise FaultEffectError(f"{fired.fault} does not change the simulator")

    def _revoke(self, fired: Fired, context: Mapping[str, object]) -> None:
        approval_id = context.get("approval_id")
        if not isinstance(approval_id, str):
            raise FaultEffectError(f"{fired.fault} fired where no approval is cited")
        with self._log.ordering():
            now = self._scheduler.now
            answer = self._control.revoke(approval_id, actor=APPROVER, vt=now)
            seq = self._log.next_seq()
        self._log.actor_acted(
            ActorRecord(
                seq=seq,
                actor="approver",
                action="revoke",
                subject=approval_id,
                details={"fault": str(fired.fault), "status": str(answer.status)},
                vt=now,
            )
        )

    def _change_quote(self, fired: Fired, context: Mapping[str, object]) -> None:
        supplier_id, sku = context.get("supplier_id"), context.get("sku")
        if not isinstance(supplier_id, str) or not isinstance(sku, str):
            raise FaultEffectError(f"{fired.fault} fired where no quote is cited")
        key = quote_key(supplier_id, sku)
        current = self._control.current_fact(key)
        if current is None:
            raise FaultEffectError(f"{fired.fault}: there is no quote {key}")
        changes: dict[str, str | int]
        variant = fired.planned.variant
        if variant == "harmless":
            changes = {"lead_time_days": int(str(current.data["lead_time_days"])) + 1}
        elif variant in (None, "price_up"):
            price = int(str(current.data["unit_price_minor"]))
            percent = fired.planned.param("percent")
            raised = -(-price * (100 + percent) // 100)
            changes = {"unit_price_minor": min(raised, self._ceiling(context, price))}
        else:
            raise FaultEffectError(f"{fired.fault} has no variant {variant!r}")
        self._control.change_fact(key, changes, actor=FAULT_ACTOR, vt=self._scheduler.now)

    def _change_memory(self, fired: Fired, context: Mapping[str, object]) -> None:
        supplier_id, sku = context.get("supplier_id"), context.get("sku")
        if not isinstance(supplier_id, str) or not isinstance(sku, str):
            raise FaultEffectError("F8 fired without a supplier and sku")
        key = memory_key(supplier_id, sku)
        if str(fired.planned.variant).endswith("_relevant"):
            changes = {"order_channel": "portal"}
        else:
            current = self._control.current_fact(key)
            if current is None:
                raise FaultEffectError(f"F8: there is no memory {key}")
            changes = {"notes": f"{current.data['notes']} (corrected)"}
        self._control.correct_memory(key, changes, actor=FAULT_ACTOR, vt=self._scheduler.now)

    def _ceiling(self, context: Mapping[str, object], price: int) -> int:
        """The intent's highest acceptable unit price, so the change stays acceptable."""
        intent_id = context.get("intent_id")
        if isinstance(intent_id, str):
            accept = self._scenario.intent(intent_id).accept
            if isinstance(accept, PurchaseOrderAccept):
                return max(price, accept.max_unit_price_minor)
        return price * 2


def message_context(body: object) -> dict[str, object]:
    """What a fault on a message acts on: a write request's, or an approval answer's."""
    if not isinstance(body, Mapping):
        return {}
    context: dict[str, object] = {
        name: body[name]
        for name in ("intent_id", "approval_id", "supplier_id", "sku")
        if name in body
    }
    scope = body.get("scope")
    if isinstance(scope, Mapping):
        for name in ("supplier_id", "sku"):
            if name in scope:
                context.setdefault(name, scope[name])
    return context
