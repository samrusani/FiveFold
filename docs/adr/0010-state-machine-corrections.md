# 0010: State machine corrections found while implementing 0003

Status: Proposed
Date: 2026-11-14
Spec sections affected: Action state machine (T5, T11, T12, escalation, dispatch), Authority (validity checks)

## Context

Building the state machine module (`src/fivefold/gateway/state_machine.py` and `steps.py`) meant walking every path through records 0002, 0003 and 0005. Two paths do not fit the accepted tables:

1. **An approval that is already dead at the first poll.** Record 0002, rule 14, moves a grant from pending to revoked or expired when "the first poll that sees a decision finds the approval already revoked or expired". The action is then in PendingApproval, and the only way out is T5 to Rejected. T5's reason codes are `approval_denied`, `approval_timeout`, `stale_input` and `run_closed`. None of them says what happened. This happens when someone revokes an approval between the approver's decision and the gateway's next poll (at most `approval_poll_interval`, 500 virtual ms, later), or when `approval_ttl_ms` is shorter than that interval. In v0.1, fault F6 fires only at dispatch points, so a human revoking an approval in a demo is the realistic case.
2. **Two reason codes can never fire.** T11 lists `retry_committed` and T12 lists `retry_rejected`, both from OutcomeUnknown. But T13 moves an action from OutcomeUnknown to Dispatched before the retry is sent, so the retry's answer arrives in Dispatched and fires T8, T9 or T10. No answer can reach an action in OutcomeUnknown: the transport drops any answer that comes after the caller's timeout (0006), and the other non-definitive answers are the attempt's only answer.

The module implements the accepted tables exactly. For case 1 it raises `UnspecifiedCase` and changes nothing; a unit test pins this. The two dead reasons stay legal in the table and are never produced.

Five more rules are implied by the accepted records but not written in them. The module follows readings 3 to 6 below; reading 7 is for the dispatcher, which is not built yet. This record asks the maintainer to confirm them, so the text and the code say the same thing.

## Decision

### Changes

1. **T5 gains `approval_revoked` and `approval_expired`.** When a poll finds the approval already revoked or expired, the action moves from PendingApproval to Rejected with that reason, and the grant moves from pending to revoked or expired (0002, rule 14). The agent sees `rejected` with the reason, which the scripted policy already handles: "rejected because the approval was denied, revoked, expired or timed out: stop working on the intent" (0008).
2. **T11 loses `retry_committed`, and T12 loses `retry_rejected`.** A retry's answer fires T8, T9 or T10 from Dispatched. The event's details carry the attempt number, so the log still shows which attempt the answer belongs to. Record 0005's C2 and C3 rows ("committed or replayed gives Confirmed; a definitive rejection gives Failed") do not change.

### Clarifications

3. **Escalation ends automatic recovery.** After the `escalated` note, the gateway sends no more status probes (C1) and no more retries (C2, C3). Only an attestation moves the action. This is how the module reads "Exits: ... escalation" in 0005's recovery table.
4. **"Not committed" can be attested only after the settle time.** The gateway accepts such an attestation only at or after `S` past the action's latest send. Record 0004 counts an attestation as evidence only from that time, and says the gateway applies the same settle rules for T11 and T12. The scripted resolver already attests only after the settle time (0009). An attestation that names an effect is accepted at any time.
5. **The retry backoff counts from the latest send.** The next retry may go `T`, then `2T`, `4T` and `8T` (the cap) after the latest attempt was sent. Record 0005 does not say what the backoff counts from. Counting from the send keeps retries at least `T` apart, which 0005 needs so that a definitive rejection of a retry settles every attempt before it.
6. **A check that finds both dead authority and a stale basis names the authority.** T7 reports `approval_revoked` or `approval_expired`, not `stale_input`. Dead authority is the stronger reason to stop: re-reading the facts cannot revive it, and a new action needs a new approval anyway.
7. **A scope mismatch at a check is an internal error, not a transition.** Rule 17 of 0002 checks that the approval is in scope for the action's args. The scope comes from those args when the request is filed (rule 5), and an action's args never change, so a mismatch means the approvals service is broken. The gateway does not send, and reports an internal error instead of choosing a reason code.

## Alternatives considered

- **Case 1 as T4 then T7.** Move the action to Authorized with reason `approved`, and let the dispatch check reject it with `approval_revoked`. This needs no new reason codes. But it moves an action to Authorized when the gateway already knows the approval is dead, skips 0002's direct grant move from pending to revoked, and adds a step that does nothing. Rejected.
- **Case 1 as `approval_denied`.** The log and the agent would say "denied" about an approval that was granted and then withdrawn. The report explains failures from these reasons, so the explanation would be wrong. Rejected.
- **Case 1 as waiting for `approval_timeout`.** The action would wait `approval_wait_ms` (600,000 virtual ms in the example scenario) for nothing, and the grant cannot move from revoked to timed_out. Rejected.
- **Keep the two dead reasons.** Harmless to the code, but the table would promise transitions that cannot happen, and readers of the report and the viewer would look for them. Rejected.
- **Send retries from OutcomeUnknown, so T11 and T12 carry retry answers.** This makes the dead reasons live, but it changes T13, the outbox pattern and I3's rule that no event comes between a check and its send. A larger change for no gain. Rejected.
- **Keep probing after a C1 escalation (clarification 3).** A later probe could still confirm the action without a human. But 0005 lists escalation as an exit, and a gateway that keeps acting after handing a case to a human makes the human's view stale. Rejected for v0.1.

## Consequences

- The agent can be told `rejected` with `approval_revoked` or `approval_expired` before an action was ever authorized.
- The transition table loses two reason codes and gains two. Tests that pin the table change with it.
- `UnspecifiedCase` and its unit test go away once case 1 has an outcome.
- The admin API refuses a "not committed" attestation made before the settle time, with a message that says when it becomes possible.
- Spec changes on acceptance:
  - 0003: an "Amended by 0010" line under the status, the T5, T11 and T12 rows, and the escalation section.
  - `docs/spec.md`: the same rows of the transition table, and the escalation bullet.
- Code changes on acceptance: the table in `src/fivefold/gateway/state_machine.py`, the poll step in `steps.py`, and the expected table and the case 1 test in `tests/unit/`.
