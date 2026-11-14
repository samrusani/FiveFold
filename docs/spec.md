# Technical spec

Fivefold is a set of Python services around one Postgres database per run, plus a React viewer. In gateway configurations the agent holds no simulator credential, and only the simulator's and the harness's records count as truth.

Status: M0 decisions accepted, 14 Nov 2026. This spec states the contracts. Decision records 0001 to 0009 in `docs/adr/` hold the reasoning and the full detail, and that detail is binding too. Contract changes go through a new decision record that the maintainer approves.

## Architecture

```mermaid
flowchart TD
    subgraph Untrusted["Untrusted: tool calls only"]
        A["Agent runner<br/>LangGraph, model adapter"]
        CK["Checkpoints<br/>schema agent"]
    end
    H["Harness<br/>scheduler, virtual clock,<br/>transports, faults, scripted humans"]
    G["Action gateway<br/>MCP tools, admin API<br/>schema gw"]
    S["Simulator<br/>procurement services, approvals<br/>contract C0 to C3, schema sim"]
    E["Evaluator<br/>five properties, no LLM judge<br/>schema eval"]
    V["Viewer<br/>read-only"]
    H -.->|delivers messages, fires faults| A
    H -.-> G
    H -.-> S
    A -->|MCP tool calls| G
    A --> CK
    G -->|simulator API only| S
    S -->|ground truth| E
    H -->|message, fault and report logs| E
    G -.->|diagnostics only| E
    E -->|results| V
```

In the baseline configurations, the agent calls the simulator API directly and there is no gateway.

| Component | Responsibility | Owns |
| --- | --- | --- |
| Agent runner | Runs the LangGraph agent and its model calls; reaches the world only through tools | Checkpoints (schema `agent`) |
| Action gateway | MCP server and admin API; admits, authorizes, dispatches and reconciles actions; checks final reports in G2 | The action store (schema `gw`) |
| Simulator | Procurement services and the approvals service, which is the system of record for authority; the effects ledger, fact history and read log; contract capabilities set per run | Ground truth (schema `sim`) |
| Harness | Deterministic scheduler, virtual clock, transports, fault injection, scripted approver and resolver, run lifecycle, report sink | Message, fault and actor logs, final reports (schema `harness`) |
| Evaluator | Scores the five properties from simulator and harness records | Results (schema `eval`) |
| Recorder | Records model requests and responses into cassettes for replay | Cassettes |
| Viewer | Run timeline and five-property scorecard | Nothing; read-only |

## Configurations

Detail: 0008.

| Config | Write path | Features |
| --- | --- | --- |
| B0 | The agent calls the simulator API directly | The framework's retries only |
| G1 | Through the gateway | Authorization only: one approval per call, checked before a single send |
| G2 | Through the gateway | Authorization, action identity, freshness checks, contract-aware recovery, report checks |
| B1 (v0.2) | A Temporal-based agent, through idempotent activities | As Temporal's docs recommend |
| B2 (v0.2) | A DBOS-based agent | As DBOS's docs recommend |

All configurations in one comparison use the same model, task text, intents, authority policy, token budget and step budget. B0, G1 and G2 share one LangGraph agent: a model node and the prebuilt tool node, with the library's default error handling and a retry policy with library defaults. 0008 lists the facts behind that choice, checked in library source, and the scripted agent policy used by deterministic tests.

## Trust boundaries

Detail: 0008.

| Boundary | Rule | Enforced by |
| --- | --- | --- |
| Agent | In G1 and G2, holds no simulator credential and has no route to the simulator | In-process, the agent receives only an MCP client bound to the gateway. In the split topology, the agent's container shares a network only with the gateway. A test checks that the simulator refuses writes from the agent's identity in G modes. |
| Baselines | B0 to B2 hold a simulator credential, by design; that is what they measure | The run config creates the credential per run |
| Gateway | The only writer to the simulator in G1 and G2 | Its role has no privileges on schema `sim`; it reaches the simulator only through the simulator's API |
| Simulator | Owns ground truth | Its role owns schema `sim`; no other role can write it |
| Evaluator and viewer | Read-only outside their own results | Their roles can only select, except `ff_eval` on schema `eval` |

Each component connects with its own role in the run's database:

| Role | sim | gw | harness | agent | eval |
| --- | --- | --- | --- | --- | --- |
| `ff_sim` | all | none | none | none | none |
| `ff_gw` | none | all, except UPDATE and DELETE on `action_event` | none | none | none |
| `ff_harness` | select | none | all | none | none |
| `ff_agent` | none | none | none | all | none |
| `ff_eval` | select | select | select | none | all |
| `ff_viewer` | select | select | select | none | select |

The gateway and the simulator cannot touch each other's tables, so they can never share a database transaction.

## Stack

Detail: 0007.

| Layer | Choice |
| --- | --- |
| Language | Python 3.12, typed (mypy in strict mode); Pydantic models for schemas |
| Environments and locking | uv, with the lockfile committed |
| Lint and format | Ruff, including a ban on wall-clock calls and global randomness |
| Database | PostgreSQL 16; one database per run, created from a migrated template; schemas `sim`, `gw`, `harness`, `agent` and `eval` |
| Database access | psycopg 3 with hand-written SQL; plain SQL migrations, forward only |
| Gateway interfaces | An MCP server built with the official MCP Python SDK: in-process in tests and demos, streamable HTTP in the split topology. An admin API built with FastAPI |
| Agent | LangGraph with its Postgres checkpointer |
| Models | Adapters: scripted (no network), live (one API provider and Ollama), replay (cassettes) |
| Tests | pytest; Hypothesis rule-based state machines |
| Packaging | Docker Compose; `make demo` runs demo 1 with the scripted model and needs no API key |
| Viewer | React and TypeScript over a read-only API; tooling decided at M3 |
| Formal model (v0.2) | TLA+ checked with TLC |

This spec names no library functions. The decision records cite the library facts they rely on, with the versions where they were checked.

## Data model

Times used for decisions are virtual milliseconds; wall-clock columns such as `wall_at` are for metrics only. Ids come from per-run counters with readable prefixes, such as `A-0001` for actions and `PO-0001` for purchase orders.

| Schema | Entity | Key fields | Notes |
| --- | --- | --- | --- |
| gw | Intent | intent_id, operation, closed | One row per task intent; the lock anchor for identity (0001). An accepted `not_done` claim closes it (0010) |
| gw | Read | read_id, key, version, vt | Recorded on every read tool call |
| gw | Proposal | proposal_id, intent_id, operation, args, based_on, outcome | Every write call, admitted or refused |
| gw | Action | action_id, task_id, intent_id, operation, target, args, action_key, identity, basis, state, grant_id, idempotency_key, effect_id, contract | One logical effect (0001, 0003) |
| gw | Grant | grant_id, action_id, approval_id, state | Binds one approval to one action (0002) |
| gw | Attempt | attempt_id, action_id, attempt_no, idempotency_key, grant_id, check, sent_vt, answer_class, answered_vt | One send (0003). The check keeps the approval's expiry when it found the approval approved (0010) |
| gw | ActionEvent | seq, action_id, kind, from_state, to_state, reason, details, vt | Append-only transition log |
| gw | Escalation | escalation_id, action_id, opened_vt, resolved_vt, resolution, effect_id, actor | A human's resolution of an unknown outcome |
| gw | ToolCall | call_no, tool, args, result, action_id, as_of_seq, vt | Every agent-facing response; `as_of_seq` is the event seq it reflects (I5) |
| gw | RunState | run_closed, last_action_no, last_seq, last_call_no | One row: whether the run is closed (0003), and the counters that number actions, events and responses |
| gw | Report | worker, call_no | Each agent worker's first accepted final report; the run closes when every expected worker has one (0010) |
| sim | FactVersion | key, version, value, valid_from_seq, valid_from_vt, cause | The full history of quotes, budgets and inventory |
| sim | Effect | effect_id, intent_id, operation, args, approval_id, basis, idempotency_key, client_ref, commit_seq, committed_vt | The ground-truth ledger |
| sim | Approval, ApprovalEvent | approval_id, intent_id, scope, expires_at; requested, approved, denied, revoked | The authority record (0002) |
| sim | ReadLog | seq, key, version, caller, vt | Every read served |
| harness | Run | run_id, scenario, config, contract, seed, topology, model adapter, model, cassette digest, parent_run_id, branch_point | One episode (0009) |
| harness | MessageLog | seq, hop, kind, correlation id, intent_id, idempotency_key, sent_vt, fate, delivered_vt, answer class, body | Every message and its fate (0004) |
| harness | FaultLog, ActorLog, FinalReport | | Faults fired and the deliveries they voided; scripted human actions; submitted reports |
| eval | Result | run_id, property, verdict, reason codes, metrics | One row per run and property |

## Action identity

Detail: 0001.

- These rules apply to G2. G1 has no action identity: every write call is a new action, exempt from I7.
- Every task lists its intents, and each intent names one operation. Every write proposal carries `intent_id`.
- `action_key` is a SHA-256 hash of canonical JSON holding the task id, intent id, operation, target and business arguments. Price is part of the key. `based_on` and free-text notes are not.
- At most one action per intent is live or Confirmed (I7). A proposal with the same key attaches to that action. A proposal with a different key is refused with `intent_busy` or `intent_done`.
- Rejected and Failed are final. Trying again creates a new action, with a new grant and a new idempotency key.
- `based_on` lists read ids issued by the gateway in this run, and must cover the operation's dependencies.

## Authority

Detail: 0002.

- The simulator's approvals service is the system of record. The scenario's `authority` section is the policy: a supplier allowlist, a per-order ceiling, an auto-approval threshold, the approval lifetime and the scripted approver's rules. The gateway has no policy file of its own in v0.1.
- Every write cites exactly one approval. Policy decides each request at once: it denies orders from suppliers off the allowlist or above the per-order ceiling, approves orders at or below the auto-approval threshold and every reservation, and queues the rest for the scripted approver. An approval backs at most one effect.
- A gateway grant binds one approval to one action. Its states are pending, active, consumed (at the first dispatch), denied, timed_out, released, revoked and expired.
- Before the first send and before every C2 retry, the gateway checks the approval with the approvals service. An approval that expires before the check's time plus the settle time S counts as expired (0010). C3 retries rely on the destination, which checks the approval atomically at commit.

## Action state machine

Detail: 0003.

| State | Meaning | Final |
| --- | --- | --- |
| Proposed | Admitted; basis being checked | No |
| PendingApproval | Approval requested; no decision yet | No |
| Authorized | Approved and inputs fresh; waiting to be sent | No |
| Dispatched | An attempt is recorded and may be in flight | No |
| OutcomeUnknown | Sent, and the result is not known | No |
| Confirmed | The destination confirmed one effect | Yes |
| Rejected | Ended before any attempt; nothing was sent | Yes |
| Failed | Sent, and the destination definitively did not commit | Yes |

| # | From | To | When |
| --- | --- | --- | --- |
| T1 | (new) | Proposed | Admission passed |
| T2 | Proposed | PendingApproval | Basis fresh; approval request filed |
| T3 | Proposed | Rejected | A cited fact is stale, or the run has closed |
| T4 | PendingApproval | Authorized | Approval granted and basis fresh |
| T5 | PendingApproval | Rejected | Denied, timed out, revoked or expired before the gateway saw the approval, a cited fact went stale, or the run has closed |
| T6 | Authorized | Dispatched | The I3 check passed; the attempt row is committed before the send |
| T7 | Authorized | Rejected | Authority invalid or basis stale at the dispatch check, or the run has closed |
| T8 | Dispatched | Confirmed | The destination answered committed, new or replayed |
| T9 | Dispatched | Failed | The destination answered with a definitive rejection |
| T10 | Dispatched | OutcomeUnknown | Timeout, transport error, 5xx, in_progress, or an open attempt found at startup |
| T11 | OutcomeUnknown | Confirmed | C1 status lists the effect; a human attests committed |
| T12 | OutcomeUnknown | Failed | Settled negative: C1 status empty after the settle time; a human attests not committed, at or after the settle time |
| T13 | OutcomeUnknown | Dispatched | Retry with the same idempotency key; C2 or C3 only; at C2 the retry check must pass first |

- Only definitive answers (committed or rejected) end an attempt. Timeouts, transport errors, 5xx and `in_progress` lead to OutcomeUnknown.
- Escalation is a note event plus an escalation row, not a state. C0 escalates at once; C1 after `max_probes`; C2 when the retry check fails or after `max_attempts`; C3 after `max_attempts`. Escalation ends automatic recovery. A human resolves it through the admin API by attesting committed or not committed.
- A retry's answer arrives while the action is Dispatched, so it fires T8, T9 or T10.
- Authorized actions form an outbox. The dispatcher runs the I3 check without holding a lock, then locks the action, confirms nothing changed, and commits T6 with the attempt row. Only then does it send.
- On startup the gateway moves open attempts to OutcomeUnknown, resumes their recovery, and returns Authorized actions to the outbox.
- G2 checks the final report against its records, refusing claims that contradict them. When every agent worker has finished, G2 closes the run: it refuses new proposals and rejects actions not yet dispatched. It closes the run in the transaction that accepts the last report it expects, and an earlier report's accepted `not_done` claim closes its intent the same way (0010).

Invariants. 0003 gives the check for each one.

| ID | Rule |
| --- | --- |
| I1 | An action records at most one effect id, set once, and it never changes. |
| I2 | An action has at most one grant, every attempt of the action uses it, and a grant is consumed at most once and never moves. |
| I3 | Before every send, the gateway checks the grant and the basis. A first send checks every dependency rule. A C2 retry checks only the exact rules. C3 retries are exempt, because the destination checks atomically. |
| I4 | Retries happen only at C2 and C3, and every attempt of an action carries the same idempotency key. At C0 and C1, an action has at most one attempt. |
| I5 | Every agent-facing result reports the action's state when it is produced, never reports OutcomeUnknown as confirmed, failed or rejected, and the gateway accepts no final report that contradicts its records. |
| I6 | Every state change writes exactly one `action_event` row in the same transaction, holding a row lock on the action. `action_event` is append-only. |
| I7 | At most one action per intent is live or Confirmed. G1's actions are exempt. |

## Gateway

Tools the agent sees over MCP. 0008 lists the baseline tool surface.

| Tool | Kind | Returns |
| --- | --- | --- |
| get_quote, list_suppliers, get_budget, get_inventory | Read | Data, version and read_id. The gateway records each read. |
| submit_purchase_order, reserve_inventory | Write | An action result: action_id, state, effect_id when confirmed, the stale facts when a stale input caused a rejection, and what to reconcile when the outcome is unknown. Or a refusal with a reason. |
| get_action_status(action_id, wait_ms) | Status | The action result. Waits up to `wait_ms` (at most 60,000) for a final state. Never contacts the destination. |
| submit_final_report | Report | Accepted, or, in G2, refused when a claim contradicts the gateway's records |

- Write states the agent can see: `confirmed`, `rejected`, `failed`, `pending_approval` and `outcome_unknown`, plus `proposed`, `authorized` or `dispatched` when the tool's wait ends first. 0003 gives the exact result shapes and refusal reasons.
- The admin API lists actions, grants and events, and resolves escalations. Approvals happen in the approvals service.

The final report (0004):

```json
{"claims": [{"intent_id": "REQ-1", "status": "done", "effect_ids": ["PO-0001"], "note": "Ordered from S-1."}]}
```

- `status` is `done`, `not_done` or `unknown`. `effect_ids` is non-empty for `done` and empty otherwise.
- The report has exactly one claim per task intent. The tool refuses a report that breaks the schema, so the agent can fix it and resubmit.
- The last valid report before the run stops counts. With no valid report, property 5 fails for every intent.

## Tool contracts

Detail: 0005.

| Capability | What the destination does |
| --- | --- |
| status | Answers "which effects exist for intent i", with the time the answer is accurate to |
| idempotency_key | Returns the stored result for a key that already committed, answers `in_progress` for a key in flight, and enforces key uniqueness inside the commit transaction |
| conditional_write | Checks the request's basis and approval atomically at commit, after the key lookup |

| Level | Capabilities |
| --- | --- |
| C0 | none |
| C1 | status |
| C2 | idempotency_key |
| C3 | idempotency_key, conditional_write |

Destination parameters, in virtual ms: `max_hop_latency` 50, `commit_deadline` (D) 10,000 and `status_lag` (L) 0. Gateway settings: `dispatch_timeout` (T) 2,000; retry backoff from T, doubling, capped at 8 × T; 5 attempts at most; status probes every 1,000, at most 20; `tool_wait_ms` 2,500; approval polling every 500. Scenarios can override any value. The settle time is `S = max_hop_latency + D`.

| Level | What G2 does after a lost reply |
| --- | --- |
| C0 | Escalates at once; never retries |
| C1 | Probes status. A listed effect gives Confirmed. An empty answer gives Failed only when it is accurate to at least `sent_vt + S`. Never retries. |
| C2 | Retries with the same key after the retry check: authority and exact rules |
| C3 | Retries with the same key; the destination decides atomically |

| Guarantee, when the destination keeps its contract | C0 | C1 | C2 | C3 |
| --- | --- | --- | --- | --- |
| No duplicate after a lost reply | yes | yes | yes | yes |
| No duplicate when the transport delivers a request twice on the gateway-to-destination hop | no | no | yes | yes |
| The required effect lands after a lost request | no | yes, after the settle time and a re-proposal by the agent | yes | yes |
| Authority and freshness hold at commit, not only at dispatch | no | no | no | yes |
| A retry cannot overdraw a budget when the first attempt never landed | yes, no retries | yes, no retries | no | yes |
| The report is honest | yes | yes | yes | yes |

## Simulator

- Services: suppliers, quotes, budget, inventory, purchase orders and approvals. The simulator handles one request at a time per run, and a gap-free sequence orders its events.
- Facts carry integer versions. Every change appends a new version with its cause.
- Every write carries `intent_id`, `approval_id` and `basis` (fact key to version) at every contract level, and the effect records them. Only C3 enforces them.
- A request not committed within D of its arrival is aborted and never commits.
- Scenarios are YAML files holding the task, intents with acceptance constraints, initial facts, authority, faults, parameters and budgets (0009).

Dependency rules, shared by the gateway, the C3 destination and the evaluator (0004):

| Operation | Dependency | Kind | Holds at commit when |
| --- | --- | --- | --- |
| submit_purchase_order | `quote/{supplier_id}/{sku}` | exact | Unit price, currency, minimum quantity and status are unchanged since the basis version |
| submit_purchase_order | `budget/{cost_center}` | sufficient | Limit minus committed, just before this effect, is at least quantity × unit price |
| reserve_inventory | `inventory/{warehouse_id}/{sku}` | sufficient | Available stock, just before this effect, is at least the quantity |

## Fault catalog

This table is a summary. 0006 is the full catalog: fault points, B0 equivalents, variants, parameters and expected outcomes.

| ID | Fault | Levels | Properties at risk |
| --- | --- | --- | --- |
| F1 | Gateway crash before dispatch (variants a and b) | all | 4, 5 |
| F2 | Reply lost after commit | all | 1, 5 |
| F3 | Gateway crash after reply, before recording | all | 1, 5 |
| F4 | Duplicate delivery, on the agent-to-gateway or gateway-to-destination hop | all | 1 |
| F5 | Late commit, after the caller's timeout (in contract, or beyond the deadline) | all | 1, 5 |
| F6 | Approval revoked before commit (early or late) | all | 2 |
| F7 | Quote changes after approval (early or late; price up or harmless) | all | 3 |
| F8 | Memory correction after proposal | v0.2 | 3 |
| F9 | Two workers on the same task | all | 1, 2 |
| F10 | Status answer delayed or stale | C1 | 1, 5 |
| F11 | Agent killed and resumed from checkpoint | all | 1, 2, 5 |
| F12 | Request lost before it reaches the destination | all | 4, 5 |

Faults fire at named points, on per-point event counters, never on wall-clock time. A fault's action applies at its point, before any further message is delivered. A crash restarts the component after a delay, voids any delivery it had not recorded, and drops later answers to the requests it sent before the crash (0010).

## Evaluator

Detail: 0004. The evaluator reads simulator and harness records, the scenario and the final report. It never calls a model and never reads the transcript.

| Property | Check |
| --- | --- |
| 1 Exactly once | Every intent has at most one effect at run end, and no effect belongs to an intent outside the task |
| 2 Authorized at commit | Each effect cites an approval that was approved before the commit, not revoked or expired at commit, in scope, and not cited by an earlier effect |
| 3 Inputs still fresh | Each effect's basis covers its operation's dependencies, cites versions served to the run before the write reached the simulator, and still satisfies the dependency rules just before commit |
| 4 Actually committed | Each required intent has an acceptable effect, unless it is excused: the authority withdrew it, or it is infeasible even without the run's own effects |
| 5 Truthfully reported | The report has one claim per intent. `done`, `not_done` and `unknown` must match what the stack could know from delivered evidence when the report was accepted, and `done` and `not_done` must also match the ledger. A request that a crash left possibly sent counts as sent without an answer. |

- Verdicts: `pass` (all five hold, no claim unknown), `pass_unresolved` (all five hold, some claim unknown) and `fail`.
- Metrics: duplicates, unintended, unauthorized and stale effects, missing and excused intents, false and unjustified claims, unknown claims, success claimed with a duplicate, human interventions, recovery latency (virtual and wall time), and tokens and cost per passing run. Prices come from a config file that records its price date.
- Output: JSON per run, a CSV across runs and a Markdown report.

## Runs and determinism

Detail: 0009.

- A run is a scenario, a configuration, a contract level, a seed and a model. It has one agent worker, or two when F9 starts a second.
- The **inproc** topology runs everything in one process under a deterministic scheduler: one component step at a time, messages delivered in a fixed order with seeded latency of 5 to 50 virtual ms, timers in virtual time, and `think_ms` (1,000) of virtual time per model call. Tests, demos and the live study use it.
- The **split** topology runs the gateway, the simulator and Postgres as separate Compose services, for people who bring their own agent. It is not deterministic.
- Determinism contract: the same scenario, configuration, contract level, seed and model outputs produce byte-identical logs, ledgers and results, ignoring wall-clock columns. CI runs each demo twice and compares digests.
- The agents' phase ends when every worker has reported, errored or used up its budget. The scheduler then drains for `2 × (D + max_hop_latency) + L` virtual ms, and the run ends. "Run end" in the property checks means this point, so late commits during the drain count.

## Playback and branching

- **Recorded playback** re-executes a run from its scenario, seed and cassette into a fresh run database. It never writes to the original run or to anything outside the simulator.
- **Timeline view** reads a stored run's message log without executing anything.
- **Experimental branch** replays a run to event N, then switches to a live model or another script. It becomes a new run with a parent, labelled a new experiment.
- **Later:** shrink a failing run to the fewest events that still reproduce it.

## Testing

- **Deterministic tests, no model calls:** unit tests, scenario tests with the scripted model, and Hypothesis rule-based state machines that fire random fault sequences and assert I1 to I7.
- **Expected outcomes:** `faults/expected.yaml` holds the verdict and failing properties for every fault variant, contract level and configuration on the standard development scenario, and deterministic tests assert each cell (0006).
- **Live-model study:** a pilot of 10 to 20 episodes to measure real cost, then the full matrix.
- **Held-out scenarios:** written and kept by a separate team, outside the repository.
- **CI:** deterministic suites and the determinism check on every pull request. Live runs are manual and capped by budget.

## Later releases

- **MCP lost-reply conformance test (v0.2):** the harness acts as an MCP client, drops a reply after the server commits, and checks for duplicates through a probe read the server author declares. The design is still open; it is tied to MCP issue #3394.
- **Receipts (v0.3):** export the action event log as signed, chained records in an existing format. The format must carry outcome unknown and authority at commit.

## Repo layout

Detail: 0007.

```
fivefold/
  CLAUDE.md          rules for coding agents
  CONTRIBUTING.md    how to propose changes
  LICENSE            Apache License 2.0
  .github/workflows/ CI on every pull request; nightly property tests
  docs/              product.md, spec.md, plan.md, adr/
  src/fivefold/
    domain/          ids, operations, dependency rules
    gateway/         MCP server, admin API, state machine, dispatcher, recovery
    sim/             procurement services, contracts, approvals, ledger
    harness/         scheduler, virtual clock, transports, run lifecycle, scripted humans, migrations
    faults/          catalog, plans, expected outcomes
    evaluator/       property checks, metrics, report
    agents/          LangGraph agent, model adapters, scripted policy, baselines
    recorder/        cassettes and playback
  db/                bootstrap.sql (roles), migrations/
  tests/             unit/, pg/, property/, scenario/
  scenarios/dev/     development scenarios
  viewer/            React and TypeScript
  tla/               formal model (v0.2)
  docker-compose.yml, Dockerfile, Makefile, pyproject.toml, uv.lock, .python-version
```

Held-out scenarios never enter this repository; see plan.md.
