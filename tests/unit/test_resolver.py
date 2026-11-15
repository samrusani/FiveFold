"""The gateway's admin path and the scripted resolver (0003, 0009, 0010).

A stand-in world plays the approvals service, which approves every request at once, and
a destination whose ledger records the approval each effect cites. It can lose a request,
lose an answer, commit a request late, or leave status probes unanswered. The resolver
reads that ledger as it reads `sim.effect` in a run. Every test pins the virtual times of
the steps it checks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import pytest

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.scenario import Resolver
from fivefold.gateway.admin import GatewayAdmin, OpenEscalation, UnknownEscalation
from fivefold.gateway.destination import ApprovalNow, FactNow
from fivefold.gateway.dispatcher import Dispatcher
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.records import (
    Action,
    ActionResult,
    Answer,
    ApprovalStatus,
    Attempt,
    BasisCheck,
    ProbeAnswer,
    Resolution,
    WriteCall,
)
from fivefold.gateway.state_machine import (
    FINAL_STATES,
    ActionState,
    AnswerClass,
    EventKind,
    GatewaySettings,
    Note,
    Reason,
)
from fivefold.gateway.steps import StepRefused
from fivefold.harness.actors import ScriptedResolver
from fivefold.harness.log import ActorRecord, MemoryHarnessLog
from fivefold.harness.scheduler import Scheduler

S = ActionState
SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
    max_attempts=3,
    max_probes=3,
)
T = SETTINGS.dispatch_timeout_ms
SETTLE = SETTINGS.settle_ms
HOP = 20
"""The stand-in's one-way latency, in virtual ms."""
FAR = 10**9
"""An approval expiry far beyond any test's virtual time."""
TIMEOUT = Answer(AnswerClass.UNKNOWN, Reason.TIMEOUT)


@dataclass
class World:
    """The approvals service and a destination, as the gateway's dispatcher reaches them."""

    scheduler: Scheduler
    effects: dict[str, str] = field(default_factory=dict)
    """The ledger: each effect id, in commit order, with the approval it cites."""
    lose_requests: int = 0
    lose_answers: int = 0
    commit_late_ms: int | None = None
    """Commit the next request this long after it arrives, after the caller's timeout."""
    status_answers: bool = True
    calls: int = 0
    """Every call the gateway made."""

    async def facts(self, keys: Sequence[str]) -> Mapping[str, FactNow]:
        self.calls += 1
        await self.scheduler.sleep(2 * HOP)
        return {key: FactNow(key, 1, {}) for key in keys}

    async def request_approval(self, action: Action) -> ApprovalNow:
        self.calls += 1
        await self.scheduler.sleep(2 * HOP)
        return ApprovalNow(f"APR-{action.action_id[2:]}", ApprovalStatus.APPROVED, FAR)

    async def approval(self, approval_id: str) -> ApprovalNow:
        self.calls += 1
        await self.scheduler.sleep(2 * HOP)
        return ApprovalNow(approval_id, ApprovalStatus.APPROVED, FAR)

    async def send(self, action: Action, attempt: Attempt, approval_id: str) -> Answer:
        self.calls += 1
        if self.lose_requests:
            self.lose_requests -= 1
            await self.scheduler.sleep(T)
            return TIMEOUT
        await self.scheduler.sleep(HOP)
        if self.commit_late_ms is not None:
            delay, self.commit_late_ms = self.commit_late_ms, None

            def commit() -> None:
                self._commit(approval_id)

            self.scheduler.call_later(delay, commit)
            await self.scheduler.sleep(T - HOP)
            return TIMEOUT
        effect_id = self._commit(approval_id)
        if self.lose_answers:
            self.lose_answers -= 1
            await self.scheduler.sleep(T - HOP)
            return TIMEOUT
        await self.scheduler.sleep(HOP)
        return Answer(AnswerClass.COMMITTED, Reason.COMMITTED, effect_id)

    async def lookup(self, action: Action) -> ProbeAnswer:
        self.calls += 1
        await self.scheduler.sleep(2 * HOP)
        if not self.status_answers:
            return ProbeAnswer(answered=False)
        listed = tuple(self.effects)
        return ProbeAnswer(answered=True, effect_ids=listed, as_of_vt=self.scheduler.now)

    def _commit(self, approval_id: str) -> str:
        effect_id = f"PO-{len(self.effects) + 1:04d}"
        self.effects[effect_id] = approval_id
        return effect_id

    def effects_citing(self, approval_id: str) -> tuple[str, ...]:
        """The ledger as the resolver reads it, out of band."""
        return tuple(e for e, cited in self.effects.items() if cited == approval_id)


class Fresh:
    """Every basis is fresh."""

    def keys(self, action: Action) -> Sequence[str]:
        return ["quote/S-1/W-100", "budget/CC-OPS"]

    def check(self, action: Action, now: Mapping[str, FactNow], *, full: bool) -> BasisCheck:
        return BasisCheck(True, True if full else None)


class Gateway:
    """A gateway on a scheduler, with its admin path, and a resolver if `after_ms` is set.

    `handed` records each time the dispatcher left an escalated action to a human.
    """

    def __init__(self, contract: Contract = Contract.C0, *, after_ms: int | None = None) -> None:
        self.scheduler = Scheduler()
        self.world = World(self.scheduler)
        self.store = MemoryStore(
            task_id="TASK-1",
            intents={"REQ-1": "submit_purchase_order"},
            contract=contract,
            settings=SETTINGS,
        )
        self.log = MemoryHarnessLog()
        self.handed: list[tuple[int, str]] = []
        self.after_first_hand_over: Callable[[], None] | None = None
        """Runs once, right after the first hand-over, in the same step."""
        self.resolver: ScriptedResolver | None = None
        if after_ms is not None:
            rule = Resolver(after_ms=after_ms, attests="truth")
            self.resolver = ScriptedResolver(
                self.scheduler, self.admin, self.world, self.log, rule, settle_ms=SETTLE
            )
        self.dispatcher = Dispatcher(
            self.scheduler, self.store, self.world, Fresh(), on_escalated=self._escalated
        )

    def admin(self) -> GatewayAdmin | None:
        if not self.scheduler.is_up("gateway"):
            return None
        return GatewayAdmin(self.scheduler, self.store, self.dispatcher)

    def up(self) -> GatewayAdmin:
        admin = self.admin()
        assert admin is not None, "the gateway is down"
        return admin

    def _escalated(self, action_id: str) -> None:
        self.handed.append((self.scheduler.now, action_id))
        if self.resolver is not None:
            self.resolver.escalated(action_id)
        then, self.after_first_hand_over = self.after_first_hand_over, None
        if then is not None:
            then()

    def propose(self) -> str:
        call = WriteCall(
            "REQ-1",
            "submit_purchase_order",
            "supplier/S-1",
            {"supplier_id": "S-1", "quantity": 100},
            "key",
            (),
            (),
        )
        result = self.store.propose(call, self.scheduler.now)
        assert isinstance(result, ActionResult)
        self.dispatcher.drive(result.action_id)
        return result.action_id

    def go(self, *, until_ms: int = 100_000, setup: Callable[[], None] | None = None) -> str:
        """Propose at vt 0 and run to `until_ms`; `setup` schedules more first."""
        holder: list[str] = []

        async def main() -> None:
            holder.append(self.propose())
            if setup is not None:
                setup()
            await self.scheduler.run(deadline_ms=until_ms)

        asyncio.run(main())
        return holder[0]

    def crash(self, at: int, *, restart_after_ms: int) -> None:
        """Crash the gateway at `at`; it restarts through startup recovery (0003)."""
        self.scheduler.call_at(
            at,
            lambda: self.scheduler.crash(
                "gateway", restart_after_ms=restart_after_ms, restart=self.dispatcher.start
            ),
        )

    def sent_and_opened(self, action_id: str) -> tuple[int, int]:
        """When the action's only attempt was sent, and when its escalation opened."""
        view = self.store.view(action_id)
        (attempt,) = view.attempts
        assert view.escalation is not None
        return attempt.sent_vt, view.escalation.opened_vt

    def last(self, action_id: str) -> tuple[str, Reason | Note, int]:
        event = self.store.view(action_id).events[-1]
        return str(event.to_state), event.reason, event.vt


def _escalated(contract: Contract = Contract.C0, **world: object) -> tuple[Gateway, str]:
    """A gateway whose first action is escalated at vt 360, sent at 160, and nothing more."""
    gateway = Gateway(contract)
    for name, value in world.items():
        setattr(gateway.world, name, value)
    action_id = gateway.go(until_ms=1_000)
    assert gateway.sent_and_opened(action_id) == (160, 160 + T)
    return gateway, action_id


def _at(gateway: Gateway, vt: int, step: Callable[[], object]) -> None:
    """Run `step` at `vt`, then let the run go on to 100,000."""
    results: list[object] = []

    async def main() -> None:
        gateway.scheduler.call_at(vt, lambda: results.append(step()))
        await gateway.scheduler.run(deadline_ms=100_000)

    asyncio.run(main())


# The admin path ----------------------------------------------------------------------------


def test_the_admin_path_lists_an_open_escalation_with_what_a_human_needs() -> None:
    gateway, action_id = _escalated(lose_answers=1)
    assert gateway.up().open_escalations() == (
        OpenEscalation(
            escalation_id="ESC-0001",
            action_id=action_id,
            intent_id="REQ-1",
            operation="submit_purchase_order",
            target="supplier/S-1",
            args={"supplier_id": "S-1", "quantity": 100},
            approval_id="APR-0001",
            opened_vt=360,
            last_sent_vt=160,
        ),
    )


def test_attesting_a_commit_confirms_the_action_with_the_named_effect() -> None:
    gateway, action_id = _escalated(lose_answers=1)
    results: list[ActionResult] = []
    _at(
        gateway,
        2_000,
        lambda: results.append(
            gateway.up().resolve("ESC-0001", Resolution.COMMITTED, "PO-0001", actor="resolver")
        ),
    )
    ((state, effect_id, reason),) = [(r.state, r.effect_id, r.reason) for r in results]
    assert (state, effect_id, reason) == (S.CONFIRMED, "PO-0001", Reason.HUMAN_ATTESTED)
    view = gateway.store.view(action_id)
    assert view.action.effect_id == "PO-0001"
    assert gateway.last(action_id) == ("confirmed", Reason.HUMAN_ATTESTED, 2_000)
    assert view.events[-1].details == {"effect_id": "PO-0001", "actor": "resolver"}
    escalation = view.escalation
    assert escalation is not None
    assert (escalation.resolved_vt, escalation.resolution, escalation.effect_id) == (
        2_000,
        Resolution.COMMITTED,
        "PO-0001",
    )
    assert escalation.actor == "resolver"
    assert gateway.up().open_escalations() == ()


def test_not_committed_is_refused_before_the_settle_time_and_accepted_at_it() -> None:
    """0010, reading 8: only at or after S past the action's latest send."""
    gateway, action_id = _escalated(lose_requests=1)
    sent, _ = gateway.sent_and_opened(action_id)
    refusals: list[str] = []

    def too_early() -> None:
        with pytest.raises(StepRefused, match=f"before vt {sent + SETTLE}") as refused:
            gateway.up().resolve("ESC-0001", Resolution.NOT_COMMITTED, actor="resolver")
        refusals.append(str(refused.value))

    _at(gateway, sent + SETTLE - 1, too_early)
    assert refusals and gateway.store.view(action_id).action.state is S.OUTCOME_UNKNOWN
    _at(
        gateway,
        sent + SETTLE,
        lambda: gateway.up().resolve("ESC-0001", Resolution.NOT_COMMITTED, actor="resolver"),
    )
    assert gateway.last(action_id) == ("failed", Reason.HUMAN_ATTESTED, sent + SETTLE)


def test_an_attestation_ends_a_status_wait_at_once() -> None:
    """`get_action_status(wait_ms)` returns as soon as the action is final (0003)."""
    gateway, action_id = _escalated(lose_answers=1)
    woke: list[tuple[int, ActionState]] = []

    async def waiter() -> None:
        await gateway.dispatcher.wait(action_id, FINAL_STATES.__contains__, 60_000)
        woke.append((gateway.scheduler.now, gateway.store.view(action_id).action.state))

    def attest() -> None:
        gateway.up().resolve("ESC-0001", Resolution.COMMITTED, "PO-0001", actor="resolver")

    async def main() -> None:
        gateway.scheduler.spawn("gateway", waiter(), name="waiter")
        gateway.scheduler.call_at(5_000, attest)
        await gateway.scheduler.run(deadline_ms=100_000)

    asyncio.run(main())
    assert woke == [(5_000, S.CONFIRMED)], "not at the end of the wait, 61,000"


def test_an_unknown_or_resolved_escalation_and_a_bad_attestation_are_refused() -> None:
    gateway, action_id = _escalated(lose_answers=1)
    admin = gateway.up()
    with pytest.raises(UnknownEscalation):
        admin.resolve("ESC-0009", Resolution.COMMITTED, "PO-0001", actor="resolver")
    with pytest.raises(ValueError, match="names the effect"):
        admin.resolve("ESC-0001", Resolution.COMMITTED, actor="resolver")
    with pytest.raises(ValueError, match="human who made it"):
        admin.resolve("ESC-0001", Resolution.COMMITTED, "PO-0001", actor="")
    assert gateway.store.view(action_id).action.state is S.OUTCOME_UNKNOWN
    admin.resolve("ESC-0001", Resolution.COMMITTED, "PO-0001", actor="resolver")
    with pytest.raises(StepRefused):
        admin.resolve("ESC-0001", Resolution.COMMITTED, "PO-0001", actor="resolver")


def test_the_admin_path_never_contacts_the_simulator() -> None:
    gateway, _ = _escalated(lose_answers=1)
    calls = gateway.world.calls
    admin = gateway.up()
    admin.open_escalations()
    admin.resolve("ESC-0001", Resolution.COMMITTED, "PO-0001", actor="resolver")
    assert gateway.world.calls == calls


# The dispatcher leaves escalated actions to a human ----------------------------------------


def test_the_dispatcher_hands_over_an_escalated_action_and_again_after_a_restart() -> None:
    gateway = Gateway(Contract.C0)
    gateway.world.lose_answers = 1
    action_id = gateway.go(setup=lambda: gateway.crash(1_000, restart_after_ms=500))
    assert gateway.handed == [(360, action_id), (1_500, action_id)]


def test_c1_hands_over_after_its_last_unanswered_probe() -> None:
    gateway = Gateway(Contract.C1)
    gateway.world.lose_answers = 1
    gateway.world.status_answers = False
    action_id = gateway.go()
    escalation = gateway.store.view(action_id).escalation
    assert escalation is not None
    assert gateway.handed == [(escalation.opened_vt, action_id)]
    probes = [e for e in gateway.store.view(action_id).events if e.reason is Note.PROBE]
    assert len(probes) == SETTINGS.max_probes and probes[-1].vt == escalation.opened_vt


def test_an_action_that_recovers_by_itself_is_never_handed_over() -> None:
    gateway = Gateway(Contract.C2)
    gateway.world.lose_answers = 1
    action_id = gateway.go()
    assert gateway.store.view(action_id).action.state is S.CONFIRMED
    assert gateway.handed == []


# The resolver's decisions --------------------------------------------------------------


def _attestations(gateway: Gateway) -> list[ActorRecord]:
    return [record for record in gateway.log.actors if record.actor == "resolver"]


def test_the_resolver_attests_the_effect_it_finds_after_ms_after_the_escalation() -> None:
    gateway = Gateway(Contract.C0, after_ms=5_000)
    gateway.world.lose_answers = 1
    action_id = gateway.go()
    _, opened = gateway.sent_and_opened(action_id)
    (record,) = _attestations(gateway)
    assert (record.action, record.subject, record.vt) == ("attest", "ESC-0001", opened + 5_000)
    assert record.details == {
        "resolution": "committed",
        "intent_id": "REQ-1",
        "action_id": action_id,
        "effect_id": "PO-0001",
    }
    assert gateway.last(action_id) == ("confirmed", Reason.HUMAN_ATTESTED, opened + 5_000)
    assert gateway.store.view(action_id).action.effect_id == "PO-0001"


def test_the_resolver_attests_not_committed_when_the_ledger_has_nothing() -> None:
    gateway = Gateway(Contract.C0, after_ms=5_000)
    gateway.world.lose_requests = 1
    action_id = gateway.go()
    sent, opened = gateway.sent_and_opened(action_id)
    assert opened + 5_000 >= sent + SETTLE
    (record,) = _attestations(gateway)
    assert record.vt == opened + 5_000
    assert record.details == {
        "resolution": "not_committed",
        "intent_id": "REQ-1",
        "action_id": action_id,
    }
    assert gateway.last(action_id) == ("failed", Reason.HUMAN_ATTESTED, opened + 5_000)
    assert gateway.world.effects == {}


@pytest.mark.parametrize(
    ("world", "resolution"),
    [({"lose_requests": 1}, "not_committed"), ({"lose_answers": 1}, "committed")],
)
def test_the_resolver_attests_only_after_the_settle_time(
    world: dict[str, int], resolution: str
) -> None:
    """0009: "attests only after the settle time". With `after_ms` 0 it would act when
    the escalation opens, T after the send; it waits until S past the send instead,
    whatever it will find."""
    gateway = Gateway(Contract.C0, after_ms=0)
    for name, value in world.items():
        setattr(gateway.world, name, value)
    action_id = gateway.go()
    sent, opened = gateway.sent_and_opened(action_id)
    assert opened < sent + SETTLE
    (record,) = _attestations(gateway)
    assert (record.vt, record.details["resolution"]) == (sent + SETTLE, resolution)
    state = "confirmed" if resolution == "committed" else "failed"
    assert gateway.last(action_id) == (state, Reason.HUMAN_ATTESTED, sent + SETTLE)


def test_a_commit_that_lands_after_the_escalation_is_in_the_ledger_when_it_looks() -> None:
    """The order commits late, after the caller's timeout and before S: when the resolver
    looks, at S past the send, the truth is committed."""
    gateway = Gateway(Contract.C0, after_ms=0)
    gateway.world.commit_late_ms = 500
    action_id = gateway.go()
    sent, opened = gateway.sent_and_opened(action_id)
    assert opened < sent + HOP + 500 < sent + SETTLE, "it lands after the escalation"
    (record,) = _attestations(gateway)
    assert (record.vt, record.details["resolution"]) == (sent + SETTLE, "committed")
    assert record.details["effect_id"] == "PO-0001"
    assert gateway.last(action_id) == ("confirmed", Reason.HUMAN_ATTESTED, sent + SETTLE)


def test_while_the_gateway_is_down_the_resolver_waits_for_its_restart() -> None:
    """It cannot reach the admin path at 5,360; the restart at 6,000 hands it the action
    again, and it attests then."""
    gateway = Gateway(Contract.C0, after_ms=5_000)
    gateway.world.lose_answers = 1
    action_id = gateway.go(setup=lambda: gateway.crash(5_000, restart_after_ms=1_000))
    _, opened = gateway.sent_and_opened(action_id)
    assert opened + 5_000 == 5_360
    (record,) = _attestations(gateway)
    assert (record.vt, record.details["resolution"]) == (6_000, "committed")
    assert gateway.handed == [(opened, action_id), (6_000, action_id)]
    assert gateway.last(action_id) == ("confirmed", Reason.HUMAN_ATTESTED, 6_000)


def test_a_restart_before_the_resolver_acts_does_not_make_it_act_twice() -> None:
    gateway = Gateway(Contract.C0, after_ms=5_000)
    gateway.world.lose_answers = 1
    action_id = gateway.go(setup=lambda: gateway.crash(1_000, restart_after_ms=1_000))
    _, opened = gateway.sent_and_opened(action_id)
    assert len(gateway.handed) == 2
    (record,) = _attestations(gateway)
    assert record.vt == opened + 5_000, "counted from the escalation, once"
    transitions = [
        e for e in gateway.store.view(action_id).events if e.kind is EventKind.TRANSITION
    ]
    assert [e.reason for e in transitions].count(Reason.HUMAN_ATTESTED) == 1


def test_after_ms_counts_from_when_the_escalation_opened_even_if_the_resolver_hears_late() -> None:
    """The gateway crashes in the step that escalated, before the resolver can look, so
    the resolver first sees the escalation after the restart, 1,000 ms later."""
    gateway = Gateway(Contract.C0, after_ms=5_000)
    gateway.world.lose_answers = 1
    gateway.after_first_hand_over = lambda: gateway.scheduler.crash(
        "gateway", restart_after_ms=1_000, restart=gateway.dispatcher.start
    )
    action_id = gateway.go()
    _, opened = gateway.sent_and_opened(action_id)
    assert gateway.handed == [(opened, action_id), (opened + 1_000, action_id)]
    (record,) = _attestations(gateway)
    assert (record.vt, record.details["resolution"]) == (opened + 5_000, "committed")


def test_after_ms_counts_from_the_escalation_not_from_the_unknown_outcome() -> None:
    """At C1 the gateway probes first, and escalates after its last unanswered probe."""
    gateway = Gateway(Contract.C1, after_ms=1_000)
    gateway.world.lose_answers = 1
    gateway.world.status_answers = False
    action_id = gateway.go()
    view = gateway.store.view(action_id)
    unknown = next(e.vt for e in view.events if e.to_state is S.OUTCOME_UNKNOWN)
    _, opened = gateway.sent_and_opened(action_id)
    assert opened > unknown + 1_000
    (record,) = _attestations(gateway)
    assert (record.vt, record.details["resolution"]) == (opened + 1_000, "committed")


def test_the_resolver_does_nothing_when_nothing_is_escalated() -> None:
    gateway = Gateway(Contract.C2, after_ms=0)
    gateway.world.lose_answers = 1
    action_id = gateway.go()
    assert gateway.store.view(action_id).action.state is S.CONFIRMED
    assert gateway.log.actors == []
