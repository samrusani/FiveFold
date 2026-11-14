# 0002: Authority: approvals and grants

Status: Accepted (2026-11-14)
Date: 2026-11-14
Spec sections affected: Data model (Grant), Gateway (admin API, policy), Simulator (approvals service), Fault catalog (F6), Evaluator (property 2)
Resolves M0 questions: 3, 13, 14, 22; parts of 1, 2 and 38

## Context

Property 2 asks whether each committed effect had valid authority when it committed. The spec puts grants in the gateway, an approvals service in the simulator, authorized limits in the scenario and approval thresholds in a gateway policy file. Four places can disagree. B0 has no gateway, so a grant held by the gateway cannot score B0.

There is a second problem. If the evaluator judges authority from the gateway's grant table, the system under test grades itself.

## Decision

### One system of record

1. The simulator's approvals service is the system of record for authority. It plays the role of the business's approval system, such as an ERP or a ticketing tool.
2. Each run's policy lives in the scenario's `authority` section (0009). The approvals service enforces it. The gateway has no policy file of its own in v0.1.
3. Every write needs exactly one approval, whatever its size. Policy approves small writes at once; large ones wait for a scripted approver. This keeps property 2 uniform: every effect cites one approval.

### Approvals (simulator)

4. An approval request carries `intent_id`, `operation`, `scope` and a client request key. A repeated request with the same client request key returns the original request. The gateway uses the action id as this key, so a gateway crash cannot file a request twice.
5. Scope fields:
   - purchase orders: `intent_id, operation, supplier_id, sku, max_quantity, max_amount_minor, currency, cost_center`
   - reservations: `intent_id, operation, warehouse_id, sku, max_quantity`

   A write is in scope when every named field matches, and its quantity and amount do not exceed the maxima.
6. Policy decides each request on arrival:

| Condition | Decision |
| --- | --- |
| Supplier not on the allowlist, or amount above `per_order_ceiling_minor` | Denied by policy |
| Purchase order at or below `auto_approve_up_to_minor`, or any reservation | Approved by policy |
| Any other purchase order | Queued for the scripted approver |

7. The scripted approver follows rules in the scenario: a match on request fields, a decision (approve or deny), and a delay in virtual time. It never sees the agent's messages.
8. An approval expires at `decided_vt + approval_ttl_ms`. The scripted approver or fault F6 can revoke it at any time. Revocation is permanent.
9. Every change is an append-only approval event: requested, approved, denied, revoked. Expiry needs no event, because `expires_at` determines it.
10. An approval backs at most one effect. The simulator records which effect cited it when the effect commits.
11. At C0 to C2, the purchase-order and inventory services record the cited approval but do not check it. At C3 they check it atomically at commit: approved, not revoked, not expired, in scope, and not already cited by another committed effect (0005).

### Grants (gateway)

12. A grant is the gateway's binding of one approval to one action. The gateway creates it when it files the approval request for that action. It never moves to another action. While the grant is pending, the gateway polls the approvals service every `approval_poll_interval` (0005).
13. Each action has at most one grant. Each grant has exactly one action. Each approval is bound to at most one grant.
14. Grant states:

| From | To | When |
| --- | --- | --- |
| (new) | pending | Approval request filed |
| pending | active | The authority approved it |
| pending | denied | The authority denied it |
| pending | timed_out | No decision within the scenario's `approval_wait_ms` |
| pending | released | The action ended before a decision, for example on stale input or when the run closed |
| pending | revoked or expired | The first poll that sees a decision finds the approval already revoked or expired |
| active | consumed | The action's first attempt is recorded (Authorized to Dispatched) |
| active | released | The action ended before any attempt |
| active or consumed | revoked | The gateway observes a revocation |
| active or consumed | expired | The gateway observes expiry |

15. A grant is consumed once, at the first dispatch. Retries of the same action reuse it. No action ever consumes a second grant.
16. A grant in released, denied, timed_out, revoked or expired never backs a send.

### Validity checks

17. Before every send, the gateway asks the approvals service for the approval's current status. It sends only if the approval is approved, not revoked, not expired at the current virtual time, and in scope for the action's args. This check runs before the first dispatch and before every C2 retry. Before a C2 retry, the gateway also re-checks the exact dependency rules (0003, I3).
18. If the check fails before the first send, the action moves to Rejected (`approval_revoked` or `approval_expired`) and the grant to revoked or expired.
19. If the check fails before a C2 retry, the gateway does not send. The action stays OutcomeUnknown and is escalated (0003). Sending could commit a new effect under dead authority if the first attempt never landed.
20. C3 retries skip the gateway's check, because the destination checks authority atomically. It returns the stored result if the first attempt committed, and rejects otherwise (0005).
21. At C0 to C2, the check and the commit are separate steps. A revocation between them can let an effect commit without valid authority. This race is a documented limit. The late variant of fault F6 measures it, and C3 closes it.

### Admin API

22. The gateway's admin API lists actions, grants and events, and resolves escalations (0003). It does not issue or revoke approvals. In a demo, the viewer's approve and revoke buttons call the approvals service, acting as the human approver.

### Checks

- Property 2 is scored from simulator records only (0004).
- Database: `gw.grant` has unique `action_id` and unique `approval_id`. Every attempt's `grant_id` equals its action's `grant_id`.
- Hypothesis: across random sequences of approve, deny, revoke, expire and dispatch, no attempt is sent under an approval the authority reported invalid at check time, and no grant is consumed twice.
- Scenario tests: F6 early makes B0 fail property 2 at C0 to C2, and G2 pass at every level. F6 late makes G2 fail property 2 at C0 to C2 and pass at C3.

## Alternatives considered

- **Grants live only in the gateway (the spec as written).** Simple, and close to single-use grant gateways. But B0 cannot be scored on property 2, and the evaluator would grade the gateway with the gateway's own records. Rejected.
- **The gateway issues auto-approvals itself from a policy file.** This keeps the spec's YAML policy in the gateway, but the system under test would write the authority records it is graded on. Rejected for v0.1. Agent-specific guardrails in the gateway (the spec's later Cedar adapter) can sit on top of the authority system, not replace it.
- **Re-bind an unused approval to a new action for the same intent.** This saves a human round trip after a stale-input rejection. But the human approved a decision made on facts that have since changed, which is the failure studied in CommitGuard (arXiv 2607.10487, a preprint). Rejected.
- **Consume the grant at Confirmed instead of at first dispatch.** The difference only matters if grants can move between actions, which rule 12 forbids. Consuming at dispatch records that the grant has been used to send. Chosen.
- **Retry at C2 even when authority has lapsed.** Better liveness, but it can commit a new effect without valid authority. Rejected; the action stays unknown and a human decides.

## Consequences

- One authority source scores every configuration, including B0 and, later, B1 and B2.
- B0 gets two tools, `request_approval` and `get_approval_status` (0008). Without a gateway, the agent must obtain approvals itself.
- Fault F6 moves from "Admin API" to "approvals service, through the scripted approver" (0006).
- At C2, G2 gives up liveness to keep safety when authority lapses between attempts. Those runs end with unresolved outcomes, and the report counts them.
- Spec changes on acceptance: Grant fields and states; the admin API's scope; "Policy in v0.1 is YAML rules" becomes "policy lives in the scenario, and the approvals service enforces it"; the simulator section names the approvals service as the system of record for authority.
