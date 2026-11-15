# 0011: The scripted policy after a `run_closed` rejection

Status: Accepted (2026-11-15)
Date: 2026-11-15
Spec sections affected: Configurations (the scripted agent policy, through record 0008)

## Context

Record 0008's table says what the scripted policy does with each result of a write. Refusals have a catch-all row: "refused for any other reason: stop working on the intent". Rejections do not. The table covers an action rejected for stale inputs, and one rejected because the approval was denied, revoked, expired or timed out, but no other reason.

Record 0010 added a reason that falls in that gap. An accepted `not_done` claim closes its intent: the gateway moves the intent's undispatched actions to Rejected with reason `run_closed`, and refuses later proposals for the intent with `run_closed`. Closing the whole run does the same, but by then every worker has stopped.

Fault F9 (two workers on the same task, milestone M2) makes the case reachable. One worker reports `not_done` for an intent while the other still has an action for it that has not been dispatched. The second worker's next result is then that action, rejected with `run_closed`. The refusal of a later proposal is already covered by the catch-all row.

The code already handles the case one way (`src/fivefold/agents/policy.py`): it stops working on the intent, and step 5 then reports `not_done`, because the intent "ended rejected". Record 0008 says the policy changes only by decision record, so this record proposes the row instead of leaving it to a reading.

## Decision

Add one row to 0008's table, after the row for authority rejections:

| Result | Next step |
| --- | --- |
| rejected because the intent or the run has closed (`run_closed`) | Stop working on the intent |

With step 5, the claim for the intent is `not_done`. The claim is true, and the stack can prove it: the gateway accepts a `not_done` claim only when none of the intent's actions was dispatched (0010), and once the intent has closed, none can be.

## Alternatives considered

- **Re-read and try again, as after stale inputs.** Every new proposal for the intent is refused with `run_closed`, so retries only spend steps. Rejected.
- **Wait with `get_action_status`.** The action is final; waiting changes nothing. Rejected.
- **Claim `unknown`.** The stack knows that nothing was sent for the intent, so `unknown` fails property 5 (`K_i` is `not_committed`). Rejected.

## Consequences

- No code change: the policy already behaves this way. A unit test pins it (`tests/unit/test_policy.py`).
- The F9 cells of `faults/expected.yaml` may rely on it, when one worker reports `not_done` before the other worker's action is dispatched.

## Spec changes on acceptance

- Record 0008: add the row above to the scripted policy's table, and note "Amended by: 0011".
- `docs/spec.md`, Configurations: none, since the spec refers to 0008 for the policy.
