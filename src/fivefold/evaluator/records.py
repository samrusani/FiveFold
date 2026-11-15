"""The ground truth of one run, as the evaluator reads it (0004).

These records copy rows of the simulator's and the harness's tables, and the scenario.
The evaluator decides only from them. `fivefold.evaluator.load` reads them from a run
database; tests build them by hand.

The harness's conventions that the property checks rely on (0006, 0008):

- Components are `gateway`, `simulator`, the agent workers `agent-1` and `agent-2`, and
  the report sink `report_sink`.
- The stack is the system under test (0004): the gateway in G modes, the agent workers in
  baselines. Its write requests are `submit_purchase_order` and `reserve_inventory`
  requests that a stack component sends to the simulator on the stack's hop: `gw_sim`
  in G modes, `agent_sim` in baselines.
- A message's body is the request or answer as sent. On `gw_sim` and `agent_sim` it is
  the simulator API's JSON (`fivefold.domain.api`).
- The approvals service records a decision that policy makes on arrival with the actor
  `policy` (`fivefold.domain.authority.POLICY_ACTOR`). Any other actor that decides is a
  human: the scripted approver in deterministic runs (0002, rules 6 and 7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from pydantic import JsonValue

from fivefold.domain.api import ApiName
from fivefold.domain.authority import POLICY_ACTOR, Decision
from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.rules import Operation
from fivefold.domain.scenario import Scenario, scenario_digest

GATEWAY = "gateway"
SIMULATOR = "simulator"
REPORT_SINK = "report_sink"
AGENT_PREFIX = "agent-"
"""Agent workers are named `agent-1`, `agent-2` and so on."""

WRITE_APIS = frozenset({str(ApiName.SUBMIT_PURCHASE_ORDER), str(ApiName.RESERVE_INVENTORY)})
"""The simulator calls that write. Each one's name is also its operation."""

STATUS_APIS = frozenset({str(ApiName.GET_ORDER_STATUS), str(ApiName.GET_RESERVATION_STATUS)})
"""The simulator's status lookups (C1)."""

POSSIBLY_SENT_POINT = "gw.dispatch.after_record"
"""A crash here leaves a request possibly sent: recorded, but maybe never sent (0006)."""

RESOLVER = "resolver"
"""The scripted resolver's actor name in the actor log."""

ATTEST = "attest"
"""The actor-log action of an attestation by the resolver (0003, escalation)."""


class RecordsError(Exception):
    """The run's records break a rule the evaluator relies on, so it cannot score them.

    This means a bug in the component that wrote them, or the wrong scenario. The
    evaluator refuses to guess.
    """


class Config(StrEnum):
    """The configurations the harness runs (0008)."""

    B0 = "B0"
    G1 = "G1"
    G2 = "G2"

    @property
    def has_gateway(self) -> bool:
        """Whether the agent writes through the gateway."""
        return self is not Config.B0

    @property
    def stack_hop(self) -> str:
        """The hop on which the stack talks to the simulator."""
        return "gw_sim" if self.has_gateway else "agent_sim"

    def is_stack(self, component: str) -> bool:
        """Whether evidence delivered to `component` reaches the stack (0004).

        That is the gateway in G modes, and any agent worker in baselines.
        """
        if self.has_gateway:
            return component == GATEWAY
        return component.startswith(AGENT_PREFIX)

    @property
    def report_receiver(self) -> str:
        """The component a final report is delivered to: the gateway, or the sink."""
        return GATEWAY if self.has_gateway else REPORT_SINK

    @property
    def accepted_by(self) -> str:
        """How `harness.final_report.accepted_by` names that component."""
        return GATEWAY if self.has_gateway else "sink"


@dataclass(frozen=True)
class RunInfo:
    """The row of `harness.run`: what the run is (0009). Wall-clock time is left out."""

    run_id: str
    scenario_id: str
    scenario_digest: str
    config: Config
    contract: Contract
    seed: int
    topology: str
    model_adapter: str
    model: str
    cassette_digest: str | None = None
    parent_run_id: str | None = None
    branch_point: int | None = None
    params: JsonValue = None
    """The values the run used (0009), as the harness recorded them."""


@dataclass(frozen=True)
class Destination:
    """The row of `sim.run`: what the destination offered and promised (0005)."""

    scenario_id: str
    task_id: str
    capabilities: frozenset[str]
    params: ContractParams
    last_vt: int = 0
    """The virtual time of the simulator's last event."""

    @property
    def settle_ms(self) -> int:
        """S: a request sent at `s` has committed, or never will, by `s + S` (0005)."""
        return self.params.settle_ms


@dataclass(frozen=True)
class Effect:
    """A row of `sim.effect`: one committed write (0004)."""

    effect_id: str
    intent_id: str
    operation: Operation
    args: Mapping[str, object]
    approval_id: str
    basis: Mapping[str, int]
    idempotency_key: str | None
    client_ref: str | None
    received_seq: int
    commit_seq: int
    committed_vt: int


APPROVAL_STATUSES = frozenset({"pending", "approved", "denied", "revoked"})
"""The statuses `sim.approval` stores. Expiry is never stored (0002, rule 8)."""


@dataclass(frozen=True)
class Approval:
    """A row of `sim.approval`: one approval request and what became of it (0002).

    `status` is as stored: pending, approved, denied or revoked. A revoked approval was
    approved first. An approved approval expires at `expires_at`, which needs no change to
    the row. `decided_by` is `policy` for a decision on arrival (rule 6), otherwise the
    actor of the human approver who decided (rule 7). The seqs are simulator events.
    """

    approval_id: str
    intent_id: str
    operation: Operation
    scope: Mapping[str, object]
    requested_seq: int
    requested_vt: int
    status: str
    decided_seq: int | None = None
    decided_vt: int | None = None
    decided_by: str | None = None
    expires_at: int | None = None
    revoked_seq: int | None = None
    revoked_vt: int | None = None
    revoked_by: str | None = None

    @property
    def decision(self) -> Decision | None:
        """The decision on the request by the end of the run, or None while pending."""
        if self.status not in APPROVAL_STATUSES:
            raise RecordsError(f"approval {self.approval_id} has no known status: {self.status!r}")
        if self.status == "pending":
            return None
        if self.decided_seq is None:
            raise RecordsError(
                f"approval {self.approval_id} is {self.status} but was never decided"
            )
        return Decision.DENY if self.status == "denied" else Decision.APPROVE

    def decision_before(self, seq: int) -> Decision | None:
        """The decision as it stood before simulator event `seq`: None if still pending."""
        decision = self.decision
        if decision is None or self.decided_seq is None or self.decided_seq >= seq:
            return None
        return decision

    @property
    def revoked(self) -> bool:
        """Whether the approval was revoked by the end of the run."""
        return self.status == "revoked"

    def revoked_before(self, seq: int) -> bool:
        """Whether the approval was revoked before simulator event `seq`."""
        if self.revoked and self.revoked_seq is None:
            raise RecordsError(f"approval {self.approval_id} is revoked but has no revocation")
        return self.revoked_seq is not None and self.revoked_seq < seq

    @property
    def decided_by_policy(self) -> bool:
        """Whether policy decided the request on arrival, not a human (0002, rule 6)."""
        return self.decided_by == POLICY_ACTOR


@dataclass(frozen=True)
class FactVersion:
    """A row of `sim.fact_version`: one version of a fact, valid from simulator event
    `valid_from_seq` (0004).

    `cause` is `initial` (version 1, from the scenario), `effect` (the consequence of the
    effect `cause_effect_id`) or `control` (a change through the simulator's control
    interface, such as fault F7's).
    """

    key: str
    version: int
    value: Mapping[str, object]
    valid_from_seq: int
    valid_from_vt: int
    cause: str
    cause_effect_id: str | None = None


@dataclass(frozen=True)
class Read:
    """A row of `sim.read_log`: version `version` of fact `key`, served to `caller` at
    simulator event `seq` (0004)."""

    seq: int
    key: str
    version: int
    caller: str
    vt: int


@dataclass(frozen=True)
class Message:
    """A row of `harness.message_log`: one message on one hop, and its fate (0004).

    `seq` numbers its send and `delivered_seq` its delivery, on the harness's one event
    counter. `correlation` is, for an answer, the seq of the request it answers.
    """

    seq: int
    hop: str
    kind: str
    sender: str
    receiver: str
    api: str | None
    body: object
    sent_vt: int
    correlation: int | None = None
    intent_id: str | None = None
    idempotency_key: str | None = None
    answer_class: str | None = None
    fate: str | None = None
    drop_reason: str | None = None
    delivered_seq: int | None = None
    delivered_vt: int | None = None

    def delivered_before(self, seq: int) -> bool:
        """Whether the message was delivered before harness event `seq`."""
        return (
            self.fate == "delivered" and self.delivered_seq is not None and self.delivered_seq < seq
        )


@dataclass(frozen=True)
class Fault:
    """A row of `harness.fault_log`: a fault that fired (0006).

    `voided` holds the seqs of messages whose delivery a crash voided.
    """

    seq: int
    fault: str
    point: str
    occurrence: int
    action: str
    vt: int
    variant: str | None = None
    target_seq: int | None = None
    component: str | None = None
    voided: tuple[int, ...] = ()
    params: Mapping[str, object] | None = None


@dataclass(frozen=True)
class ActorAction:
    """A row of `harness.actor_log`: an action of the scripted approver or resolver."""

    seq: int
    actor: str
    action: str
    subject: str
    details: Mapping[str, object]
    vt: int


@dataclass(frozen=True)
class SubmittedReport:
    """A row of `harness.final_report`: a final report the stack submitted (0004).

    `message_seq` is the message that carried it to the component that accepted it.
    """

    seq: int
    worker: str
    report: object
    valid: bool
    accepted_by: str
    vt: int
    message_seq: int | None = None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunRecords:
    """Everything the evaluator reads about one run.

    The tuples may come in any order; the checks sort what they need.
    """

    run: RunInfo
    scenario: Scenario
    destination: Destination
    effects: tuple[Effect, ...] = ()
    messages: tuple[Message, ...] = ()
    faults: tuple[Fault, ...] = ()
    actor_actions: tuple[ActorAction, ...] = ()
    reports: tuple[SubmittedReport, ...] = ()
    approvals: tuple[Approval, ...] = ()
    fact_versions: tuple[FactVersion, ...] = ()
    reads: tuple[Read, ...] = ()

    def end_vt(self) -> int:
        """The virtual time of the run's end, as far as the records show it.

        The harness does not record the stop itself, so this is the latest virtual time
        on record: the simulator's last event, and every message, fault, actor action and
        report. Nothing in the run changed after it, except the passage of time.
        """
        times = [self.destination.last_vt]
        for message in self.messages:
            times.append(message.sent_vt)
            if message.delivered_vt is not None:
                times.append(message.delivered_vt)
        times.extend(fault.vt for fault in self.faults)
        times.extend(action.vt for action in self.actor_actions)
        times.extend(report.vt for report in self.reports)
        times.extend(effect.committed_vt for effect in self.effects)
        return max(times)

    def check_scenario(self) -> None:
        """Refuse records that belong to another scenario than the one given, or to a
        version of it that has changed since the run: the digests must match."""
        expected = (self.scenario.id, self.scenario.id, self.scenario.task_id)
        found = (self.run.scenario_id, self.destination.scenario_id, self.destination.task_id)
        if found != expected:
            raise RecordsError(
                f"the run is of scenario {self.run.scenario_id} (simulator:"
                f" {self.destination.scenario_id}, task {self.destination.task_id}), but the"
                f" scenario given is {self.scenario.id} (task {self.scenario.task_id})"
            )
        digest = scenario_digest(self.scenario)
        if self.run.scenario_digest != digest:
            raise RecordsError(
                f"the run's scenario {self.run.scenario_id} has digest"
                f" {self.run.scenario_digest}, but the scenario given has {digest}: it"
                " changed since the run"
            )
