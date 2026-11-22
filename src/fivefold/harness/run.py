"""One run in the inproc topology: every component on one scheduler (0009).

A run is a scenario, a configuration, a contract level, a seed and a model (0009). This
build runs B0, G1 and G2 with the scripted model, recorded into a cassette or not, with a
cassette's replay, with a live model, always recorded, or as an experimental branch of a
recorded run, which replays the parent's first K model calls and then switches to the
scripted model or a live one, also always recorded (`fivefold.harness.models`). A branch
runs its parent's run: the same scenario, configuration, contract level, seed, fault plan
and workers. It is a new run all the same, with its own run id, the parent's with
`.branch-K` added, and `harness.run` names its parent and its branch point.

The run's fault plan is fixed before anything runs (`run_plan`): the run's own plan, or
else the scenario's, is a list of faults, checked against the run, or a random plan,
from which the run's `faults` generator draws such a list (0006, Plans;
`fivefold.faults.plan`). Either way, the list, with the parameters drawn from the seed,
is what fires and what `harness.run` records, so playback, branches and the report see
the faults the run had.

`InprocRun.execute`:

1. records the run, in its run database and in the run registry (0012,
   `fivefold.harness.registry`), sets up the simulator for the contract with the
   configuration's credentials (only the gateway in G1 and G2, the agent workers in B0;
   0008), and serves the simulator's API and, in B0, the report sink on the transport;
2. in G1 and G2, starts the gateway: its store, its dispatcher after startup recovery, and
   its tools, one MCP server per agent connection. G1's store has action identity off,
   which makes the gateway authorization only (0008);
3. starts each agent worker with its own checkpoint thread, and the scripted approver,
   which decides queued approval requests by the scenario's rules (0002, rule 7). F9
   starts one more worker on the same task when it fires; the gateway expects a report
   from every worker the plan can start (0010);
4. fires the plan's faults: the transport fires those on its hops, the dispatcher those
   at its points (F1, F6 and F7 early), and this module those at the agent's points (F1's
   B0 equivalent, F11). A crashed gateway restarts through startup recovery (0003), and
   each agent worker then opens a new MCP connection to it; a crashed worker restarts
   from its last checkpoint. A component that a crash stopped reaches no further fault
   point, at the gateway's points and the agent's alike: when two crash faults meet at
   one event, only the first fires, and the component restarts after its delay. The
   simulator's endpoint fires F5 and F10 at the simulator's points
   (`fivefold.harness.sim_points`). Each fault's record names the task intent it
   touched, which recovery latency starts from (0014);
5. runs until the agents' phase ends: every worker finished, used up its step budget or
   failed;
6. in G2, closes the run if the last report did not (0003); then keeps everything but
   the agents running for the drain window, `2 x (D + max_hop_latency) + L` (0009),
   records the stop time, and a recording's cassette digest, and stops every component.
   A replay that did not serve exactly its cassette's calls then fails the run with
   `ReplayError` (0009);
7. runs the evaluator, which scores the run from the simulator's and the harness's
   records and writes its result to the `eval` schema (0004, 0009).

When the drain ends, before the stop is recorded, the harness has the simulator abort
every write still in flight past its deadline, unless a fault waived the deadline (F5
beyond the deadline): such a write never commits, and the ledger then says so (0005).

In G modes, when the scenario has a scripted resolver (0009), the gateway's dispatcher
tells it when an action is left to a human, and it attests what the simulator's ledger
shows through the gateway's admin path (`fivefold.harness.actors`, 0003).

Each component connects to the run database as its own role: `ff_sim`, `ff_gw`,
`ff_harness`, `ff_agent` and `ff_eval` (0008). The registry's row is written as
`ff_registry`, in the registry's own database on the same server.

Every model call of every worker goes into the harness's model-call log once the model
answers, with its virtual times, the run's model and the reply's token usage (0004). Each
worker has the scenario's step and token budgets (0009, `fivefold.agents.worker`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import psycopg
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from psycopg.conninfo import make_conninfo

from fivefold.agents.checkpoint import InlinePostgresSaver, checkpoint_serializer, connect
from fivefold.agents.connection import GatewayConnection
from fivefold.agents.live import Think, refusal
from fivefold.agents.policy import Mode, scripted_policy
from fivefold.agents.scripted import ScriptedModel
from fivefold.agents.tools import Timeouts, baseline_tools, gateway_tools
from fivefold.agents.worker import CallLog, WorkerEnd, WorkerSpec, build_graph, run_worker
from fivefold.domain.contracts import Capability, Contract
from fivefold.domain.scenario import Scenario, scenario_digest
from fivefold.evaluator import RunResult as Evaluation
from fivefold.evaluator import evaluate_run
from fivefold.faults.catalog import Action
from fivefold.faults.plan import (
    FaultInjector,
    Fired,
    PlannedFault,
    check_configuration,
    check_run,
    draw_plan,
    parse_plan,
    parse_random_plan,
    seed_parameters,
)
from fivefold.gateway.admin import GatewayAdmin
from fivefold.gateway.dispatcher import Dispatcher, Timing
from fivefold.gateway.freshness import RuleFreshness
from fivefold.gateway.pg_store import PgStore
from fivefold.gateway.server import serve
from fivefold.gateway.sim_client import SimClient
from fivefold.gateway.state_machine import GatewaySettings
from fivefold.gateway.tools import GatewayTools, ToolTiming
from fivefold.harness.actors import PgEffectsLedger, ScriptedApprover, ScriptedResolver
from fivefold.harness.clock import settle, wall_ms
from fivefold.harness.fault_effects import SimulatorFaults, message_context
from fivefold.harness.log import FaultRecord, ModelCallRecord, PgHarnessLog, RunRecord
from fivefold.harness.mcp_link import Link
from fivefold.harness.models import (
    SCRIPTED,
    Branch,
    ModelSpec,
    Parent,
    RunModels,
    Scripted,
    branch_run_id,
    parent_of,
)
from fivefold.harness.registry import RegistryEntry, database_of, register
from fivefold.harness.reports import ReportSink, sink_endpoint
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import ComponentFailed, Scheduler
from fivefold.harness.sim_points import SimulatorPoints
from fivefold.harness.transport import Envelope, Outgoing, Stage, Transport
from fivefold.recorder.cassette import RunHeader, plain_object
from fivefold.sim import SimConfig, Simulator
from fivefold.sim.endpoint import endpoint

if TYPE_CHECKING:
    from fivefold.harness.dbos_worker import DBOSWorkers
    from fivefold.harness.temporal_worker import TemporalHost, TemporalWorkers

Config = Literal["B0", "B1", "B2", "G1", "G2"]

GATEWAY_CONFIGS = ("G1", "G2")
"""The configurations that write through the gateway (0008). G1 is authorization only."""

ORIGINAL_CONFIGS: tuple[Config, ...] = ("B0", "G1", "G2")
CONFIGS: tuple[Config, ...] = (*ORIGINAL_CONFIGS, "B1", "B2")
"""Every configuration the in-process harness can run (0008, 0018, 0020)."""

GATEWAY = "gateway"
SIMULATOR = "simulator"
REPORT_SINK = "report_sink"

POSSIBLY_SENT_POINT = "gw.dispatch.after_record"
"""A gateway crash here leaves a request that may or may not have left (0006, crashes)."""

VOIDING_POINT = "gw.dispatch.after_answer"
"""A gateway crash here voids the answer it just received (0006, crashes)."""

GATEWAY_TOOL_NAMES = frozenset(
    {
        "get_quote",
        "list_suppliers",
        "get_budget",
        "get_inventory",
        "submit_purchase_order",
        "reserve_inventory",
        "get_action_status",
        "submit_final_report",
    }
)
BASELINE_TOOL_NAMES = frozenset(
    {
        "get_quote",
        "list_suppliers",
        "get_budget",
        "get_inventory",
        "request_approval",
        "get_approval_status",
        "submit_purchase_order",
        "reserve_inventory",
        "get_order_status",
        "get_reservation_status",
        "submit_final_report",
    }
)


@dataclass(frozen=True)
class RunSpec:
    """What to run: `plan` defaults to the scenario's fault plan. A plan is a list of
    faults, or a random plan, `{random: {from: [...], max: N}}`, which the run draws from
    its seed (`run_plan`)."""

    scenario: Scenario
    config: Config
    contract: Contract
    seed: int
    plan: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None
    workers: int = 1
    model: ModelSpec = SCRIPTED
    """The model adapter: scripted, scripted with recording, replay, live, or a branch's
    (0009)."""

    @property
    def parent(self) -> Parent | None:
        """For an experimental branch, or a replay of one, the run it branched from and
        its branch point (0009); None for any other run."""
        return parent_of(self.model)

    @property
    def run_id(self) -> str:
        """A readable id that names the run's choices. A branch's is its parent's run id
        with `.branch-K` added, K being its branch point (`branch_run_id`)."""
        parent = self.parent
        if parent is not None:
            return branch_run_id(parent)
        return f"{self.scenario.id}.{self.config}.{self.contract}.{self.seed}"


@dataclass
class RunResult:
    """How a run ended: each worker's end, the virtual time the run stopped, and the
    evaluator's result."""

    run_id: str
    ends: dict[str, tuple[WorkerEnd, str]] = field(default_factory=dict)
    stopped_vt: int = 0
    evaluation: Evaluation | None = None


def drain_window_ms(scenario: Scenario) -> int:
    """`2 x (D + max_hop_latency) + L` (0009)."""
    params = scenario.params
    return 2 * (params.commit_deadline_ms + params.max_hop_latency_ms) + params.status_lag_ms


def run_plan(spec: RunSpec) -> tuple[PlannedFault, ...]:
    """The run's fault plan, as it fires and as `harness.run` records it (0006, Plans).

    The run's own plan, or else the scenario's, is either a list of faults, which must
    fit the run's configuration, timing and contract level, or a random plan, from which
    the run's `faults` generator draws such a list (`fivefold.faults.plan.draw_plan`).
    Then the same generator draws each seeded parameter the list leaves unset (0009). So
    a seed always gives the same plan, and a list with no random part draws what it
    always did. Raises `ValueError` for a plan the run cannot carry out.
    """
    faults = spec.plan if spec.plan is not None else spec.scenario.faults
    draws = Generators(spec.seed).get("faults")
    if isinstance(faults, Mapping):
        plan = draw_plan(
            parse_random_plan(faults),
            draws,
            scenario=spec.scenario,
            config=spec.config,
            contract=spec.contract,
        )
    else:
        plan = parse_plan(faults)
        check_configuration(plan, gateway=spec.config in GATEWAY_CONFIGS)
        check_run(plan, params=spec.scenario.params, contract=spec.contract)
    return seed_parameters(plan, draws)


class InprocRun:
    """One run of a scenario on one scheduler, in a migrated run database."""

    def __init__(
        self,
        spec: RunSpec,
        dsn: str,
        *,
        temporal_host: TemporalHost | None = None,
        admin_dsn: str | None = None,
    ) -> None:
        """`dsn` names the run database; each component connects with its own role."""
        if spec.config not in CONFIGS:
            raise ValueError(f"this build runs {', '.join(CONFIGS)}, not {spec.config}")
        if spec.config == "B1" and (not isinstance(spec.model, Scripted) or spec.model.record):
            raise ValueError("B1 runs the scripted model without a cassette (0018)")
        if spec.config == "B2" and (not isinstance(spec.model, Scripted) or spec.model.record):
            raise ValueError("B2 runs the scripted model without a cassette (0020)")
        self._admin_dsn = admin_dsn
        self._temporal_host = temporal_host
        self._temporal_workers: TemporalWorkers | None = None
        self._dbos_workers: DBOSWorkers | None = None
        self.spec = spec
        self._plan = run_plan(spec)
        self._dsn = dsn
        self.scheduler = Scheduler()
        self.result = RunResult(spec.run_id)
        self._workers = list(_worker_names(spec.workers))
        self._ranks: dict[str, int] = {}
        self._agent_workers = spec.workers + sum(
            1 for entry in self._plan if entry.kind.action is Action.START_WORKER
        )
        gateway = spec.config in GATEWAY_CONFIGS
        self._tool_names = GATEWAY_TOOL_NAMES if gateway else BASELINE_TOOL_NAMES
        self._closing: list[Any] = []
        self._last_write_answers: dict[tuple[str, str], int] = {}
        self._params: dict[str, object] = {
            "params": spec.scenario.params.model_dump(mode="json"),
            "plan": [entry.model_dump(mode="json") for entry in self._plan],
            "workers": spec.workers,
            "agent_workers": self._agent_workers,
        }
        """The values the run uses, as `harness.run.params` records them (0009)."""
        if isinstance(spec.model, Branch):
            _check_branch(spec, self._params, spec.model.parent.run)
        self.models = RunModels(spec.model)
        self._model_calls = 0

    def _conninfo(self, role: str) -> str:
        return make_conninfo(self._dsn, user=role)

    async def execute(self) -> RunResult:
        """Run to the end of the drain window; return how each worker ended."""
        if self.spec.config == "B2":
            return await self._execute_dbos()
        if self.spec.config != "B1":
            return await self._run_and_score()
        try:
            from fivefold.harness.temporal_worker import (
                TemporalHost,
                TemporalInfrastructureError,
                TemporalWorkers,
            )
        except ModuleNotFoundError as error:
            if error.name != "temporalio":
                raise
            raise RuntimeError("Install fivefold[temporal] to run B1") from error
        host_context = (
            nullcontext(self._temporal_host) if self._temporal_host is not None else TemporalHost()
        )
        with host_context as host:
            self._temporal_workers = TemporalWorkers(self, host)
            try:
                return await self._run_and_score()
            except ComponentFailed as error:
                if isinstance(error.__cause__, TemporalInfrastructureError):
                    raise error.__cause__ from None
                raise

    async def _execute_dbos(self) -> RunResult:
        import sys
        from importlib.util import find_spec

        if sys.platform not in ("linux", "darwin"):
            raise RuntimeError("B2 runs on Linux and macOS (0020)")
        if find_spec("dbos") is None:
            raise RuntimeError("Install fivefold[dbos] to run B2")
        from fivefold.harness.dbos_worker import (
            DBOSInfrastructureError,
            DBOSWorkers,
            state_database,
        )

        admin = self._admin_dsn or make_conninfo(self._dsn, dbname="postgres")
        with state_database(admin, _worker_names(self._agent_workers)) as state:
            self._dbos_workers = DBOSWorkers(self, state)
            try:
                return await self._run_and_score()
            except ComponentFailed as error:
                if isinstance(error.__cause__, DBOSInfrastructureError):
                    raise error.__cause__ from None
                raise
            finally:
                await self._dbos_workers.stop()

    async def _run_and_score(self) -> RunResult:
        try:
            await self._run()
        finally:
            await self._stop()
        self.models.check()
        self.result.evaluation = evaluate_run(self._conninfo("ff_eval"), self.spec.scenario)
        return self.result

    async def _run(self) -> None:
        spec, scenario = self.spec, self.spec.scenario
        params = scenario.params
        harness_conn = psycopg.connect(self._conninfo("ff_harness"), autocommit=True)
        self._closing.append(harness_conn)
        self.log = PgHarnessLog(harness_conn)
        plan = self._plan
        parent = spec.parent
        self.log.run_recorded(
            RunRecord(
                run_id=spec.run_id,
                scenario_id=scenario.id,
                scenario_digest=scenario_digest(scenario),
                config=spec.config,
                contract=str(spec.contract),  # type: ignore[arg-type]
                seed=spec.seed,
                model_adapter=self.models.adapter,
                model=self.models.name,
                cassette_digest=self.models.cassette_digest,
                params=self._params,
                parent_run_id=None if parent is None else parent.run_id,
                branch_point=None if parent is None else parent.branch_point,
            )
        )
        register(
            self._dsn,
            RegistryEntry(
                database=database_of(self._dsn),
                run_id=spec.run_id,
                scenario_id=scenario.id,
                config=spec.config,
                contract=str(spec.contract),
                seed=spec.seed,
            ),
        )
        self.faults = FaultInjector(plan)
        generators = Generators(spec.seed)
        self.transport = Transport(
            self.scheduler,
            self.log,
            generators.get("latency"),
            max_hop_latency_ms=params.max_hop_latency_ms,
            faults=self.faults,
        )
        self.transport.observe(self._agent_points)
        self.transport.observe(self._gateway_answers)
        self.transport.before_send(self._before_tool_call)
        gateway = spec.config in GATEWAY_CONFIGS
        credentials = (GATEWAY,) if gateway else _worker_names(self._agent_workers)
        config = SimConfig.preset(
            spec.contract, credentials=credentials, params=params.contract_params()
        )
        self.simulator = Simulator.setup(self._conninfo("ff_sim"), scenario, config, vt=0)
        self._closing.append(self.simulator)
        self.approver = ScriptedApprover(
            self.scheduler, self.simulator.control, self.log, scenario.authority
        )
        self.resolver = self._resolver(PgEffectsLedger(harness_conn))
        self.effects = SimulatorFaults(self.scheduler, self.simulator.control, self.log, scenario)
        self.transport.apply_faults_with(
            lambda fired, envelope: self.effects.apply(fired, message_context(envelope.body))
        )
        self.transport.serve(
            SIMULATOR,
            endpoint(
                self.simulator,
                self.scheduler,
                poll_ms=params.approval_poll_interval_ms,
                max_wait_ms=params.max_wait_ms,
                served=[self.approver.served],
                points=self._simulator_points(),
            ),
        )
        intents = {intent.id: str(intent.operation) for intent in scenario.intents}
        self._intents = intents
        self.sink = ReportSink(self.scheduler, self.log, intents)
        if not gateway:
            self.transport.serve(REPORT_SINK, sink_endpoint(self.sink))
        if self._temporal_workers is None and self._dbos_workers is None:
            checkpoint_conn = connect(self._conninfo("ff_agent"))
            self._closing.append(checkpoint_conn)
            self.checkpointer = InlinePostgresSaver(checkpoint_conn, serde=checkpoint_serializer())
            self.checkpointer.setup()
        self.timeouts = Timeouts(
            t_ms=params.dispatch_timeout_ms,
            tool_wait_ms=params.tool_wait_ms,
            max_wait_ms=params.max_wait_ms,
        )
        if gateway:
            self._start_gateway()
        for worker in self._workers:
            self._start_worker(worker, resume=False)
        await self.scheduler.run(until=self._agents_done)
        self._close_run()
        drain_end = self.scheduler.now + drain_window_ms(scenario)
        await self.scheduler.run(deadline_ms=drain_end)
        # The drain lasts its whole window (0009). The scheduler returns as soon as nothing
        # is queued and leaves the clock there, so a quiet drain moves it to the end here.
        self.scheduler.clock.advance_to(max(self.scheduler.now, drain_end))
        self.simulator.control.abort_overdue(vt=self.scheduler.now)
        self.result.stopped_vt = self.scheduler.now
        self.log.run_stopped(self.scheduler.now)
        self._models_stopped()

    def _agents_done(self) -> bool:
        return len(self.result.ends) == len(self._workers)

    def _simulator_points(self) -> SimulatorPoints:
        """F5 and F10, at the simulator's fault points (0006). They also watch the status
        answers go out, to log each one F10 changed (0014)."""
        params = self.spec.scenario.params
        points = SimulatorPoints(
            self.scheduler,
            self.faults,
            self.log,
            dispatch_timeout_ms=params.dispatch_timeout_ms,
            status_lag_ms=params.status_lag_ms,
        )
        self.transport.observe(points.answer_sent)
        return points

    # The gateway --------------------------------------------------------------------------

    def _start_gateway(self) -> None:
        """Start the gateway: its store, startup recovery and its dispatcher (0003).

        Also how a crashed gateway restarts: everything it had in memory is new.
        """
        scenario, params = self.spec.scenario, self.spec.scenario.params
        intents = self._intents
        settings = GatewaySettings(
            approval_wait_ms=scenario.authority.approval_wait_ms,
            params=params.contract_params(),
            dispatch_timeout_ms=params.dispatch_timeout_ms,
            max_attempts=params.max_attempts,
            max_probes=params.max_probes,
            agent_workers=self._agent_workers,
        )
        self.store = PgStore(
            self._conninfo("ff_gw"),
            task_id=scenario.task_id,
            intents=intents,
            contract=self.spec.contract,
            settings=settings,
            identity=self.spec.config == "G2",
        )
        self._closing.append(self.store)
        client = SimClient(self.transport, timeout_ms=params.dispatch_timeout_ms)
        self.dispatcher = Dispatcher(
            self.scheduler,
            self.store,
            client,
            RuleFreshness(self.store),
            component=GATEWAY,
            timing=Timing(
                approval_poll_ms=params.approval_poll_interval_ms,
                probe_interval_ms=params.status_probe_interval_ms,
            ),
            faults=self.faults,
            on_fault=self._gateway_fault,
            on_escalated=self._escalated,
        )
        self.tools = GatewayTools(
            self.scheduler,
            self.store,
            self.dispatcher,
            client,
            self.sink,
            task_id=scenario.task_id,
            intents=intents,
            timing=ToolTiming(tool_wait_ms=params.tool_wait_ms, max_wait_ms=params.max_wait_ms),
        )
        self.dispatcher.start()

    def _close_run(self) -> None:
        """In G2, once the agents' phase is over, close the run if no report did (0003).

        A gateway that is down then closes it when it restarts.
        """
        if self.spec.config != "G2" or not self.scheduler.is_up(GATEWAY):
            return
        if not self.store.snapshot().run_closed:
            self.store.close_run(self.scheduler.now)

    def _gateway_fault(self, fired: Fired, point: str, context: Mapping[str, object]) -> None:
        """A fault at a dispatch point: record it, then apply it: F6 and F7 early through
        the simulator, F1 and F3 by crashing the gateway.

        What the crash changes for the evidence goes in the fault's record (0006, crashes;
        0004, property 5). A crash after the attempt is recorded leaves a request that may
        have left: the record names it. A crash after the answer arrived voids that answer,
        which the gateway never recorded.
        """
        action = fired.planned.kind.action
        params: dict[str, object] = dict(fired.planned.params)
        voided: tuple[int, ...] = ()
        if action is Action.KILL and point == POSSIBLY_SENT_POINT:
            params["request"] = {
                "intent_id": context["intent_id"],
                "api": context["api"],
                "idempotency_key": context["idempotency_key"],
            }
        if action is Action.KILL and point == VOIDING_POINT:
            answer = self._last_write_answers.get((str(context["api"]), str(context["intent_id"])))
            if answer is None:
                raise RuntimeError(f"{fired.fault} at {point}: no answer reached the gateway")
            voided = (answer,)
        self.log.fault_fired(
            FaultRecord(
                seq=self.log.next_seq(),
                fault=str(fired.fault),
                variant=fired.planned.variant,
                point=point,
                occurrence=fired.occurrence,
                action=str(action),
                vt=self.scheduler.now,
                component=GATEWAY,
                voided=voided,
                params=params,
                intent_id=_text(context.get("intent_id")),
            )
        )
        if action is Action.KILL:
            self._crash_gateway(fired.planned.param("restart_delay_ms"))
        else:
            self.effects.apply(fired, context)

    def _crash_gateway(self, restart_after_ms: int) -> None:
        """Stop the gateway now and start it again later (0006, crashes).

        Its memory goes, and so does its database connection, with any open
        transaction and the run's lock; its tasks are cancelled. The restart builds a new
        gateway, which recovers from its records before it serves anything (0003).
        """
        self.store.close()
        self.scheduler.crash(
            GATEWAY, restart_after_ms=restart_after_ms, restart=self._restart_gateway
        )

    def _restart_gateway(self) -> None:
        self._start_gateway()
        if self._agents_done():
            self._close_run()

    def _connect(self, worker: str) -> Link:
        """Open a new MCP connection from `worker` to the gateway.

        A gateway that is up serves it, with the tools of its current incarnation; one
        that is down never does.
        """
        link = Link(self.scheduler, self.transport, agent=worker, gateway=GATEWAY)
        if self.scheduler.is_up(GATEWAY):
            self.scheduler.spawn(GATEWAY, serve(self.tools, link), name=f"mcp server for {worker}")
        return link

    # The scripted resolver (0003, 0009) -----------------------------------------------------

    def _resolver(self, ledger: PgEffectsLedger) -> ScriptedResolver | None:
        """The scripted resolver, in G modes when the scenario has one (0009).

        It reads the simulator's ledger out of band through `ledger`, as `ff_harness`
        (0008). B0 has no gateway, so nothing there escalates.
        """
        rule = self.spec.scenario.authority.resolver
        if self.spec.config not in GATEWAY_CONFIGS or rule == "none":
            return None
        settle_ms = self.spec.scenario.params.contract_params().settle_ms
        return ScriptedResolver(
            self.scheduler, self._admin, ledger, self.log, rule, settle_ms=settle_ms
        )

    def _admin(self) -> GatewayAdmin | None:
        """The admin path of the gateway's current incarnation, or None while it is down."""
        if not self.scheduler.is_up(GATEWAY):
            return None
        return GatewayAdmin(self.scheduler, self.store, self.dispatcher)

    def _escalated(self, action_id: str) -> None:
        """The gateway left an escalated action to a human: tell the resolver, if any."""
        if self.resolver is not None:
            self.resolver.escalated(action_id)

    # Agent workers ------------------------------------------------------------------------

    def _start_worker(self, worker: str, *, resume: bool) -> None:
        label = "resumes" if resume else "starts"
        self.scheduler.spawn(worker, self._worker(worker, resume=resume), name=f"{worker} {label}")

    async def _worker(self, worker: str, *, resume: bool) -> None:
        durable_workers = self._temporal_workers or self._dbos_workers
        if durable_workers is not None:
            await durable_workers.work(worker, resume=resume)
            return
        scenario = self.spec.scenario
        model = self._chat_model(worker)
        budget = scenario.budget
        spec = WorkerSpec(worker, scenario.task, budget.max_agent_steps, budget.max_tokens)
        on_call = self._call_log(worker)

        def on_step(step: int) -> None:
            self._agent_step(worker, step)

        if self.spec.config == "B0":
            tools = baseline_tools(
                self.transport,
                self.scheduler,
                self.timeouts,
                worker=worker,
                status=self.spec.contract.has(Capability.STATUS),
            )
            graph = build_graph(
                model,
                tools,
                self.checkpointer,
                max_steps=spec.max_steps,
                max_tokens=spec.max_tokens,
                on_step=on_step,
                on_call=on_call,
            )
            self.result.ends[worker] = await run_worker(graph, spec, resume=resume)
            return
        connection = GatewayConnection(
            self.scheduler,
            lambda: self._connect(worker),
            worker=worker,
            timeout_ms=scenario.params.dispatch_timeout_ms,
        )
        try:
            listed = await connection.list_tools()
            tools = gateway_tools(connection, listed, self.timeouts)
            graph = build_graph(
                model,
                tools,
                self.checkpointer,
                max_steps=spec.max_steps,
                max_tokens=spec.max_tokens,
                on_step=on_step,
                on_call=on_call,
            )
            self.result.ends[worker] = await run_worker(graph, spec, resume=resume)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A worker that cannot connect, or fails otherwise, ends without a report (0008).
            self.result.ends[worker] = (WorkerEnd.ERROR, _describe(error))
        finally:
            # After a crash the link ends the connection itself, once in-flight requests
            # have reached the gateway; this closes it only when the worker finishes.
            connection.close()

    # Fault points of the agent (0006) -----------------------------------------------------

    def _agent_step(self, worker: str, step: int) -> None:
        """`agent.step`: F9 starts another worker on the same task (0006).

        The new worker runs the same scripted policy from the start, with its own
        checkpoint thread and step budget; in the `different_args` variant it chooses the
        second-cheapest acceptable option (0008).
        """
        for fired in self.faults.at("agent.step", {"worker": worker, "step": step}):
            if fired.planned.kind.action is not Action.START_WORKER:
                continue
            started = f"agent-{len(self._workers) + 1}"
            self.log.fault_fired(
                FaultRecord(
                    seq=self.log.next_seq(),
                    fault=str(fired.fault),
                    variant=fired.planned.variant,
                    point="agent.step",
                    occurrence=fired.occurrence,
                    action=str(Action.START_WORKER),
                    vt=self.scheduler.now,
                    component=started,
                    params={**fired.planned.params, "worker": worker, "step": step},
                    intent_id=None,
                )
            )
            self._workers.append(started)
            self._ranks[started] = 1 if fired.planned.variant == "different_args" else 0
            self._start_worker(started, resume=False)

    def _before_tool_call(self, outgoing: Outgoing) -> None:
        """F1's B0 equivalent: kill a worker just before it sends a tool call (0006).

        The call is never sent. The worker resumes from its last checkpoint, which comes
        before the tool node, so it sends the call once it resumes.
        """
        worker = outgoing.sender
        if outgoing.kind != "request" or worker not in self._workers:
            return
        if outgoing.api not in self._tool_names:
            return
        fields = {"api": outgoing.api, "tool": outgoing.api, "intent_id": outgoing.intent_id}
        for fired in self.faults.at("agent.before_tool_call", fields):
            if fired.planned.kind.action is not Action.KILL:
                continue
            self.log.fault_fired(
                FaultRecord(
                    seq=self.log.next_seq(),
                    fault=str(fired.fault),
                    variant=fired.planned.variant,
                    point="agent.before_tool_call",
                    occurrence=fired.occurrence,
                    action=str(Action.KILL),
                    vt=self.scheduler.now,
                    component=worker,
                    params=dict(fired.planned.params),
                    intent_id=outgoing.intent_id,
                )
            )
            self._kill(worker, fired.planned.param("restart_delay_ms"))
            return  # The worker is down, so it reaches no further fault (`_agent_points`).

    def _gateway_answers(self, envelope: Envelope, stage: Stage) -> None:
        """Note each write answer delivered to the gateway, by API and intent, so that a
        crash right after it (F3) can void it."""
        if (
            stage == "delivered"
            and envelope.hop == "gw_sim"
            and envelope.kind == "answer"
            and envelope.receiver == GATEWAY
            and envelope.api is not None
            and envelope.intent_id is not None
        ):
            self._last_write_answers[(envelope.api, envelope.intent_id)] = envelope.seq

    def _agent_points(self, envelope: Envelope, stage: Stage) -> None:
        """F11, and F3's B0 equivalent: kill a worker after it sends a tool call, or as a
        tool result reaches it."""
        if stage == "sent" and envelope.kind == "request":
            worker, point = envelope.sender, "agent.after_tool_call"
        elif stage == "delivered" and envelope.kind == "answer":
            worker, point = envelope.receiver, "agent.after_tool_result"
        else:
            return
        if worker not in self._workers or envelope.api not in self._tool_names:
            return
        fields = {"api": envelope.api, "tool": envelope.api, "intent_id": envelope.intent_id}
        for fired in self.faults.at(point, fields):
            if fired.planned.kind.action is not Action.KILL:
                continue
            delay = fired.planned.param("restart_delay_ms")
            voided = (envelope.seq,) if point == "agent.after_tool_result" else ()
            self.log.fault_fired(
                FaultRecord(
                    seq=self.log.next_seq(),
                    fault=str(fired.fault),
                    variant=fired.planned.variant,
                    point=point,
                    occurrence=fired.occurrence,
                    action=str(Action.KILL),
                    vt=self.scheduler.now,
                    target_seq=envelope.seq,
                    component=worker,
                    voided=voided,
                    params=dict(fired.planned.params),
                    intent_id=envelope.intent_id,
                )
            )
            # The kill is immediate. After a tool call, the request is already on the
            # wire; after a result, the worker must not see it: the transport hands a
            # delivery to its receiver only after the observers ran, and only if the
            # receiver is still up (0006, voided results).
            self._kill(worker, delay)
            # A worker that is down reaches no fault point, as the gateway's dispatcher
            # stops at the first crash: a second kill at this event does not fire, and
            # the worker restarts once, after this kill's delay.
            return

    def _kill(self, worker: str, restart_after_ms: int) -> None:
        self.scheduler.crash(
            worker,
            restart_after_ms=restart_after_ms,
            restart=lambda: self._start_worker(worker, resume=True),
        )

    # The model adapter (0009) -------------------------------------------------------------

    def _chat_model(self, worker: str) -> BaseChatModel:
        """The chat model of `worker`, built each time it starts or resumes, by the run's
        model adapter (`fivefold.harness.models`)."""
        scenario = self.spec.scenario
        mode = Mode.GATEWAY if self.spec.config in GATEWAY_CONFIGS else Mode.BASELINE
        rank = self._ranks.get(worker, 0)

        def think() -> Awaitable[None]:
            return self.scheduler.sleep(scenario.params.think_ms)

        def scripted(its_think: Think) -> BaseChatModel:
            return ScriptedModel(
                lambda: scripted_policy(
                    scenario.intents, mode=mode, rank=rank, max_wait_ms=scenario.params.max_wait_ms
                ),
                its_think,
            )

        return self.models.chat_model(
            worker, scripted=scripted, think=think, hold=self.scheduler.hold
        )

    def _call_log(self, worker: str) -> CallLog:
        """Log each model call of `worker` once it is answered (0004): its number in the
        run, its virtual times, the run's model, and the reply's token usage."""

        def asked() -> Callable[[AIMessage], None]:
            asked_vt, started = self.scheduler.now, wall_ms()

            def answered(reply: AIMessage) -> None:
                self._model_calls += 1
                usage = reply.usage_metadata
                self.log.model_called(
                    ModelCallRecord(
                        call_no=self._model_calls,
                        worker=worker,
                        asked_vt=asked_vt,
                        answered_vt=self.scheduler.now,
                        model=self.models.name,
                        input_tokens=None if usage is None else usage["input_tokens"],
                        output_tokens=None if usage is None else usage["output_tokens"],
                        refused=refusal(reply),
                        wall_ms=wall_ms() - started,
                    )
                )

            return answered

        return asked

    def _models_stopped(self) -> None:
        """The run has stopped: a recording's cassette is complete, and `harness.run`
        records its digest."""
        if self.log.run is None:
            raise RuntimeError("the run was never recorded")
        digest = self.models.stopped(self.log.run)
        if digest is not None:
            self.log.cassette_recorded(digest)

    # Stopping -------------------------------------------------------------------------------

    async def _stop(self) -> None:
        """Stop every component, let their tasks unwind, and close their connections."""
        for component in (*self._workers, GATEWAY, SIMULATOR, REPORT_SINK):
            if self.scheduler.is_up(component):
                self.scheduler.crash(component)
        await settle()
        for resource in reversed(self._closing):
            resource.close()


def _worker_names(count: int) -> tuple[str, ...]:
    """The names of a run's agent workers: `agent-1` to `agent-{count}`."""
    return tuple(f"agent-{number}" for number in range(1, count + 1))


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _describe(error: BaseException) -> str:
    """The error that ended a worker; the first one inside an exception group, as the
    MCP session's task group wraps them."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}"


def _check_branch(spec: RunSpec, params: Mapping[str, object], parent: RunHeader) -> None:
    """Refuse a branch that would not run its parent's run (0009): the same scenario, with
    the same digest, configuration, contract level, seed and topology, and the same
    values, which hold the fault plan with the parameters drawn from the seed, and the
    workers. Otherwise the branch would name a parent it does not continue."""
    scenario = spec.scenario
    mine: dict[str, object] = {
        "scenario_id": scenario.id,
        "scenario_digest": scenario_digest(scenario),
        "config": spec.config,
        "contract": str(spec.contract),
        "seed": spec.seed,
        "topology": "inproc",
        "params": plain_object(params, "the run's params"),
    }
    differ = [name for name, value in mine.items() if getattr(parent, name) != value]
    if differ:
        raise ValueError(
            f"a branch runs its parent's run, but it differs from {parent.run_id} in"
            f" {', '.join(differ)}"
        )
