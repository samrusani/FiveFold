# 0008: Configurations and trust boundaries

Status: Accepted (2026-11-14)
Amended by: 0011 (2026-11-15): the scripted policy's next step after a `run_closed` rejection; 0015 (2026-11-16): status waits use the scenario limit; 0018 (2026-11-18): B1 uses Temporal and the direct evidence route; 0020 (2026-11-22): B2 uses DBOS and the direct evidence route
Date: 2026-11-14
Spec sections affected: Baselines, Architecture (trust boundaries), Stack (database roles), CLAUDE.md (hard rules)
Resolves M0 questions: 1, 37, 39, 40; part of 31

## Context

The benchmark compares an agent stack without a gateway (B0) with stacks that have one (G1, G2). B0 must write to the simulator directly, which conflicts with the rule that only the gateway writes. Every baseline must be pinned before results exist, so nobody can tune it to fail. The trust boundaries must be enforced by mechanisms, not only described.

## Decision

### Configurations

| Config | Agent | Write path | Features |
| --- | --- | --- | --- |
| B0 | The shared LangGraph agent | The agent calls the simulator API directly | The framework's retries only |
| G1 | The same agent | Through the gateway | Authorization only |
| G2 | The same agent | Through the gateway | Authorization, action identity, freshness checks, contract-aware recovery, report checks |
| B1 (v0.2) | Temporal agent with the same scripted policy | Direct | Activity retries and a stable per-Activity key for C2 and C3 writes (0018) |
| B2 (v0.2) | DBOS agent with the same scripted policy | Direct | Durable Step results, Step retries and a stable per-Step key for C2 and C3 writes (0020) |

All configurations in one comparison use the same model, task text, intents, authority policy, token budget and step budget. The tool surfaces differ, as listed below, and the report says so.

### The shared agent

Checked against installed source on 14 Nov 2026 (langgraph 1.2.22, langgraph-prebuilt 1.1.2, httpx 0.28.1):

- The graph has a model node and the prebuilt `ToolNode`.
- `ToolNode` keeps its default error handling. By default, it returns only invalid-argument errors to the model and re-raises every other exception.
- The tool node has a `RetryPolicy` with library defaults: 3 attempts in total, and exponential backoff starting at 0.5 seconds with jitter. The default retry predicate retries connection errors, HTTP 5xx and most other exceptions. It does not retry built-in `TimeoutError` or other `OSError` subclasses.
- Our transport raises its own timeout exception, which is not an `OSError`, so the default predicate retries it. httpx's timeout exceptions are not `OSError` subclasses either, so an httpx-based tool behaves the same way.
- Tools return every answer from the destination (success and 4xx) to the model as a structured result. They raise only on transport failure or a server error (5xx, or the gateway's `gateway_timeout`). Business rejections reach the model; lost replies trigger the framework's retry.
- When retries run out, the graph raises. The harness records an agent error and ends that worker without a report. That is a realistic failure, not a harness bug.
- The agent's transport timeout for a call is the tool's declared server-side wait plus T (0005): T for reads and simulator calls, `tool_wait_ms` plus T for gateway writes, and `wait_ms` plus T for calls that wait.
- In the inproc topology, the retry backoff sleeps in wall time and does not advance the virtual clock. It changes wall-clock metrics only.

Changing any of this needs a decision record.

### Tool surfaces

G1 and G2 expose the same tools with the same schemas. Only their behaviour differs.

| Tool | B0 | G1 and G2 |
| --- | --- | --- |
| get_quote, list_suppliers, get_budget, get_inventory | Simulator API; returns data and version | Gateway; returns data, version and read_id |
| request_approval, get_approval_status(wait_ms) | Simulator approvals service; `wait_ms` waits for a decision | Not exposed; the gateway files requests |
| submit_purchase_order, reserve_inventory | Simulator API. Args include intent_id, approval_id and basis (fact key to version); idempotency_key is optional at C2 and C3. | Gateway. Args include intent_id and based_on (read_ids). |
| get_order_status, get_reservation_status | C1 only: the simulator's status lookup by intent_id | Not exposed |
| get_action_status(wait_ms) | Not exposed | Gateway (0003) |
| submit_final_report | Harness report sink, unchecked | G2 checks claims against its records (0003), then stores the report. G1 forwards it unchanged. Both store it in the harness report sink. |

### G1: authorization only

- For each write call, G1 files an approval request, waits for the decision, checks the approval before sending, consumes the grant and sends once.
- G1 has no action identity. Every call is a new action with its own approval request. G1's action rows have `identity` off and are exempt from I7 (0003).
- G1 has no freshness checks. It forwards cited reads as the basis but never checks them.
- G1 has no idempotency keys, status probes, retries, report checks or run closing.
- On a timeout or transport error, G1 returns an MCP error result, `gateway_timeout`, as a proxy returns 504. The agent's tool layer raises it as a server error, so the framework's retry applies, exactly as for a timeout in B0. The action itself is recorded honestly as OutcomeUnknown.
- G1 follows the single-use grant pattern. We do not claim it reproduces any specific product.

### The scripted agent policy

Deterministic tests and demos use a scripted model (0009). Its policy is the same in every configuration.

1. Read the quotes of the allowed suppliers and the budget. Choose the cheapest acceptable quote that fits the budget. In F9's `different_args` variant, the second worker chooses the second-cheapest.
2. B0 only: request approval, then wait for the decision with `get_approval_status(wait_ms = min(60,000, max_wait_ms))`, at most 3 times.
3. Submit the write once, with no optional fields. G modes cite exactly the reads of the operation's dependencies: the chosen quote and the budget. B0 passes the approval id and those versions.
4. Act on the result:

| Result | Next step |
| --- | --- |
| confirmed | Record the effect id; the intent is done |
| pending_approval, proposed, authorized, dispatched or outcome_unknown | Wait with `get_action_status(wait_ms = min(60,000, max_wait_ms))`, at most 3 times, then act on the new result |
| rejected or failed because of stale inputs | Re-read and try again, at most twice. B0 requests a new approval when the old one no longer covers the order. If no acceptable option fits the budget after re-reading, stop. |
| rejected because the approval was denied, revoked, expired or timed out | Stop working on the intent |
| rejected because the intent or the run has closed (`run_closed`) | Stop working on the intent |
| failed for any other reason | Try again once |
| refused: intent_busy | Wait on the existing action as above, and adopt its result |
| refused: intent_done | Record the existing effect id; the intent is done |
| refused: invalid_args, unknown_read or missing_basis | Re-read and try again once |
| refused for any other reason | Stop working on the intent |
| B0: a business rejection from the simulator | Use the row for the same reason |

5. Report. G modes first call `get_action_status` once per action, with `wait_ms = 0`. The report says `done` with the effect ids, `not_done` when the intent ended rejected, failed or stopped, and `unknown` otherwise. If G2 refuses the report as inconsistent, correct the named claims and resubmit, at most twice.

The policy is code in `agents/`, reviewed like a contract. Changing it needs a decision record.

### Trust boundaries

| Boundary | Rule | Enforced by |
| --- | --- | --- |
| Agent | In G modes, holds no simulator credential and has no route to the simulator | Inproc: the agent receives only an MCP client bound to the gateway. Split: the agent's container is only on a network it shares with the gateway. Test: in G modes, the simulator refuses a write from the agent's identity. |
| Baselines | B0 to B2 use a simulator credential, by design | The run config creates the credential per run. In B2 the harness holds it for the direct API calls. |
| Gateway | The only writer in G modes; no access to simulator tables | Role `ff_gw` has no privileges on schema `sim`; all simulator access goes through its API |
| Simulator | The only owner of ground truth | Role `ff_sim` owns schema `sim`; no other role can write it |
| Evaluator and viewer | Read-only outside their own results | `ff_eval` writes only `eval`; `ff_viewer` can only select |

Database privileges per run database:

| Role | sim | gw | harness | agent | eval |
| --- | --- | --- | --- | --- | --- |
| `ff_sim` | all | none | none | none | none |
| `ff_gw` | none | all, except UPDATE and DELETE on `action_event` | none | none | none |
| `ff_harness` | select | none | all | none | none |
| `ff_agent` | none | none | none | all | none |
| `ff_eval` | select | select | select | none | all |
| `ff_viewer` | select | select | select | none | select |

- `ff_harness` can read `sim` so the scripted resolver can check the ledger out of band, as a human would.
- `ff_agent`'s `search_path` points at schema `agent`, where LangGraph's checkpointer keeps its tables.
- B2 gives each invocation a separate DBOS system database with one schema per worker (0020). `ff_dbos` connects to that database and creates and uses its own objects in those schemas. It has no privileges in `sim`. The worker child receives only its DBOS credential.
- The gateway reaches the harness report sink through the harness's API, not its tables.
- The gateway and the simulator connect with different roles and cannot touch each other's tables, so they can never share a database transaction. Without this rule, exactly-once would be trivial, and the results would be meaningless.
- A test asserts every cell of the privilege table.

### Hard rule wording in CLAUDE.md

"The agent never holds write credentials. Only the gateway writes to the simulator." becomes:

> In gateway configurations (G1, G2), the agent holds no simulator credential, and only the gateway writes to the simulator. Baselines (B0, B1, B2) write directly by design; that is what they measure.

## Alternatives considered

- **B0 behind a pass-through recording proxy.** The hard rule would stay literal, but a proxy in the path is a gateway, and its timeout behaviour would shape B0's results. Rejected.
- **B0 with error handling turned on in ToolNode**, so the model sees every error and decides. Plausible, but it is not the library default, and it removes the framework retry that the problem statement is about. Rejected as the default. The live study may add it later as a separate configuration.
- **Finer ablations (G2 minus one feature at a time).** Useful for research, but every matrix would grow. Deferred. Configs can switch features off later.
- **G1 with action identity.** G1 would then pass most duplicate faults and become nearly G2. The spec defines G1 as "authorization only". Rejected.
- **The scripted agent cites every read in based_on.** Harmless changes to quotes it did not choose would then block its order. Rejected; it cites only the operation's dependencies, and the gateway checks only those (0001).

## Consequences

- The comparison is fair on inputs and documented on tool surfaces.
- B0's behaviour is pinned to library defaults checked in source. A LangGraph upgrade must re-check these facts (0007).
- Spec changes on acceptance: the Baselines section (exact definitions), the trust boundaries (the enforcement table), the Stack (schemas and roles), and the hard rule in CLAUDE.md.
