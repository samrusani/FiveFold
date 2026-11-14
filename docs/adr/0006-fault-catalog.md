# 0006: Fault catalog, fault points and expected outcomes

Status: Proposed
Date: 2026-11-14
Spec sections affected: Fault catalog, Demos (product.md), Testing, plan.md (M1 to M3), product.md success measures, CLAUDE.md (Tests)
Resolves M0 questions: 4, 6, 10 (fault side), 30, 33 (F4), 34, 35; part of 37

## Context

Faults must fire at exact, reproducible points. Each fault must mean the same thing in every configuration, including B0, which has no gateway. The expected outcome of every combination of fault, contract and configuration must be fixed before live results exist, so nobody tunes a baseline to fail.

## Decision

### Fault points

Code calls a fault hook at each named point, and the harness decides whether a fault fires there. The list changes only by decision record.

| Point | Where |
| --- | --- |
| `gw.dispatch.before_check` | The gateway picked an Authorized action, before the I3 check |
| `gw.dispatch.after_check` | The I3 check passed; the attempt row is not yet written |
| `gw.dispatch.after_record` | The attempt row is committed; the request is not yet sent |
| `gw.dispatch.after_answer` | An answer arrived; it is not yet recorded |
| `net.{hop}.request` | A request on a hop: `agent_gw` or `gw_sim` in G modes, `agent_sim` in baselines |
| `net.{hop}.answer` | An answer on a hop |
| `sim.write.received` | A write request reached the destination |
| `sim.status.answer` | A C1 status answer is being produced |
| `agent.before_tool_call` | The agent is about to send a tool call |
| `agent.after_tool_call` | The tool call was sent; no result yet |
| `agent.after_tool_result` | A tool result arrived; the checkpoint is not yet written |
| `agent.step` | The agent finished a step |

- A fault in a plan names a point, an optional match on message fields (tool or API name, intent_id, attempt number, answer class), and an occurrence number. The fault fires the Nth time the point is reached with a matching event in this run.
- Counters are kept per point per run, so unrelated new events do not shift a plan. Faults never fire on wall-clock time.
- A fault takes effect at its point, before the scheduler delivers any further message. Faults that change simulator state (F6's revocation, F7's quote change) apply directly through the simulator's control interface at that moment. They are not sent as messages, so they cannot arrive late.

### Transport rules

- When a caller times out, the transport drops any later answer to that request. This matches an HTTP client that has closed its connection.
- Messages addressed to a component that is down are dropped.
- When the transport delivers a request twice (F4), the answer to the copy is not delivered to the sender. A retrying proxy behaves this way.
- Every message's fate is recorded in `harness.message_log` (0004).

### Crashes

A crash fault stops the component at the point and discards its memory, including open database transactions. The harness restarts it after `restart_delay_ms` (default 1,000 virtual ms; each crash fault can set its own). The gateway restarts through startup recovery (0003). The agent restarts from its last checkpoint, so LangGraph re-runs the node that was interrupted.

Some crash points change what the stack can know. The fault log records this, and property 5 uses it (0004):

| Crash point | Effect on evidence |
| --- | --- |
| `gw.dispatch.after_answer` | Voids the answer just received |
| `agent.after_tool_result` | Voids the tool result just received |
| `gw.dispatch.after_record` | Records a possibly-sent request: the stack recorded an attempt but cannot know whether it left. The evaluator treats it as a request without an answer. |
| any other point | Nothing |

### Catalog

| ID | Fault | Fires at (G modes) | B0 equivalent | Variants and parameters | Levels | Properties at risk |
| --- | --- | --- | --- | --- | --- | --- |
| F1 | Gateway crash before dispatch | F1a: crash at `gw.dispatch.after_check`; F1b: crash at `gw.dispatch.after_record` | Agent crash at `agent.before_tool_call` for the write | a, b; `restart_delay_ms` | all | 4, 5 |
| F2 | Reply lost after commit | `net.gw_sim.answer`, answer class committed: dropped | `net.agent_sim.answer` | none | all | 1, 5 |
| F3 | Gateway crash after reply, before recording | Crash at `gw.dispatch.after_answer` | Agent crash at `agent.after_tool_result` | `restart_delay_ms` | all | 1, 5 |
| F4 | Duplicate delivery | `net.{hop}.request`: delivered twice | `net.agent_sim.request` | hop: `agent_gw` or `gw_sim`; `copy_delay_ms` (default: seeded, 5 to 50) | all | 1 |
| F5 | Late commit, after the caller's timeout | `sim.write.received`: commit delayed by `delay_ms` | same | in contract (T < delay < D; default 5,000); beyond deadline (delay > D, a contract violation) | all | 1, 5 |
| F6 | Approval revoked before commit | Revokes the action's approval. Early: at `gw.dispatch.before_check`. Late: at `net.gw_sim.request` of the write. | Revokes the agent's approval. Early: when the approval answer reaches the agent. Late: at `net.agent_sim.request` of the write. | early, late | all | 2 |
| F7 | Quote changes after approval | Changes the quote the action cites, at the same points as F6 | as F6 | early, late; change `price_up` (default +5%, within the scenario's maximum) or `harmless` (lead time only) | all | 3 |
| F8 | Memory correction after proposal | Deferred to v0.2 with AliceMemory | | | | 3 |
| F9 | Two workers on the same task | `agent.step` occurrence N starts a second agent worker | same | same_args; different_args (the second worker picks the second-cheapest acceptable supplier) | all | 1, 2 |
| F10 | Status answer delayed or stale | `sim.status.answer`, for the next `count` answers | same, if the agent calls the status tool | delayed (the answer arrives after T, so the probe counts as unanswered); stale within L; stale beyond L (answers reflect the state when F10 fired, a contract violation); `count` (default 20) | C1 | 1, 5 |
| F11 | Agent killed and resumed from checkpoint | Kill at `agent.after_tool_call` (default) or `agent.after_tool_result` | same | point; `restart_delay_ms` | all | 1, 2, 5 |
| F12 | Request lost before it reaches the destination (new) | `net.{hop}.request`: dropped | `net.agent_sim.request` | hop: `agent_gw` or `gw_sim` | all | 4, 5 |

Notes:

- F6 was "Approval revoked before dispatch" and fired in "Admin API". It now fires in the approvals service (0002). The early variant tests whether the stack checks authority. The late variant tests the race between the check and the commit.
- F7's `harmless` variant is a control. Every configuration should pass it, which shows the freshness rules do not reject harmless changes.
- F9 was "Two workers propose the same action". A worker is one instance of the agent graph with its own checkpoint thread and its own budgets, running the same policy on the same task (0008, 0009).
- F10 needs a status probe, so its matrix cells pair it with F2. The "stale within L" cells set `status_lag_ms` to 3,000 in the scenario's parameters (0009), because the default L is 0.
- F12 is new. It is the counterpart of F2. Without it, every lost message in the catalog happens after a commit, and C0 looks safer than it is.
- F8 is deferred, because it adds a second project to the critical path while F7 already exercises property 3. The fact model treats memory entries as ordinary facts, so F8 will need no change to property 3.

### Plans

A scenario's fault plan is a fixed list:

```yaml
faults:
  - fault: F2
    at: {point: net.gw_sim.answer, match: {api: submit_purchase_order, answer: committed}, occurrence: 1}
  - fault: F11
    at: {point: agent.after_tool_call, match: {tool: submit_purchase_order}, occurrence: 1}
    params: {restart_delay_ms: 10000}
```

or a random plan drawn from the run seed, which the Hypothesis suites use:

```yaml
faults:
  random: {from: [F2, F4, F5, F12], max: 3}
```

### Expected outcomes

`faults/expected.yaml` holds the expected verdict and failing properties for every cell (fault variant, contract, configuration) of the standard development scenario, `dev-001` (0009). Deterministic tests assert every cell.

The claims below are fixed now. The rest of `expected.yaml` is filled from the first reviewed deterministic run and frozen before any live-model run. If that run contradicts a claim here, the claim is revisited by decision record. Nobody edits an expectation to match a result.

The G2 cells below fail or stay unresolved by design. They are documented limits, not bugs. Every other G2 cell is expected to pass. The failing properties are those of `dev-001`, whose budget covers one order but not two; another scenario can fail a different set.

| Cell | Expected G2 verdict | Why |
| --- | --- | --- |
| F1b at C0 | fail (4) | The request may never have left, and C0 offers no way to tell. Safety is kept; liveness is lost. |
| F12 (`gw_sim`) at C0 | fail (4) | Same reason |
| F4 (`gw_sim`) at C0 and C1 | fail (1, 2, 3) | The transport duplicated the request. Without keys, the destination commits a second order that cites the same approval and overdraws the budget. |
| F5 beyond deadline at C1 | fail (1, 3) | The destination broke its deadline, so Failed was wrong. The agent's second order committed, then the late first one overdrew the budget. |
| F6 late at C0 to C2 | fail (2) | Authority changed after the check and before the commit |
| F7 late, `price_up`, at C0 to C2 | fail (3) | The quote changed after the check and before the commit |
| F2 with F10 stale beyond L, at C1 | fail (5) | The stale status made Failed wrong. The agent's second try was rejected, because the first order had used the budget, so the report said "not done" while an order existed. |
| F2, F3 and F5 (both variants) at C0, with no resolver | pass_unresolved | The honest answer is "unknown", and a human must reconcile |

For B0 with the scripted policy (0008), we expect these results:

- **C0 to C2, faults that make B0 resend** (F2, F3, F4, F5, F9 and F11): B0 fails, because it resends without keys.
- **C0 to C2, F6 and F7 `price_up`:** B0 fails, because it checks nothing before writing.
- **C3:** the destination's atomic checks and single-use approvals block some duplicates and stale writes. Some failures move to property 5. The first reviewed run records these cells.
- **F1, F7 `harmless`, F10 and F12:** B0 should pass.

### Test rule

This replaces the rule in CLAUDE.md ("Every fault type gets a deterministic test that fails on baseline B0 and passes on gateway G2") and the matching success measure in product.md:

> Every fault variant has deterministic tests for every applicable (contract, configuration) cell, asserting the verdict and failing properties in `faults/expected.yaml`. For every fault except F1, F10 and F12, at least one cell shows B0 failing where G2 passes. F1, F10 and F12 test gateway recovery and status handling, which B0 does not have.

### Demos

Each demo runs B0 and G2 on the same seed and shows them side by side.

| Demo | Contract | Faults | What it shows |
| --- | --- | --- | --- |
| 1. Order placed, reply lost | C2 | F2 on the purchase order's answer; F11 at `agent.after_tool_call` on the same call, with `restart_delay_ms` 10,000 | In G2, the gateway retries with the same key while the agent is down and gets the stored result. The resumed agent re-sends the same proposal, which attaches to the confirmed action. One order. B0 re-sends after resuming and places a second order that reuses the first approval and overdraws the budget. |
| 2a. Approval goes stale: quote | C2 | F7 early, `price_up` | G2 refuses to send and names the fact (`quote/S-1/W-100`, version 1 to 2, field `unit_price_minor`). The agent re-reads and re-proposes. B0 orders at the old price. |
| 2b. Approval goes stale: revoked | C2 | F6 early | G2 refuses to send and names the revoked approval. B0 orders anyway. |
| 3. Outcome unknown | C0 | F2, with no resolver | G2 reports `outcome_unknown` and what to reconcile. The final report says "unknown", and the verdict is pass_unresolved. B0 orders twice. |

Demo 2 uses a variant of `dev-001` whose order is above the auto-approval threshold, so a human approves the order that later goes stale.

### Milestones

- M1: F2 and F11 at C2 only (demo 1). F11 moves up from M2, because demo 1 needs it.
- M2: F1, F6, F7, F9 and F12, and contract levels C0 to C3. F1 moves up from M3 ("the remaining faults"), because recovery belongs with the state machine work.
- M3: F3, F4, F5 and F10, and demos 2 and 3.
- v0.2: F8.

## Alternatives considered

- **Wall-clock triggers.** Not reproducible. Rejected; the spec already says so.
- **One global event counter.** Any new event anywhere would shift every plan. Rejected in favour of per-point counters.
- **Fault actions as scheduled messages.** Seeded latency would let an "early" revocation arrive after the check it targets. Rejected in favour of applying them at the point.
- **Keep "every fault fails B0 and passes G2".** F1 and F12 do not hurt B0, and some G2 cells are genuine limits. Forcing the rule would mean designing faults to make B0 look bad. Rejected.
- **No F12.** Every lost message would then follow a commit, which makes C0 look safer than it is. Rejected.
- **Pull F8 into v0.1.** Deferred, as explained above.

## Consequences

- Every fault means one thing in every configuration.
- The matrix is the experimental hypothesis, and it can be reviewed before any money is spent on live models.
- plan.md: M1 gains F11, M2 gains F1 and F12.
- Spec changes on acceptance:
  - The fault catalog table: points, variants, B0 equivalents and F12. F6 is renamed and moves to the approvals service. F9 is renamed. Properties at risk change for F1, F3 and F11.
  - The Testing section.
  - The Demos section in product.md.
  - The Tests rule in CLAUDE.md, and the matching success measure in product.md.
