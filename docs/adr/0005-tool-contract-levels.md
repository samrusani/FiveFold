# 0005: Tool contract levels

Status: Accepted (2026-11-14)
Amended by: 0015 (2026-11-16): the status wait limit can be lowered, not raised
Date: 2026-11-14
Spec sections affected: Tool contracts, Simulator, Gateway (recovery)
Resolves M0 questions: 5, 15, 36; parts of 14 and 23

## Context

The contract a destination offers decides what the gateway can safely do after a lost reply. The spec names four levels. It does not say whether the levels are cumulative, how long a request can stay in flight, how fresh a status answer is, or what C3's conditional write checks.

## Decision

### Capabilities

Three independent capabilities:

| Capability | What the destination does |
| --- | --- |
| status | Answers "which effects exist for intent `i`", with the time the answer is accurate to (`as_of_vt`). This is a business lookup, such as "find purchase orders for requisition REQ-1". |
| idempotency_key | Stores the key with each committed effect. A request whose key already committed gets the stored result, marked as a replay, and creates nothing. A request whose key is still in flight gets `in_progress`. Key uniqueness is enforced inside the commit transaction, so even a late duplicate cannot create a second effect. |
| conditional_write | Inside the commit transaction, checks the request's basis with the dependency rules (0004) and its approval (0002, rule 11). On any failure it rejects with `precondition_failed` and names each failed check. |

At C3 the destination checks in this order: key lookup first (replay or `in_progress`), then preconditions, then commit. A retry of a committed action always gets the stored result, even if its inputs have gone stale since.

### Levels

The presets match the spec's table exactly. They are not cumulative.

| Level | Capabilities |
| --- | --- |
| C0 | none |
| C1 | status |
| C2 | idempotency_key |
| C3 | idempotency_key, conditional_write |

Run configs may combine capabilities for experiments. The published matrix uses only these four presets.

### Parameters

Every destination publishes these promises. Defaults are in virtual milliseconds.

| Parameter | Promise | Default |
| --- | --- | --- |
| `max_hop_latency` | Every message arrives within this time on one hop, or it counts as dropped | 50 |
| `commit_deadline` (D) | A request not committed within D of its arrival is aborted and never commits | 10,000 |
| `status_lag` (L) | A status answer reflects every commit up to its `as_of_vt`, which is at most L before the answer is produced | 0 |

Gateway settings. These are not promises by the destination.

| Setting | Default |
| --- | --- |
| `dispatch_timeout` (T); must exceed `2 × max_hop_latency` | 2,000 |
| Retry backoff at C2 and C3 | T, then doubling, capped at 8 × T |
| `max_attempts` at C2 and C3 | 5 |
| Status probe interval at C1 | 1,000 |
| `max_probes` at C1 | 20 |
| A status probe with no answer within T | Counts as unanswered |
| `tool_wait_ms`: how long a write tool call waits before returning the current state | 2,500 |
| `approval_poll_interval` | 500 |
| Longest `wait_ms` accepted by status tools | 60,000, or a scenario value from 0 to 60,000 |

Scenarios can override every value in their `params` block (0009), except that `max_wait_ms` cannot exceed 60,000. Each run records the values it used. The simulator keeps its promises unless a fault breaks one. Such faults are marked as contract violations in 0006.

### Gateway behaviour after a non-definitive answer

Let `S = max_hop_latency + commit_deadline`, the settle time.

| Level | What G2 does from OutcomeUnknown | Exits |
| --- | --- | --- |
| C0 | Nothing it can do safely. It escalates at once (0003). | Confirmed or Failed only by human attestation; otherwise unresolved at run end |
| C1 | Probes status at every interval. A listed effect gives Confirmed. An empty answer gives Failed only when `as_of_vt >= sent_vt + S` for the action's attempt. It never retries. | Confirmed, Failed, or escalation after `max_probes` |
| C2 | Retries with the same key, but only after the retry check passes: authority (0002, rule 17) and the exact dependency rules (0003, I3). Sufficient rules are not re-checked, because the action's own commit, if the first attempt landed, has already changed the budget or inventory. Committed or replayed gives Confirmed. A definitive rejection gives Failed. `in_progress` or unknown means wait and retry. | Confirmed, Failed, or escalation |
| C3 | Retries with the same key without a gateway pre-check; the destination decides atomically. | Confirmed, Failed, or escalation after `max_attempts` |

These are the same settle rules the evaluator uses for property 5 (0004). Retries are at least T apart, and T exceeds `max_hop_latency`, so a definitive rejection of a retry always settles the earlier attempts.

### Idempotency keys

- Each action has one key: its action id. Every attempt of the action sends it (I4).
- A new action for the same intent gets a new key. This is safe, because the previous action is Rejected (never sent) or Failed (settled negative).
- Keys are unique within a run. Each run has its own simulator database (0009), so keys from different runs never meet.

### What each level guarantees when the destination keeps its contract

| Guarantee | C0 | C1 | C2 | C3 |
| --- | --- | --- | --- | --- |
| No duplicate after a lost reply (property 1) | yes, by never retrying | yes | yes | yes |
| No duplicate when the transport delivers a request twice on the gateway-to-destination hop | no | no | yes | yes |
| The required effect lands after a lost request (property 4) | no | yes, after the settle time and a re-proposal by the agent | yes | yes |
| Authority and freshness hold at commit, not only at dispatch (properties 2 and 3) | no | no | no | yes |
| A retry cannot overdraw the budget or inventory when the first attempt never landed | yes, no retries | yes, no retries | no | yes |
| The report is honest (property 5) | yes | yes | yes | yes |

Every "no" is a documented limit. The report states them before any results.

### M1

M1 builds C2 only, for demo 1. C0, C1 and C3 arrive in M2.

## Alternatives considered

- **Cumulative levels, where C2 includes status.** Closer to some real APIs. But the spec lists capabilities per level, and independent flags let us measure each capability's contribution. Rejected as the default; configs can still combine flags.
- **C3 checks versions only (ETag equality), not approvals.** More like common APIs. But the authority race would stay open at every level, while plan.md expects C3 to close the race windows. ETag equality on budgets would also reject harmless changes. Rejected. The simulator's C3 applies the same rules as the evaluator.
- **No commit deadline.** Then C1 could never conclude "not committed", and C1 would equal C0 for lost requests. Real systems bound request lifetimes, and the "beyond deadline" variant of F5 shows what happens when one does not. Rejected.
- **Check idempotency keys on arrival only.** A late duplicate could still commit. Rejected in favour of uniqueness at commit.

## Consequences

- Contract violations become explicit fault variants, not hidden assumptions.
- At C1, declaring a lost request failed takes up to S (about 10 virtual seconds). Virtual time makes this free in tests.
- Spec changes on acceptance: the Tool contracts section gains the capability definitions, the parameters and the guarantee table; C3's description extends to approvals.
