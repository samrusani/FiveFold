# 0010: State machine corrections found while implementing 0003

Status: Accepted (2026-11-14), with the recommended option for item 6
Date: 2026-11-14
Spec sections affected: Action state machine (T5, T11, T12, escalation, dispatch, final report), Authority (validity checks), Fault catalog (transport rules)

## Context

Building the state machine module (`src/fivefold/gateway/state_machine.py` and `steps.py`) meant walking every path through records 0002, 0003, 0005 and 0006. An independent review of the module then walked the fault scenarios again. Together they found paths that the accepted records leave without an outcome, or with a wrong one:

1. **An approval that is already dead at the first poll.** Record 0002, rule 14, moves a grant from pending to revoked or expired when "the first poll that sees a decision finds the approval already revoked or expired". The action is then in PendingApproval, and the only way out is T5 to Rejected. T5's reason codes are `approval_denied`, `approval_timeout`, `stale_input` and `run_closed`. None of them says what happened. This happens when someone revokes an approval between the approver's decision and the gateway's next poll (at most `approval_poll_interval`, 500 virtual ms, later), or when `approval_ttl_ms` is shorter than that interval. In v0.1, fault F6 fires only at dispatch points, so a human revoking an approval in a demo is the realistic case.
2. **Two reason codes can never fire.** T11 lists `retry_committed` and T12 lists `retry_rejected`, both from OutcomeUnknown. But T13 moves an action from OutcomeUnknown to Dispatched before the retry is sent, so the retry's answer arrives in Dispatched and fires T8, T9 or T10.
3. **An answer can reach a gateway that has forgotten the request.** The transport drops an answer after the caller's timeout, and drops messages to a component that is down (0006). Neither covers a gateway that crashes and restarts quickly. Example at C0: the attempt is sent at vt 0; another fault crashes the gateway at vt 5 with `restart_delay_ms` 10; recovery moves the action to OutcomeUnknown; the committed answer arrives at vt 60, before the timeout. No transition accepts it, so the gateway drops it. The evaluator still counts it as evidence (0004), so a correct G2 stack fails property 5.
4. **Expiry is predictable, but the check ignores it.** Rule 17 of 0002 sends only if the approval is "not expired at the current virtual time". An approval that expires one millisecond later passes, and at C0 to C2 the effect can then commit after `expires_at`, which fails property 2. Unlike a revocation, the gateway knows `expires_at` in advance.
5. **Accepting a report and closing the run are separate steps.** Record 0003 accepts `not_done` while an action has not been dispatched, because closing the run will reject that action. Nothing makes the close happen together with the report. A dispatch can run in between, and with two workers (F9) the run stays open while the other worker continues. If the action then commits and the accepted report is the one that counts, property 5 fails through timing, which 0004 says cannot happen to a G2 stack that follows its rules.

The module implements the accepted records exactly. For case 1 it raises `UnspecifiedCase` and changes nothing; a unit test pins this. The two dead reasons stay legal in the table and are never produced. Cases 3 to 5 are not handled in the module.

Several more rules are implied by the accepted records but not written in them. This record states them, so the text and the code say the same thing. The module follows readings 7 to 10, 12 and 14. Reading 11 belongs to the dispatcher and reading 13 to the transport layer, neither of which is built yet.

## Decision

### Changes

1. **T5 gains `approval_revoked` and `approval_expired`.** When a poll finds the approval already revoked or expired, the action moves from PendingApproval to Rejected with that reason, and the grant moves from pending to revoked or expired (0002, rule 14). The agent sees `rejected` with the reason, which the scripted policy already handles: "rejected because the approval was denied, revoked, expired or timed out: stop working on the intent" (0008).
2. **T11 loses `retry_committed`, and T12 loses `retry_rejected`.** A retry's answer fires T8, T9 or T10 from Dispatched. The event's details carry the attempt number, so the log still shows which attempt the answer belongs to. Record 0005's C2 and C3 rows ("committed or replayed gives Confirmed; a definitive rejection gives Failed") do not change.
3. **A crash drops the answers to the crashed component's open requests** (amends 0006, transport rules). When a component crashes, the transport drops every later answer to a request that component sent before the crash. This matches an HTTP client whose connections closed when it died, and it extends the rule 0006 already has for timeouts. The gateway's recovery is unchanged: it never expects an answer in OutcomeUnknown, and the evaluator does not count an answer that was never delivered.
4. **An approval that expires before a send could commit counts as expired** (amends 0002, rule 17). The check passes only if `expires_at > vt + S`, where `vt` is the time of the check and `S` the settle time (0005). A request sent at `vt` commits by `vt + S` or never, so an effect sent under a passing check always commits before the approval expires. This applies to every check the gateway makes, before the first send and before a C2 retry. C3 is unchanged, because the destination checks expiry at commit. With the example scenario's values (`approval_ttl_ms` 3,600,000, S 10,050), an approval loses its last 0.3% of life.

### Reports and run closing

6. **The final report and the run's close must not be separable** (case 5). Decided:
   - The gateway closes the run in the same transaction that accepts the last report it expects. The run config says how many agent workers report (0009).
   - An accepted `not_done` claim from an earlier report closes its intent in the same transaction: the gateway rejects the intent's undispatched actions with reason `run_closed`, and refuses later proposals for that intent with `run_closed`. The claim then stays true whichever report counts.

   Alternatives considered: accept `not_done` only when every action of the intent is final. This needs no new rule, but an agent waiting for a human approval could then report nothing true until `approval_wait_ms` passed. Or count only the report of the last worker to stop. This conflicts with 0004's "the last valid report before the run stops counts".

### Readings confirmed

7. **Escalation ends automatic recovery.** After the `escalated` note, the gateway sends no more status probes (C1) and no more retries (C2, C3). Only an attestation moves the action. This is how the module reads "Exits: ... escalation" in 0005's recovery table.
8. **"Not committed" can be attested only after the settle time.** The gateway accepts such an attestation only at or after `S` past the action's latest send. Record 0004 counts an attestation as evidence only from that time, and says the gateway applies the same settle rules for T11 and T12. The scripted resolver already attests only after the settle time (0009). An attestation that names an effect is accepted at any time.
9. **The retry backoff counts from the latest send.** The next retry may go `T`, then `2T`, `4T` and `8T` (the cap) after the latest attempt was sent. Record 0005 does not say what the backoff counts from. Counting from the send keeps retries at least `T` apart, which 0005 needs so that a definitive rejection of a retry settles every attempt before it.
10. **Dead authority is named before a stale basis.** When a check finds both, T7 reports `approval_revoked` or `approval_expired`, not `stale_input`. When a poll finds a denial and a stale basis, T5 reports `approval_denied`. Re-reading the facts cannot revive authority, and a new action needs a new approval anyway.
11. **A scope mismatch at a check is an internal error, not a transition.** Rule 17 of 0002 checks that the approval is in scope for the action's args. The scope comes from those args when the request is filed (rule 5), and an action's args never change, so a mismatch means the approvals service is broken. The gateway does not send, and reports an internal error instead of choosing a reason code.
12. **The report check counts every effect the gateway learned.** "The effect ids the gateway recorded for the intent" (0003, final report) includes every effect a C1 status answer listed, not only the one the action records (I1). When the transport delivered a request twice (F4) and the reply was lost, a status answer can list two effects. The evaluator counts both as known (0004), and 0003's testing plan requires the report check and the evaluator to agree in every run without a contract violation, which F4 is not.
13. **A malformed answer is reported as `server_error`.** Record 0003 classes a malformed answer as unknown but gives T10 no reason code for it. A proxy's error page is the usual malformed answer, so the transport layer reports it like a 5xx.
14. **A timeout needs a final poll that still reads pending.** Rule 14 of 0002 times out a request with "no decision within `approval_wait_ms`". The gateway checks this instead of assuming it: if the authority decided just before the deadline, the decision is acted on, so no approval is left valid and unused (which would block property 4's `authority_withdrawn` excuse).

## Alternatives considered

- **Case 1 as T4 then T7.** Move the action to Authorized with reason `approved`, and let the dispatch check reject it with `approval_revoked`. This needs no new reason codes. But it moves an action to Authorized when the gateway already knows the approval is dead, skips 0002's direct grant move from pending to revoked, and adds a step that does nothing. Rejected.
- **Case 1 as `approval_denied`.** The log and the agent would say "denied" about an approval that was granted and then withdrawn. The report explains failures from these reasons, so the explanation would be wrong. Rejected.
- **Case 1 as waiting for `approval_timeout`.** The action would wait `approval_wait_ms` (600,000 virtual ms in the example scenario) for nothing, and the grant cannot move from revoked to timed_out. Rejected.
- **Keep the two dead reasons.** Harmless to the code, but the table would promise transitions that cannot happen, and readers of the report and the viewer would look for them. Rejected.
- **Send retries from OutcomeUnknown, so T11 and T12 carry retry answers.** This makes the dead reasons live, but it changes T13, the outbox pattern and I3's rule that no event comes between a check and its send. A larger change for no gain. Rejected.
- **Case 3 as new reason codes on T11 and T12 for answers after a restart.** The gateway could use such an answer. But it reopens the late-answer path that T13's design avoids, and recovery at C1 to C3 settles the action anyway. At C0 the cost is one escalation. Rejected in favour of change 3.
- **Case 4 as a documented race, like revocation.** Consistent with rule 21, but a revocation cannot be foreseen and an expiry can. Rejected.
- **Keep probing after a C1 escalation (reading 7).** A later probe could still confirm the action without a human. But 0005 lists escalation as an exit, and a gateway that keeps acting after handing a case to a human makes the human's view stale. Rejected for v0.1.

## Consequences

- The agent can be told `rejected` with `approval_revoked` or `approval_expired` before an action was ever authorized.
- The transition table loses two reason codes and gains two. Tests that pin the table change with it.
- `UnspecifiedCase` and its unit test go away once case 1 has an outcome.
- The harness's transport drops answers addressed to a crashed incarnation; fault tests for crash points gain a check that it does.
- Approvals stop being usable `S` before they expire.
- The admin API refuses a "not committed" attestation made before the settle time, with a message that says when it becomes possible.
- Spec changes on acceptance:
  - 0003: an "Amended by 0010" line under the status, the T5, T11 and T12 rows, the escalation section and the final report section.
  - 0002: an "Amended by 0010" line, and rule 17.
  - 0006: an "Amended by 0010" line, and the transport rules.
  - `docs/spec.md`: the same rows of the transition table, the escalation bullet, the Authority section's check, and the Fault catalog's transport rules.
- Code changes on acceptance: the table in `src/fivefold/gateway/state_machine.py`; the poll, dispatch and retry steps in `steps.py`; the report step if the open problem is decided as recommended; the expected table and the case 1 test in `tests/unit/`.
