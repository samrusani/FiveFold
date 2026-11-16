# 0009: Runs, scenarios and deterministic execution

Status: Accepted (2026-11-14)
Amended by: 0014 (2026-11-15): the determinism contract ignores wall-clock fields too; 0015 (2026-11-16): cap scenario status waits
Date: 2026-11-14
Spec sections affected: Simulator (determinism, scenarios), Playback and branching, Stack (packaging, `make demo`), Testing, Data model (Run)
Resolves M0 questions: 29, 31, 32, 33 (MCP transport), 41, 42 (scenario format); part of 38

## Context

The spec promises that a seed reproduces a run exactly, that demos run without an API key, and that branching restores snapshots made from template databases. A real LangGraph agent, a Postgres database and several services do not reproduce anything by default. This record defines what a run is and how it becomes deterministic.

## Decision

### A run

- A run is a scenario, a configuration, a contract level, a seed and a model. The model is scripted, a recorded cassette, or live.
- A run has one agent worker, or two when F9 starts a second (0006). Each worker has its own checkpoint thread and its own step and token budgets.
- Each run gets its own Postgres database, created from a migrated template database. A small registry database lists runs for the viewer.
- Ids inside a run come from per-run counters with readable prefixes, not from database sequences: `A-0001` actions, `PO-0001` and `RS-0001` effects, `R-0001` reads, `APR-0001` approvals, `ESC-0001` escalations.

### Topologies

| Topology | Used for | Deterministic |
| --- | --- | --- |
| inproc | Tests, demos and the live study | Yes, given the model's outputs |
| split | Bringing your own agent: the gateway (MCP over streamable HTTP), the simulator and Postgres run as separate Compose services | No; faults still fire on counters |

Checked in the mcp 2.3.0 source: the client can connect to a server object in-process, and the server supports the streamable HTTP transport. The inproc topology uses the in-process connection, so agent tool calls still pass through the MCP layer.

### The scheduler (inproc)

- One process, one thread, one asyncio event loop. Every interaction between components is a message through the scheduler: agent to gateway, gateway to simulator, scripted humans to services.
- The scheduler runs one component step to completion before starting the next. Components never run concurrently. Database calls happen inside steps, one after another.
- The agent runs as a coroutine. It is suspended while it waits for a tool result, and only the scheduler resumes it.
- Each model call advances the virtual clock by `think_ms` (default 1,000) for every adapter, so timing never depends on real model latency. Other components keep running during that time.
- Messages are delivered in order of (deliver_at, enqueue sequence). Each message's latency comes from the seeded generator, between 5 and `max_hop_latency` (50) virtual ms.
- Timeouts, probes, polls, approver delays and restart delays are timer events in virtual time.
- When nothing is ready, the clock jumps to the next event. A run full of 10-second timeouts takes milliseconds of wall time.

### Virtual clock

- One clock per run, owned by the scheduler. Every component, the gateway included, reads time only from it. Grant expiry, timeouts, `action_event.vt`, `attempt.sent_vt` and `effect.committed_vt` are virtual milliseconds.
- Wall-clock time is recorded next to it for metrics only, and never used for decisions.
- In the split topology, the virtual clock follows the host's monotonic clock.

### Randomness

- The run seed feeds independent named generators: latency, fault plans, data generation and scripted variation. Adding a new consumer does not shift another's numbers.
- No code uses the global `random` module or random UUIDs (0007).

### Models

| Adapter | Use |
| --- | --- |
| scripted | The deterministic policy in 0008, behind a chat model interface. No network and no tokens. |
| live | An API provider or Ollama. Records every request and response into a cassette. |
| replay | Serves a cassette's responses in order. The run fails if a request's hash differs from the recorded one. |

Checked in the langchain-core 1.8.0 source: a base chat model class and several fake chat models exist. The scripted adapter subclasses the base class.

### Determinism contract

The same scenario, configuration, contract level, seed and model outputs produce byte-identical message logs, effects ledgers, action events, approval events and evaluation JSON, ignoring wall-clock columns and fields. CI runs every demo scenario twice and compares digests.

We accept one known source of nondeterminism: LangGraph's retry backoff sleeps in wall time with jitter from the global generator (0008). It changes wall-clock metrics only.

### Playback and branching

- **Recorded playback** re-executes a run from its scenario, seed and cassette into a fresh run database. It never writes to the original run's database or to anything outside the simulator, which is what "writes disabled" in the spec protects.
- **Timeline view** reads a stored run's message log without executing anything.
- **Experimental branch** replays a run up to event N, then switches the model adapter from replay to live, or to another script. The result is a new run with `parent_run_id` and `branch_point`, and the viewer labels it a new experiment.
- Replay replaces snapshot restore from template databases. Replay rebuilds the in-memory state (the agent coroutine, gateway timers, messages in flight) that a database snapshot cannot hold, and it needs no quiet moment to take a snapshot. Template databases stay, for fast run creation.

### Run end

The agents' phase ends when every worker has submitted a final report, stopped with an error, or used up its budget. In G2 the gateway then closes the run (0003). The scheduler keeps running everything except the agents for a drain window (default `2 × (D + max_hop_latency) + L` virtual ms), so late commits and reconciliation can settle. Then it stops, and the evaluator runs. The last valid report counts (0004).

### Scenario file

```yaml
id: dev-001-single-po
task_id: TASK-dev-001
title: Order widgets for the main warehouse
task: |
  You are a procurement assistant. Complete requisition REQ-1: order 100 units of
  W-100 from an allowlisted supplier, charged to cost center CC-OPS, at no more
  than 6.00 USD per unit. Choose the cheapest acceptable quote.
  When you are done, submit a final report with one claim per requisition.
intents:
  - id: REQ-1
    operation: submit_purchase_order
    required: true
    accept:
      sku: W-100
      quantity: 100
      suppliers: [S-1, S-2]
      max_unit_price_minor: 600
      currency: USD
      cost_center: CC-OPS
facts:
  quote/S-1/W-100: {unit_price_minor: 540, currency: USD, min_quantity: 10, lead_time_days: 5, status: active}
  quote/S-2/W-100: {unit_price_minor: 575, currency: USD, min_quantity: 50, lead_time_days: 3, status: active}
  budget/CC-OPS: {limit_minor: 100000, committed_minor: 20000}
authority:
  allowlist: [S-1, S-2]
  per_order_ceiling_minor: 100000
  auto_approve_up_to_minor: 60000
  approval_ttl_ms: 3600000
  approval_wait_ms: 600000
  approver:
    - match: {}
      decision: approve
      after_ms: 2000
  resolver: none        # or {after_ms: 30000, attests: truth}
contracts: [C0, C1, C2, C3]
faults:
  - fault: F2
    at: {point: net.gw_sim.answer, match: {api: submit_purchase_order, answer: committed}, occurrence: 1}
params:                 # optional; defaults from 0005 and this record
  max_hop_latency_ms: 50
  commit_deadline_ms: 10000
  status_lag_ms: 0
  dispatch_timeout_ms: 2000
  think_ms: 1000
budget: {max_agent_steps: 40, max_tokens: 60000}   # per agent worker
```

- `intents`: one operation each. `accept` holds the acceptance constraints used by property 4 (0004).
- `facts`: initial values. Every fact starts at version 1.
- `authority`: see 0002. The resolver, when present, attests only after the settle time and always tells the truth.
- `faults`: see 0006. It may be a random plan.
- `params`: overrides the contract and timing defaults in 0005 and in this record. `max_wait_ms` is at most 60,000. Each run records the values it used.
- `budget`: applies to each agent worker.
- A JSON Schema for this format lives in the repo, and the loader validates every scenario against it. The loader also checks that the required intents are feasible together on the initial facts (0004).
- The loader takes any path. Held-out scenarios stay outside the repo and are run by passing their path.

## Alternatives considered

- **Real processes and HTTP for everything, with determinism from the seed alone.** Real scheduling and network timing reorder events, so "a seed reproduces a run" would be false. Rejected for tests and the study. Kept as the split topology for people who bring their own agent.
- **Template-database snapshots for branching (the spec).** Every connection must be closed first, and in-memory state is lost. Rejected in favour of replay.
- **Shared tables with a `run_id` column instead of a database per run.** Lighter, but one missing filter leaks state between runs, and parallel runs would interleave sequence values. Rejected.
- **Recorded mode as a transcript replay with no execution.** Cheap, but it proves nothing about the current code. Kept only as the timeline view; demos execute.

## Consequences

- `make demo` executes demo 1 in the inproc topology with the scripted model. It needs no API key.
- Anyone can re-execute every live result in the report, byte for byte, from its cassette.
- The deterministic scheduler is the riskiest new component. It lands in M1 with its own tests: message ordering, timers, crash and restart.
- Spec changes on acceptance: the determinism bullet in the Simulator section, the Playback and branching section, the Packaging row of the Stack table, the Testing section, and the Run entity (gains topology, model adapter, cassette digest and `parent_run_id`).
