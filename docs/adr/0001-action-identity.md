# 0001: Action identity

Status: Accepted (2026-11-14)
Date: 2026-11-14
Spec sections affected: Data model (Action identity, Proposal, Action, Freshness), Gateway (write tools)
Resolves M0 questions: 12, 17, 18, 19 (gateway side); part of 38

## Context

The gateway must recognize when two write proposals are the same logical effect. Agents restart, replan and resume from checkpoints, and LangGraph re-runs a tool call when it resumes a node that did not finish. If the gateway treats a repeated proposal as new, it can create a second effect. If it merges proposals that differ, it can commit something the agent did not ask for.

The spec defines `action_key` as a hash of run, task intent, operation, target resource and normalized arguments, minus volatile fields. It does not say where the intent comes from, which fields are volatile, or what happens when a proposal matches an action that has already ended.

## Decision

These rules describe G2. G1 has no action identity (0008).

### Intents

1. Every task lists its intents. An intent is one unit of work with a stable id, such as the purchase requisition `REQ-1`. Scenarios define intents (0009).
2. Each intent names exactly one operation: `submit_purchase_order` or `reserve_inventory`.
3. Every write proposal carries `intent_id`. The gateway refuses a proposal whose `intent_id` is not in the task (`unknown_intent`), or whose operation differs from the intent's operation (`wrong_operation`).
4. The agent cannot create intents. If a task wants two identical orders, it lists two intents.

### The action key

5. `action_key = SHA-256(canonical_json({task_id, intent_id, operation, target, args}))`.
   - `task_id` comes from the scenario. A run holds one task, so `task_id` scopes the key to the run, and a replay of the same run produces the same keys.
   - `target` is derived from args: `supplier/{supplier_id}` for purchase orders, `inventory/{warehouse_id}/{sku}` for reservations.
   - `args` holds only the operation's business arguments listed below, after normalization.
6. Canonical JSON: UTF-8, object keys sorted, separators `,` and `:` with no spaces, non-ASCII characters written as-is. Values are strings and integers only. Money is an integer in minor units plus an ISO 4217 currency code. Quantities are integers. Floats are refused.
7. Normalization: ids must match their documented format exactly (for example `S-1`, `W-100`, `CC-OPS`). The gateway refuses a non-matching id (`invalid_args`) instead of rewriting it. Currency codes are uppercased.

| Operation | Business args in the key | Volatile, excluded from the key |
| --- | --- | --- |
| submit_purchase_order | supplier_id, sku, quantity, unit_price_minor, currency, cost_center | based_on, note, any field outside the schema |
| reserve_inventory | warehouse_id, sku, quantity | based_on, note, any field outside the schema |

Price is part of the key. An order at a different price is a different action.

### Matching a proposal to an action

8. A **live** action is in Proposed, PendingApproval, Authorized, Dispatched or OutcomeUnknown. A **finished** action is in Confirmed, Rejected or Failed (0003).
9. At most one action per intent is live or Confirmed at any time. This is invariant I7 (0003). A partial unique index enforces it.
10. When a proposal arrives, the gateway locks the intent's row and applies the matching rule:

| Existing actions for the intent | Proposal has the same action_key | Proposal has a different action_key |
| --- | --- | --- |
| None, or only Rejected and Failed ones | Create a new action | Create a new action |
| One live action | Attach: return the existing action's state | Refuse with `intent_busy`, giving the existing action id, state and args |
| One Confirmed action | Attach: return `confirmed` with the effect id | Refuse with `intent_done`, giving the existing action id and effect id |

11. Attaching never creates an attempt, a grant or an approval request.
12. A finished action never changes state again. Trying again after Rejected or Failed creates a new action with a new id, a new grant and a new idempotency key. This is safe because Rejected means nothing was sent, and Failed means the destination definitively did not commit (0003, 0005).

### based_on

13. Every read tool returns a `read_id`. `based_on` is a list of read_ids. The gateway resolves each one to (fact key, version) from its own read records.
14. The gateway refuses a proposal with `unknown_read` if any read_id was not issued by the gateway in this run.
15. The gateway refuses a proposal with `missing_basis` if `based_on` does not cover every dependency the operation declares (0004, dependency rules). Extra citations are allowed. The gateway records them on the proposal but neither checks them nor sends them, because property 3 judges only the declared dependencies (0004).
16. When a proposal attaches to an action that has not been dispatched, the gateway replaces the action's basis only if every cited dependency version is at least as new as the version already held for that key. Otherwise it keeps the existing basis. After the first dispatch, the basis is frozen.

### Admission order

17. The gateway checks a proposal in this order and stops at the first failure: schema (`invalid_args`), intent (`unknown_intent`, `wrong_operation`), reads (`unknown_read`, `missing_basis`), then the matching rule in item 10. Every proposal, admitted or refused, is stored as a Proposal row with its outcome.

### Checks

- Unit: golden vectors pin the key for fixed inputs, so the key cannot drift across processes or library versions.
- Unit: changing any business arg changes the key. Changing `based_on` or `note` does not.
- Property (Hypothesis): for any sequence of proposals for one intent, at most one action is live or Confirmed at any time, and every proposal with the key of the live or Confirmed action attaches to it.
- Database: inserting a second live action for an intent fails on the partial unique index.

## Alternatives considered

- **No intent in the key (the spec minus intent).** Two identical orders that the task wants separately would merge into one action. A replanned order with a different quantity would become a second order. Rejected.
- **Free-text intent written by the agent.** A model rewrites free text after a resume, so the key changes and the duplicate guard fails. Rejected.
- **Deduplicate by action_key only, without one action per intent.** This catches exact re-proposals but not replans with different args, which is the replay case studied in "Beyond Single-Use Tokens: Durable Authorization State for Replay-Resistant LLM Agent Actions" (CapLease, arXiv 2608.01710, a preprint). Rejected.
- **Auto-supersede: a new proposal replaces a live action that has not been sent.** The agent could change its mind without a new tool, but two workers with different args could replace each other forever. Rejected for v0.1. A `cancel_action` tool can come later if scenarios need it.
- **Leave price out of the key.** Orders at different prices would merge, so the approved price and the sent price could differ. Rejected.

## Consequences

- Write tools gain a required `intent_id`, and task texts must name their intents.
- After a crash, a model that replans cannot place a second order for an intent whose first order is live or done. This is the main duplicate guard, and it works at every contract level.
- An agent cannot withdraw a live proposal in v0.1. It waits for approval, denial or the approval timeout.
- New invariant I7 (0003). New refusal reasons: `invalid_args`, `unknown_intent`, `wrong_operation`, `unknown_read`, `missing_basis`, `intent_busy`, `intent_done`.
- Spec changes on acceptance: the "Action identity" and "Freshness" paragraphs; Proposal gains `intent_id` and an outcome; Action gains `task_id`, `intent_id`, `operation`, `target`, `args` and `basis`; a `gw.intent` table holds one row per task intent as the lock anchor.
