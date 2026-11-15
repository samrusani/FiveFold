# 0014: Recovery latency

Status: Proposed
Date: 2026-11-15
Spec sections affected: Evaluator (Metrics), Data model (harness MessageLog and FaultLog, eval RunResult), Runs and determinism; records 0004 (Metrics) and 0009 (Determinism contract)

## Context

Record 0004 gives the metric one line: "recovery latency per affected action, from the first fault that touched it to its final state, in virtual and wall milliseconds". Actions and their final states are gateway records (0003). B0 has none, G1 opens one per write call (0008), and the evaluator reads gateway tables only to explain a failure (0004). No record says what a fault touched. The evaluator does not compute the metric yet (`evaluator/metrics.py`).

What the harness records, from its code and from runs of all 221 cells of `faults/expected.yaml` (dev-001, seed 7):

- A `harness.fault_log` row reaches an intent through the message it acted on (`target_seq`), the answer a crash voided (`voided`, F3 in G modes) or the possibly-sent request (`params.request`, F1b). The rows of F1a, B0's F1 and F7 early in G modes name no intent (32 cells). F6 early in G modes reaches it only through its actor-log row and `sim.approval`. F9 names the worker it started.
- `message_log.wall_at` is written at the send; a delivery gets `delivered_seq` and `delivered_vt` but no wall-clock time. LangGraph's retry backoff holds the run in wall time (`harness/clock.py`), so the two can be far apart.
- Playback and the determinism check compare the evaluation JSON exactly, without columns ending in `wall_at`.

## Decision (proposed)

1. **Per intent.** One entry per affected task intent, not per write request or gateway action: intents are the same in B0, G1 and G2, and property 5 keeps its evidence per intent. The metric reads what the properties read, simulator and harness records and the scenario, never the gateway's tables.
2. **Touched.** A fault touches the intent of the message, action or tool call it acted on; F9 touches every task intent. The harness records it in a new column, `harness.fault_log.intent_id`, null for F9 and for a fault on a message without an intent, such as a read. The start is the `vt` of the first fault, by `seq`, that touched the intent. Other intents a crash stalls do not count, since only gateway tables and agent checkpoints show all the work a component held.
3. **End.** The outcome is `committed` when `E_i(end)` is not empty, else `not_committed`. With `K_i(t)` as 0004's `K_i` at harness seq `t` instead of `t_r`, the end is the first seq at or after the start from which `K_i(t)` equals the outcome until the run stops. For `committed`, that is the first commit evidence for one of the intent's effects (a write answer, a C1 status answer or an attestation). For `not_committed`, it is when the last of its write requests, sent or possibly sent, is first shown never to commit, or the start when it sent none. Evidence held before the start gives 0 ms. Evidence after the counted report counts too, since the drain lets reconciliation settle (0009). The latency is the end's `vt` (`delivered_vt`, or the attestation's `vt`) minus the start's. A later duplicate does not move it. An intent with no end is unresolved: no latency, counted apart, with `K_i` at the stop.
4. **Wall clock.** From the start fault's `fault_log.wall_at` to a new `message_log.delivered_wall_at`, set with a delivery's fate, or to the attestation's `actor_log.wall_at`; 0 when the end is the start. It is for metrics only. Its fields end in `wall_ms`, and comparisons that need equal bytes (playback, the determinism check, the report's duplicate check) drop them.
5. **Reporting.** The result gains `recovery`, one entry per affected intent in scenario order: `intent_id`, `fault_seq`, `fault`, `start_vt`, `outcome`, `settled_by` (`write_answer`, `status_answer`, `attestation`, a settlement named in `evaluator/evidence.py`, or `no_write`), `end_seq`, `end_vt`, `latency_ms`, `latency_wall_ms`. An unresolved entry has nulls and `knowledge_at_stop`. `metrics` gains `affected_intents` and `unresolved_intents`, and the CSV adds `recovery_ms_max` and `recovery_wall_ms_max`, the largest latencies among the run's resolved intents. The report adds a table by fault plan, configuration and contract: affected, resolved and unresolved intents, the median and largest virtual latency, and the median wall-clock latency, over resolved intents (lower middle value for an even count). Latencies are never summed.

### Evidence from runs

dev-001 has one intent, REQ-1. Each row is a run on seed 7 with its cell's plan, scored by a probe script outside the repository. Where a fault row names no intent, REQ-1 counts as touched, as the new column would record.

| Cell | Affected | Start: fault, vt | End: what settled it, vt | Latency, virtual ms |
| --- | --- | --- | --- | --- |
| F2, C2, B0 | REQ-1 | F2, 5,311 | answer naming PO-0002, the framework's resend, 7,324 | 2,013 |
| F2, C2, G1 | REQ-1 | F2, 4,648 | answer naming PO-0002, a second G1 action, 6,797 | 2,149 |
| F2, C2, G2 | REQ-1 | F2, 4,815 | replayed answer naming PO-0001, 7,072 | 2,257 |
| F2, C0, G2 | REQ-1 | F2, 4,815 | none: escalated, no resolver | unresolved |
| F1a, C2, G2 | REQ-1 (row names none) | F1a, 4,788 | answer naming PO-0001 after the restart, 6,072 | 1,284 |
| F6 late, C2, G2 | REQ-1 | F6, 4,788 | answer naming PO-0001, 4,853 | 65 |
| F11 `after_tool_call`, C2, B0 | REQ-1 | F11, 5,292 | answer naming PO-0002, the resumed agent's resend, 6,340 | 1,048 |
| F11 `after_tool_call`, C2, G2 | REQ-1 | F11, 4,464 | answer naming PO-0001, to the gateway, 4,869 | 405 |
| F12 on `gw_sim`, C2, G2 | REQ-1 | F12, 4,788 | answer naming PO-0001, the same-key retry, 7,061 | 2,273 |

- Over all 221 cells, B0 has 64 resolved and 4 unresolved intents, G1 74 and 2, G2 71 and 6. G2's six are 0006's C0 limits (F1b, F2, F3, both F5 variants, F12 on `gw_sim`). B0's and G1's are at C3: the first answer never reached the stack, the keyless resend failed the conditional write, and the report said `not_done` while PO-0001 existed.
- F6 early in G2 sends no write: 0 ms. With 0009's resolver, F2 at C0 in G2 ends at the attestation: 31,973 ms.
- In the 63 G2 and 66 G1 cells with a committed outcome and evidence after the fault, the end equals the gateway's own first Confirmed `vt`, which the metric never reads.
- Three runs each of F2 at C2 in B0 and F1b at C2 in G2 gave equal virtual latencies (2,013 and 2,260 ms) but wall-clock ones of 627 to 1,371 ms and 2,016 to 3,296 ms, measured to the answer's send, the only wall-clock time it has.
- Latency alone does not rank configurations: with F2 at C2, B0 and G1 settle about as fast as G2 by ordering twice, which fails properties 1 and 3.

## Alternatives considered

- **Per gateway action**, as 0004 words it. B0 has none, and the gateway would time itself. With F12 at C1 in G2, A-0001 failed 10,520 ms after the fault; the order landed through A-0002 at 15,277 ms.
- **Per write request.** Counts differ by design (G2 retries under one key; B0 and G1 resend), and F1a, F6 and F7 early, F9 and F12 on `agent_gw` act before any request exists.
- **End at the last commit.** It times the ledger, not the stack: 0 ms for F2 at C2 in G2, learned 2,257 ms later; -38 ms for F3 at C2 in G2; 0 ms for F2 at C0 in G2, never learned.
- **End at the counted report.** It times the agent's last steps, including the status check G modes make before reporting (0008): F2 at C2 gives 5,151 ms in G2 and 3,034 ms in B0.
- **End when every effect is known.** B0's F2 cells would never end, so a duplicate would also count as unresolved.
- **Unresolved timed to the stop.** F2 at C0 in G2 stopped at 213,272 ms, after three 60,000 ms waits and the 20,100 ms drain. That times the policy, not recovery.
- **Every task intent for a row that names none**, with no new column. Exact for dev-001, but one crash would mark every intent of a larger task.
- **Wall-clock end at the answer's send**, with no new column: a backoff between send and delivery is lost. **No wall-clock time in the result**: the CSV and the report, which read only result files, could not show it.

## Consequences

- One definition serves B0, G1 and G2 and reuses `evaluator/evidence.py`. A stack that reports `unknown` shows as unresolved, not as slow.
- F9 fires at the first step in the cells, so its latency is the time to the first known order (3,815 ms in G2 at C2).
- Code on acceptance: a migration for the two columns, filled by the harness; `recovery` in the evaluator; wall-clock fields dropped by the comparisons; the report's columns and table (`evaluator/report.py`).
- Tests: golden fixtures for each kind of end and for unresolved intents; a property test that latency is never negative and that wall-clock columns change no virtual value; whole-run tests pinning the table; a G-mode test that the end equals the gateway's first Confirmed `vt` for committed outcomes.

## Spec changes on acceptance

- Record 0004, Metrics: "recovery latency per affected intent, from the first fault that touched it until the stack's evidence settles the intent's outcome at the end, in virtual and wall milliseconds, with unresolved intents counted apart (0014)". Add "Amended by: 0014".
- Record 0009, Determinism contract, and `docs/spec.md`, Runs and determinism: "ignoring wall-clock columns" becomes "ignoring wall-clock columns and fields". Add "Amended by: 0014" to 0009.
- `docs/spec.md`, Evaluator: the same Metrics wording. Data model: MessageLog gains `delivered_wall_at`, FaultLog the intent each fault touched, RunResult the recovery entries.
