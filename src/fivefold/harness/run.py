"""One run in the inproc topology: every component on one scheduler (0009).

A run is a scenario, a configuration, a contract level, a seed and a model (0009). This
build runs B0 and G2 with the scripted model. `InprocRun.execute`:

1. records the run, sets up the simulator for the contract with the configuration's
   credentials (only the gateway in G2, the agent workers in B0; 0008), and serves the
   simulator's API and, in B0, the report sink on the transport;
2. in G2, starts the gateway: its store, its dispatcher after startup recovery, and its
   tools, one MCP server per agent connection;
3. starts each agent worker with its own checkpoint thread, and the scripted approver,
   which decides queued approval requests by the scenario's rules (0002, rule 7);
4. fires the plan's faults: the transport fires those on its hops, and this module those
   at the agent's points (F11), with the restart from the worker's last checkpoint;
5. runs until the agents' phase ends: every worker finished, used up its step budget or
   failed;
6. in G2, closes the run if the last report did not (0003); then keeps everything but
   the agents running for the drain window, `2 x (D + max_hop_latency) + L` (0009), and
   stops every component;
7. runs the evaluator, which scores the run from the simulator's and the harness's
   records and writes its result to the `eval` schema (0004, 0009).

Each component connects to the run database as its own role: `ff_sim`, `ff_gw`,
`ff_harness`, `ff_agent` and `ff_eval` (0008).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import psycopg
from mcp.client.session import ClientSession
from psycopg.conninfo import make_conninfo

from fivefold.agents.checkpoint import InlinePostgresSaver, connect
from fivefold.agents.policy import Mode, scripted_policy
from fivefold.agents.scripted import ScriptedModel
from fivefold.agents.tools import Timeouts, baseline_tools, gateway_tools
from fivefold.agents.worker import WorkerEnd, WorkerSpec, build_graph, run_worker
from fivefold.domain.contracts import Capability, Contract
from fivefold.domain.scenario import Scenario, scenario_digest
from fivefold.evaluator import RunResult as Evaluation
from fivefold.evaluator import evaluate_run
from fivefold.faults.catalog import Action
from fivefold.faults.plan import FaultInjector, Fired, parse_plan
from fivefold.gateway.dispatcher import Dispatcher, Timing
from fivefold.gateway.freshness import RuleFreshness
from fivefold.gateway.pg_store import PgStore
from fivefold.gateway.server import serve
from fivefold.gateway.sim_client import SimClient
from fivefold.gateway.state_machine import GatewaySettings
from fivefold.gateway.tools import GatewayTools, ToolTiming
from fivefold.harness.actors import ScriptedApprover
from fivefold.harness.clock import settle
from fivefold.harness.fault_effects import SimulatorFaults, message_context
from fivefold.harness.log import FaultRecord, PgHarnessLog, RunRecord
from fivefold.harness.mcp_link import Link
from fivefold.harness.reports import ReportSink, sink_endpoint
from fivefold.harness.rng import Generators
from fivefold.harness.scheduler import Scheduler, SchedulerTimeout
from fivefold.harness.transport import Envelope, Stage, Transport, TransportTimeout
from fivefold.sim import SimConfig, Simulator
from fivefold.sim.endpoint import endpoint

Config = Literal["B0", "G2"]

GATEWAY = "gateway"
SIMULATOR = "simulator"
REPORT_SINK = "report_sink"

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
    """What to run: `plan` defaults to the scenario's fault list."""

    scenario: Scenario
    config: Config
    contract: Contract
    seed: int
    plan: Sequence[Mapping[str, Any]] | None = None
    workers: int = 1

    @property
    def run_id(self) -> str:
        """A readable id that names the run's choices."""
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


class InprocRun:
    """One run of a scenario on one scheduler, in a migrated run database."""

    def __init__(self, spec: RunSpec, dsn: str) -> None:
        """`dsn` names the run database; each component connects with its own role."""
        if spec.config not in ("B0", "G2"):
            raise ValueError(f"this build runs B0 and G2, not {spec.config}")
        self.spec = spec
        self._dsn = dsn
        self.scheduler = Scheduler()
        self.result = RunResult(spec.run_id)
        self._workers = [f"agent-{number}" for number in range(1, spec.workers + 1)]
        self._tool_names = GATEWAY_TOOL_NAMES if spec.config == "G2" else BASELINE_TOOL_NAMES
        self._closing: list[Any] = []

    def _conninfo(self, role: str) -> str:
        return make_conninfo(self._dsn, user=role)

    async def execute(self) -> RunResult:
        """Run to the end of the drain window; return how each worker ended."""
        spec, scenario = self.spec, self.spec.scenario
        params = scenario.params
        harness_conn = psycopg.connect(self._conninfo("ff_harness"), autocommit=True)
        self._closing.append(harness_conn)
        self.log = PgHarnessLog(harness_conn)
        plan = parse_plan(spec.plan if spec.plan is not None else _plan_of(scenario))
        self.log.run_recorded(
            RunRecord(
                run_id=spec.run_id,
                scenario_id=scenario.id,
                scenario_digest=scenario_digest(scenario),
                config=spec.config,
                contract=str(spec.contract),  # type: ignore[arg-type]
                seed=spec.seed,
                model_adapter="scripted",
                model="scripted",
                params={
                    "params": scenario.params.model_dump(mode="json"),
                    "plan": [entry.model_dump(mode="json") for entry in plan],
                    "workers": spec.workers,
                },
            )
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
        credentials = (GATEWAY,) if spec.config == "G2" else tuple(self._workers)
        config = SimConfig.preset(
            spec.contract, credentials=credentials, params=params.contract_params()
        )
        self.simulator = Simulator.setup(self._conninfo("ff_sim"), scenario, config, vt=0)
        self._closing.append(self.simulator)
        self.approver = ScriptedApprover(
            self.scheduler, self.simulator.control, self.log, scenario.authority
        )
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
                served=[self.approver.served],
            ),
        )
        intents = {intent.id: str(intent.operation) for intent in scenario.intents}
        self.sink = ReportSink(self.scheduler, self.log, intents)
        if spec.config == "B0":
            self.transport.serve(REPORT_SINK, sink_endpoint(self.sink))
        checkpoint_conn = connect(self._conninfo("ff_agent"))
        self._closing.append(checkpoint_conn)
        self.checkpointer = InlinePostgresSaver(checkpoint_conn)
        self.checkpointer.setup()
        self.timeouts = Timeouts(t_ms=params.dispatch_timeout_ms, tool_wait_ms=params.tool_wait_ms)
        if spec.config == "G2":
            self._start_gateway(intents)
        for worker in self._workers:
            self._start_worker(worker, resume=False)
        await self.scheduler.run(until=lambda: len(self.result.ends) == len(self._workers))
        if spec.config == "G2" and not self.store.snapshot().run_closed:
            self.store.close_run(self.scheduler.now)
        await self.scheduler.run(deadline_ms=self.scheduler.now + drain_window_ms(scenario))
        self.result.stopped_vt = self.scheduler.now
        await self._stop()
        self.result.evaluation = evaluate_run(self._conninfo("ff_eval"), scenario)
        return self.result

    # The gateway --------------------------------------------------------------------------

    def _start_gateway(self, intents: Mapping[str, str]) -> None:
        scenario, params = self.spec.scenario, self.spec.scenario.params
        settings = GatewaySettings(
            approval_wait_ms=scenario.authority.approval_wait_ms,
            params=params.contract_params(),
            dispatch_timeout_ms=params.dispatch_timeout_ms,
            max_attempts=params.max_attempts,
            max_probes=params.max_probes,
            agent_workers=self.spec.workers,
        )
        self.store = PgStore(
            self._conninfo("ff_gw"),
            task_id=scenario.task_id,
            intents=intents,
            contract=self.spec.contract,
            settings=settings,
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
        )
        self.tools = GatewayTools(
            self.scheduler,
            self.store,
            self.dispatcher,
            client,
            self.sink,
            task_id=scenario.task_id,
            intents=intents,
            timing=ToolTiming(tool_wait_ms=params.tool_wait_ms),
        )
        self.dispatcher.start()

    def _gateway_fault(self, fired: Fired, point: str, context: Mapping[str, object]) -> None:
        """A fault at a dispatch point: record it, then apply it (F6 and F7 early)."""
        self.log.fault_fired(
            FaultRecord(
                seq=self.log.next_seq(),
                fault=str(fired.fault),
                variant=fired.planned.variant,
                point=point,
                occurrence=fired.occurrence,
                action=str(fired.planned.kind.action),
                vt=self.scheduler.now,
                component=GATEWAY,
                params=dict(fired.planned.params),
            )
        )
        self.effects.apply(fired, context)

    # Agent workers ------------------------------------------------------------------------

    def _start_worker(self, worker: str, *, resume: bool) -> None:
        label = "resumes" if resume else "starts"
        self.scheduler.spawn(worker, self._worker(worker, resume=resume), name=f"{worker} {label}")

    async def _worker(self, worker: str, *, resume: bool) -> None:
        scenario = self.spec.scenario
        mode = Mode.GATEWAY if self.spec.config == "G2" else Mode.BASELINE
        rank = 0  # F9's different_args variant (M2) gives the second worker rank 1.
        model = ScriptedModel(
            lambda: scripted_policy(scenario.intents, mode=mode, rank=rank),
            lambda: self.scheduler.sleep(scenario.params.think_ms),
        )
        spec = WorkerSpec(worker, scenario.task, scenario.budget.max_agent_steps)
        if self.spec.config == "B0":
            tools = baseline_tools(
                self.transport,
                self.scheduler,
                self.timeouts,
                worker=worker,
                status=self.spec.contract.has(Capability.STATUS),
            )
            graph = build_graph(model, tools, self.checkpointer, max_steps=spec.max_steps)
            self.result.ends[worker] = await run_worker(graph, spec, resume=resume)
            return
        link = Link(self.scheduler, self.transport, agent=worker, gateway=GATEWAY)
        self.scheduler.spawn(GATEWAY, serve(self.tools, link), name=f"mcp server for {worker}")
        try:
            async with ClientSession(link.client_read, link.client_write) as session:
                await self._within(session.initialize(), "initialize")
                listed = await self._within(session.list_tools(), "tools/list")
                tools = gateway_tools(session, listed, self.scheduler, self.timeouts)
                graph = build_graph(model, tools, self.checkpointer, max_steps=spec.max_steps)
                self.result.ends[worker] = await run_worker(graph, spec, resume=resume)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A worker that cannot connect, or fails otherwise, ends without a report (0008).
            self.result.ends[worker] = (WorkerEnd.ERROR, _describe(error))
        finally:
            # After a crash the link ends the connection itself, once in-flight requests
            # have reached the gateway; this closes it only when the worker finishes.
            link.close_after_flight(worker, link.server_read)

    async def _within[T](self, call: Awaitable[T], what: str) -> T:
        """Wait for an MCP call outside a tool for at most T, as for a tool call (0005)."""
        timeout_ms = self.spec.scenario.params.dispatch_timeout_ms
        try:
            return await self.scheduler.wait_for(call, timeout_ms)
        except SchedulerTimeout:
            raise TransportTimeout(f"{what}: no answer within {timeout_ms} ms") from None

    # Fault points of the agent (0006) -----------------------------------------------------

    def _agent_points(self, envelope: Envelope, stage: Stage) -> None:
        """F11: kill a worker after it sends a tool call, or as a tool result reaches it."""
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
                )
            )
            # The kill is immediate. After a tool call, the request is already on the
            # wire; after a result, the worker must not see it: the transport hands a
            # delivery to its receiver only after the observers ran, and only if the
            # receiver is still up (0006, voided results).
            self._kill(worker, delay)

    def _kill(self, worker: str, restart_after_ms: int) -> None:
        self.scheduler.crash(
            worker,
            restart_after_ms=restart_after_ms,
            restart=lambda: self._start_worker(worker, resume=True),
        )

    # Stopping -------------------------------------------------------------------------------

    async def _stop(self) -> None:
        """Stop every component, let their tasks unwind, and close their connections."""
        for component in (*self._workers, GATEWAY, SIMULATOR, REPORT_SINK):
            if self.scheduler.is_up(component):
                self.scheduler.crash(component)
        await settle()
        for resource in reversed(self._closing):
            resource.close()


def _describe(error: BaseException) -> str:
    """The error that ended a worker; the first one inside an exception group, as the
    MCP session's task group wraps them."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}"


def _plan_of(scenario: Scenario) -> list[Mapping[str, Any]]:
    """The scenario's fault list; a random plan is not built yet."""
    if isinstance(scenario.faults, dict):
        raise ValueError("random fault plans are not built yet (0006)")
    return list(scenario.faults)
