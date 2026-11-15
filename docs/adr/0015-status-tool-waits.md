# 0015: Status-tool waits and the scenario's `max_wait_ms`

Status: Proposed
Date: 2026-11-15
Spec sections affected: Gateway (`get_action_status`), Tool contracts (parameters, through record 0005), Configurations (the scripted agent policy, through record 0008), Simulator (the scenario file, through record 0009)

## Context

Three accepted texts bear on how long a status tool waits, and they stop agreeing once a scenario changes the limit.

- **Record 0005** lists "Longest `wait_ms` accepted by status tools: 60,000" among the gateway settings, and says scenarios can override every value in their `params` block. The scenario file carries it as `params.max_wait_ms`.
- **Record 0008** pins the scripted policy's waits: B0 waits for an approval with `get_approval_status(wait_ms = 60,000)`, at most 3 times, and G1 and G2 wait on an action with `get_action_status(wait_ms = 60,000)`, at most 3 times. Changing the policy needs a decision record.
- **The gateway's published schema** for `get_action_status` allows `wait_ms` from 0 to 60,000 (record 0003; `docs/spec.md`, Gateway). B0's `get_approval_status` has no maximum in its schema.

Until now no code read `max_wait_ms`. An external audit of 7329d7a found that a B0 model could therefore make the simulator poll a pending approval for as long as it asked: one call with `wait_ms` 3,600,000 made 7,201 approval lookups. The fix, on branch `audit-wait` and not yet merged, enforces the run's value everywhere a status wait is checked:

- B0's `get_approval_status` refuses a longer wait before anything is sent, with the validation error it gives for any invalid argument;
- the simulator's endpoint refuses one as a request that does not parse, so one wait makes at most 121 lookups with the defaults;
- the gateway's `get_action_status` refuses one as `invalid_args`, while its published schema keeps 60,000;
- the agent's timeout for a gateway status call counts at most `max_wait_ms` of wait.

The published schemas do not change. No scenario in the repository sets `max_wait_ms`, and every cell of `faults/expected.yaml` runs with the default, so nothing that runs today behaves differently. Two gaps remain, both outside what the fix may decide.

**Gap 1: a scenario that lowers `max_wait_ms` below 60,000 breaks the scripted policy.** The policy still asks for 60,000 ms, and the limit refuses it. Runs of dev-002 (demo 2's scenario: the order waits for a human approval) at C2 with seed 7, on the fix:

| `max_wait_ms` | Config | What happens | Verdict |
| --- | --- | --- | --- |
| 60,000 (default) | B0 | The agent waits, the approver approves APR-0001 at 6,274 ms, PO-0001 commits, the report says `done` | pass |
| 60,000 (default) | G2 | The same, through the gateway; the approver approves at 6,648 ms | pass |
| 5,000 | B0 | The policy's 60,000 ms wait is refused before it is sent. The policy reads the refusal as "not approved", stops work on the intent and reports `not_done`, although the approver approved at 6,274 ms. No order. | fail (4, `missing_effect`) |
| 5,000 | G2 | The gateway refuses the 60,000 ms wait with `invalid_args`. The policy reads the tool error as an action result and raises `KeyError: 'action_id'`, so the worker ends with an error and no report. The agents' phase ends, G2 closes the run, and the order is never sent. | fail (4, `missing_effect`; 5, `no_report`) |

**Gap 2: a scenario that raises `max_wait_ms` above 60,000 reaches B0 only.** B0's schema has no maximum, so with `max_wait_ms` 120,000 B0's `get_approval_status` accepts a wait of 90,000 ms. The gateway's `get_action_status` keeps its published maximum and refuses the same wait: `wait_ms: Input should be less than or equal to 60000`. The scripted policy never asks for more than 60,000 ms, so scripted runs do not differ, but a live model that asks for more is treated differently in B0 and in the G modes.

## Options

**A. The scripted policy waits the smaller of 60,000 ms and `max_wait_ms`.** It amends steps 2 and 4 of 0008's policy; the policy still waits at most 3 times.

- Closes gap 1: every scenario the format accepts can run the scripted policy.
- With the default limit, every wait is the same as today, so the expected outcomes, the demos and their digests do not change.
- A lower limit shortens how long the scripted agent waits in all: 3 × 5,000 = 15,000 ms with the limit at 5,000. An approver slower than that leaves the approval unused. That is what the scenario asks for.
- Gap 2 stays open.

**B. The scenario format accepts `max_wait_ms` only from 0 to 60,000.** A scenario that asks for more is refused when it is loaded.

- Closes gap 2: no run can allow B0 a wait that the gateway's schema refuses, so B0 and the G modes are bounded by the same value.
- The gateway's published schema stays the same in every run. Tool schemas are part of every model request and its hash (`docs/spec.md`, Playback and branching), so requests stay comparable across scenarios.
- It narrows 0005's "scenarios can override every value" for one parameter: a scenario can lower it but not raise it.
- Gap 1 stays open.

**C. The gateway's schema follows the scenario.** `get_action_status` publishes the run's `max_wait_ms` as its maximum, and its description says so.

- Closes gap 2 and keeps raising the limit possible.
- The published input schema, and so every request hash, then depends on the scenario. Two scenarios that differ only in `max_wait_ms` give a model different tool schemas, which a comparison across scenarios would have to allow for.
- 0003 and `docs/spec.md` describe the tool's limit as the scenario's.
- Gap 1 stays open.

**D. `max_wait_ms` stops being a scenario parameter.** Every run uses 60,000.

- Closes both gaps with the least code: the limit, the policy and the schema always agree.
- No scenario can shorten status waits, for example to keep a run with a slow approver short. None does today.
- It removes a field from the scenario format and amends 0005's "every value".

**E. Keep the texts as they are.** The fix stays, and the scenario documentation warns against changing `max_wait_ms`.

- No record changes.
- Both gaps stay. A scenario that lowers the limit makes the scripted G2 worker fail with an error that looks like a bug in the stack.

## Decision

Recommended: options A and B together.

1. The scripted policy asks for `min(60,000, max_wait_ms)` ms in each wait: `get_approval_status` in B0, and `get_action_status` in G1 and G2, including the wait on another worker's action after `intent_busy`. Each still waits at most 3 times.
2. The scenario file accepts `params.max_wait_ms` from 0 to 60,000 ms. A larger value is refused when the scenario loads.
3. The status tools keep their published schemas: no maximum in B0's `get_approval_status`, and at most 60,000 in the gateway's `get_action_status`. Each refuses a wait above the run's `max_wait_ms` when it validates the call, as branch `audit-wait` does.

A test can check each rule:

- dev-002 at C2 with `max_wait_ms` 5,000 passes in B0 and G2 with the scripted policy, and the policy's waits ask for 5,000 ms;
- a scenario with `max_wait_ms` 60,001 is refused by the loader;
- the deterministic runs of the demos and of every cell of `faults/expected.yaml` give the same records as before, since they use the default.

A and B together close both gaps and change nothing that runs with the default. The published tool schemas stay fixed, so requests and cassettes stay comparable across scenarios, and the limit can still be lowered. If a study later needs waits longer than 60,000 ms, option C can be added by another record.

## Alternatives considered

- **A alone, or B alone.** Each closes one gap and leaves the other.
- **C, with or without A.** C keeps raising possible, but the published schema and every request hash would then vary by scenario. No scenario needs a wait above 60,000 ms.
- **D.** Simplest, but it removes the one way to shorten status waits that 0005 grants, and gives nothing that A and B do not.
- **E.** Leaves a scenario parameter that, once changed, fails a correct stack.
- **A, with more waits when the limit is lower,** so that the policy waits 180,000 ms in all whatever the limit. It changes "at most 3 times" in 0008 and the number of steps a run uses, for a case no scenario has. Rejected.

## Consequences

- The scripted policy takes the run's `max_wait_ms` (`src/fivefold/agents/policy.py`, built in `src/fivefold/harness/run.py`). With the default the waits do not change, so the demos, the expected outcomes and their digests do not change.
- `Params.max_wait_ms` (`src/fivefold/domain/scenario.py`) and `scenarios/scenario.schema.json` gain a maximum of 60,000.
- Tests: dev-002 at C2 with `max_wait_ms` 5,000 passes in B0 and G2, and the loader refuses 60,001. On branch `audit-wait`, the tests of the tools stay as they are; the whole-run test that pins the B0 policy's refused waits at a 5,000 ms limit changes to expect waits of 5,000 ms.
- A study or report that compares runs across scenarios sees the same tool schemas in every run.

## Spec changes on acceptance

- Record 0005, parameters: the row "Longest `wait_ms` accepted by status tools" says a scenario can lower it, to no less than 0, and cannot raise it above 60,000. The sentence "Scenarios can override every value" names this exception. Add "Amended by: 0015".
- Record 0008, the scripted agent policy: step 2 and the row for `pending_approval`, `proposed`, `authorized`, `dispatched` and `outcome_unknown` say `wait_ms` = the smaller of 60,000 and the scenario's `max_wait_ms`. Add "Amended by: 0015".
- Record 0009, scenario file: the `params` bullet says `max_wait_ms` is at most 60,000.
- Record 0003, the section on dispatch and what the agent is told: `get_action_status` waits at most 60,000 virtual ms, or the scenario's lower `max_wait_ms`.
- `docs/spec.md`, Gateway: the `get_action_status` row says "at most 60,000, or the scenario's lower `max_wait_ms`". Tool contracts: the sentence "Scenarios can override any value" names the cap on `max_wait_ms`.
