# 0003: Action state machine and invariants

Status: Accepted (2026-11-14)
Amended by: 0010 (2026-11-14): T5, T11 and T12 reasons; escalation; final report and run closing; 0015 (2026-11-16): status waits use the scenario limit
Date: 2026-11-14
Spec sections affected: Action state machine, Gateway (write tools, get_action_status, submit_final_report, startup), Data model (Action, Attempt, ActionEvent)
Resolves M0 questions: 8, 9, 10, 11, 12 (state side), 15 (state side), 16; part of 38

## Context

The spec lists seven states and ten transitions. A write can wait for approval, but no state represents the wait. Recovery after a crash before dispatch (F1) is undefined. Escalation on C0 has no exit. What the agent is told is described informally.

This record fixes the states, transitions, dispatch model, recovery, reporting rules and invariants for G2. G1 records its actions in the same tables, but without action identity, recovery or report checks (0008). The invariants are claimed for G2 only.

## Decision

### States

| State | Meaning | Final |
| --- | --- | --- |
| Proposed | Admitted (0001); basis being checked | No |
| PendingApproval | Approval requested; no decision yet | No |
| Authorized | Approved and inputs fresh; waiting to be sent | No |
| Dispatched | An attempt is recorded and may be in flight | No |
| OutcomeUnknown | Sent, and the result is not known | No |
| Confirmed | The destination confirmed one effect | Yes |
| Rejected | Ended before any attempt; nothing was sent | Yes |
| Failed | Sent, and the destination definitively did not commit | Yes |

### Transitions

No other transitions exist. Final states have no outgoing transitions.

| # | From | To | Guard | Reason codes |
| --- | --- | --- | --- | --- |
| T1 | (new) | Proposed | Admission passed (0001) | admitted |
| T2 | Proposed | PendingApproval | Basis fresh; approval request filed (0002) | approval_requested |
| T3 | Proposed | Rejected | A cited fact is stale, or the run has closed | stale_input, run_closed |
| T4 | PendingApproval | Authorized | Approval granted and basis fresh | approved |
| T5 | PendingApproval | Rejected | Denied, timed out, revoked or expired before the gateway saw the approval (0010), a cited fact went stale, or the run has closed | approval_denied, approval_timeout, approval_revoked, approval_expired, stale_input, run_closed |
| T6 | Authorized | Dispatched | I3 check passed; attempt row committed before the send | dispatched |
| T7 | Authorized | Rejected | Authority invalid or basis stale at the dispatch check, or the run has closed | approval_revoked, approval_expired, stale_input, run_closed |
| T8 | Dispatched | Confirmed | The destination answered committed, new or replayed | committed |
| T9 | Dispatched | Failed | The destination answered with a definitive rejection | destination_rejected, precondition_failed |
| T10 | Dispatched | OutcomeUnknown | Timeout, transport error, 5xx, in_progress, or an open attempt found at startup | timeout, transport_error, server_error, in_progress, recovered_open_attempt |
| T11 | OutcomeUnknown | Confirmed | C1 status lists the effect; a human attests committed. A retry's answer arrives in Dispatched and fires T8 (0010). | status_found, human_attested |
| T12 | OutcomeUnknown | Failed | Settled negative (0005): C1 status empty after the settle time; a human attests not committed. A retry's rejection arrives in Dispatched and fires T9 (0010). | status_settled_empty, human_attested |
| T13 | OutcomeUnknown | Dispatched | Retry with the same idempotency key; C2 or C3 only; at C2 the retry check (I3) must pass first | retry |

### Classifying a destination answer

Every answer to an attempt is one of four classes, at every contract level:

| Class | What it is | Definitive |
| --- | --- | --- |
| committed | Success carrying an effect id, including an idempotent replay | Yes |
| rejected | A business or precondition rejection that states nothing was committed | Yes |
| in_progress | C2 and C3 only: a request with the same key is still in flight | No |
| unknown | Timeout, connection error, any 5xx, or a malformed answer | No |

Only definitive answers end an attempt. A 503 is unknown, because a proxy can return it after the backend committed.

### Escalation

Escalation is not a state. The gateway writes an `escalated` note event on an OutcomeUnknown action and opens a `gw.escalation` row:

| Contract | Escalate when |
| --- | --- |
| C0 | At once, on entering OutcomeUnknown |
| C1 | After `max_probes` status queries without a settled answer |
| C2 | When the retry check (I3) fails, or after `max_attempts` |
| C3 | After `max_attempts` |

A human resolves an escalation through the admin API by attesting committed (with the effect id) or not committed. This fires T11 or T12 with reason `human_attested`, and counts as a human intervention. If nobody resolves it, the action stays OutcomeUnknown when the run ends.

Escalation ends automatic recovery: after the `escalated` note, the gateway sends no more status probes or retries. An attestation that nothing committed is accepted only at or after the settle time S past the action's latest send (0010).

### Dispatch: Authorized actions are the outbox

- A dispatcher picks an Authorized action and runs the I3 check without holding any lock, because the check calls the approvals service and reads facts.
- It then locks the action row, confirms that the state and the latest event are unchanged since the check, and commits T6 together with the new attempt row and the check result. Only then does it send. If anything changed, it commits nothing and starts again.
- Retries (T13) follow the same pattern.
- A write tool call runs admission and every step that needs no waiting. When policy approves at once, the call returns the result of the first send (`confirmed`, `failed` or `outcome_unknown`) within `tool_wait_ms` (0005). When approval is pending, the call returns `pending_approval` at once.
- `get_action_status(action_id, wait_ms)` reads gateway state only. It never contacts the destination. With `wait_ms` above 0 (at most 60,000 virtual ms, or the scenario's lower `max_wait_ms`), it returns as soon as the action reaches a final state, or when the wait ends. Reconciliation runs on the gateway's own timers, whether or not the agent asks.
- Network calls never happen inside a database transaction. A transition locks the action row, checks the expected from-state, writes the new state and its event, and commits. A step that finds the action in an unexpected state does nothing.

### Startup recovery

Before serving tools, the gateway processes actions in action-id order:

1. Every action in Dispatched whose latest attempt has no recorded answer moves to OutcomeUnknown (`recovered_open_attempt`).
2. OutcomeUnknown actions resume their contract's recovery (0005).
3. Authorized actions return to the outbox. Each is sent only after a fresh I3 check.
4. PendingApproval actions resume polling the approvals service.
5. Proposed actions resume their basis check.

F1 is therefore safe at every level. A crash before the attempt row leaves the action Authorized (step 3). A crash after the attempt row but before the send leaves an open attempt (step 1). Then C2 and C3 retry safely, C1 probes and fails after the settle time, and C0 escalates.

### Final report and run closing

G2 checks the final report against its records before accepting it, using the evidence rules in 0004:

| Claim | Accepted only if |
| --- | --- |
| done | The intent has a Confirmed action, and `effect_ids` lists exactly the effect ids the gateway recorded for the intent |
| not_done | Every action for the intent is Rejected or Failed, or has not been dispatched, so no attempt is open |
| unknown | At least one attempt for the intent has no settled answer (an action in Dispatched or OutcomeUnknown) |

- A report with any conflicting claim is refused with `report_inconsistent`. The refusal names each conflicting claim and the gateway's record for it, so the agent can correct the report and resubmit.
- The agents' phase ends when every agent worker has submitted a report, stopped with an error, or used up its budget (0009). The gateway then closes the run. It refuses new proposals with `run_closed`, and moves every action not yet dispatched (Proposed, PendingApproval, Authorized) to Rejected with reason `run_closed`. Reconciliation of dispatched actions continues during the drain window.
- Closing stops a late approval from sending an order after the report said it was not done.
- The gateway closes the run in the same transaction that accepts the last report it expects. An accepted `not_done` claim from an earlier report closes its intent in the same transaction: the intent's undispatched actions move to Rejected with reason `run_closed`, and later proposals for it are refused with `run_closed` (0010).
- "The effect ids the gateway recorded for the intent" include every effect a C1 status answer listed (0010).

### What the agent is told

Write tools and `get_action_status` return one of two shapes.

```json
{"result": "action", "action_id": "A-0003", "intent_id": "REQ-1", "state": "outcome_unknown",
 "effect_id": null, "reason": "timeout", "stale": [],
 "reconcile": {"contract": "C0", "how": "escalated_to_human", "escalation_id": "ESC-0001",
               "what": "A purchase order for REQ-1 at S-1 may or may not exist."}}
```

```json
{"result": "refused", "reason": "intent_busy",
 "details": {"action_id": "A-0001", "state": "dispatched"}}
```

- `state` is the action's state in snake_case, read in the transaction that produced the response.
- `effect_id` is set only when `state` is `confirmed`.
- `stale` lists `{key, cited_version, current_version, changed_fields}` when stale input caused a rejection. This is how demo 2 names which fact invalidated which action.
- `reconcile` appears only with `outcome_unknown`. `how` is one of `escalated_to_human`, `querying_status`, `retrying_with_same_key`.
- The spec's four write results map as follows. `confirmed` stays. `rejected` covers Rejected, and the new `failed` covers Failed ("sent, definitively not committed"). `pending_approval` and `outcome_unknown` stay. The states `proposed`, `authorized` and `dispatched` can appear when `tool_wait_ms` runs out first.
- Refusal reasons are those in 0001, plus `run_closed` and, for reports, `report_inconsistent`.
- Read tools return `{read_id, key, version, data}`.

### Invariants

I1 to I6 are restated so each one has a check. I5 now also covers the final report. I7 is new.

| ID | Rule | Check |
| --- | --- | --- |
| I1 | An action records at most one effect id. It is set once, on T8 or T11, and never changes. | A trigger rejects any change to a non-null `effect_id`. Property test across all answer sequences. |
| I2 | An action has at most one grant. Every attempt of the action uses it. A grant is consumed at most once and never moves to another action. | Unique constraints (0002). `attempt.grant_id = action.grant_id`. Property test. |
| I3 | Before every send, in the same dispatch step, the gateway checks the grant (0002, rule 17) and the basis. A first send checks every dependency rule (0004). A C2 retry checks only the exact rules: if the first attempt landed, the action's own commit has already changed the budget or inventory, so a sufficient rule would fail for the wrong reason. C3 retries are exempt, because the destination checks atomically. | Each attempt row stores the check result and `check_vt`. Property test: every attempt except C3 retries has a passing check of the right kind, and no other event on the action between the check and the attempt. |
| I4 | Retries happen only at C2 and C3, and every attempt of an action carries the same idempotency key. At C0 and C1, an action has at most one attempt. | Count attempts per action by contract; count distinct keys per action. |
| I5 | Every agent-facing result reports the action's state at the moment the result is produced. `confirmed` appears only for Confirmed actions. OutcomeUnknown is never reported as confirmed, failed or rejected. The gateway accepts no final report whose claims contradict its records. | Every response is logged in `gw.tool_call` with its seq and reported state, and compared with the state derived from `gw.action_event` at that seq. Every accepted report is re-checked against the records at its seq. |
| I6 | Every state change writes exactly one `action_event` row, in the same transaction that changes `action.state`, while holding a row lock on the action. `action_event` is append-only. | Replaying `action_event` reproduces `action.state`. The gateway role has no UPDATE or DELETE privilege on `action_event`, and a trigger also rejects both. A test asserts each. |
| I7 | At most one action per intent is live or Confirmed. G1's actions, which have no identity, are exempt. | Partial unique index on `gw.action (intent_id)` over the live and Confirmed states, for rows with `identity` on. Property test. |

### Data model

- `action`: action_id, task_id, intent_id, operation, target, args, action_key, identity (on for G2, off for G1), basis (fact key to version), state, grant_id, idempotency_key, effect_id, contract, created_vt, updated_vt.
- `attempt`: attempt_id, action_id, attempt_no, idempotency_key, grant_id, check (kind, approval status seen, the approval's expiry when approved (0010), versions seen, check_vt), sent_vt, answer_class, answer_ref, answered_vt.
- `action_event`: seq, action_id, kind (transition or note), from_state, to_state (equal for notes), reason, details, vt, wall_at.
- `escalation`: escalation_id, action_id, opened_vt, resolved_vt, resolution, effect_id, actor.
- `tool_call`: seq, tool, args, result, action_id, vt.
- `intent`: intent_id, operation, closed. One row per task intent, created at run start; the lock anchor for 0001. An accepted `not_done` claim closes it (0010).
- `report`: worker, call_no. Each agent worker's first accepted final report (0010).

### Testing plan

1. A pure transition module is the only code that decides whether a transition is legal. Database code calls it.
2. A Hypothesis rule-based state machine runs against an in-memory reference model. Its rules: propose, approve, deny, revoke, expire, change a fact, dispatch, answer (committed, rejected, in_progress, unknown), crash and restart, probe, retry, attest, report. After every step it asserts I1 to I7 and agreement with the model.
3. The same machine then runs against Postgres, with the in-memory model as the oracle.
4. A property test asserts that the gateway's report check and the evaluator's evidence rules (0004) agree in every run without a contract-violation fault.

## Alternatives considered

- **Seven states, with the approval wait inside Proposed.** This hides a long wait inside a state whose name means "just arrived", and the agent cannot be told `pending_approval` truthfully (I5). Rejected.
- **Escalated as its own state.** Resolution would then need a transition out of Escalated that duplicates T11 and T12. A note event plus an escalation row keeps the table smaller. Rejected.
- **Retry directly from Dispatched on errors that look like they happened before commit.** A 503 can come from a proxy after the backend committed. Only definitive answers count. Rejected.
- **Allow Failed to Confirmed when a late commit appears.** That happens only when the destination breaks its contract (0005). Keeping Failed final keeps the invariants simple, and the evaluator still scores from the ledger. Rejected for v0.1.
- **Synchronous dispatch only, with no outbox.** Simpler, but an approval that arrives after the tool call returns would need the agent to call again, and a crash between approval and send would lose the action (F1). Rejected.
- **Purely asynchronous tools that always return pending.** Honest, but agents would waste steps polling even when approval is instant. Rejected.
- **Hold the row lock during the I3 check.** Simpler to reason about, but it holds a lock across network calls. Optimistic confirmation gives the same guarantee. Rejected.
- **Accept any final report and let the evaluator judge.** Closer to a pure benchmark, but a correct stack could fail property 5 by timing alone: evidence can reach the gateway between the agent's last status check and its report. A gateway that can stop false success claims should stop them. Rejected.

## Consequences

- Eight states and thirteen transitions replace the spec's table, including the line "On a C0 tool, Outcome unknown escalates to a human instead of retrying."
- There is a new agent-facing result, `failed`, and a `refused` shape. Agents must handle `intent_busy`, `intent_done`, `run_closed` and `report_inconsistent`.
- I7 is new. I5 extends to the final report. I1 to I6 have exact checks.
- Spec changes on acceptance:
  - The Action state machine section.
  - The write tool return values.
  - `get_action_status` gains `wait_ms`.
  - `submit_final_report` checks claims in G2.
  - The Data model gains Escalation, ToolCall and Intent, and Action, Attempt and ActionEvent gain the fields above.
  - Every "I1 to I6" in CLAUDE.md, plan.md and spec.md becomes "I1 to I7".
