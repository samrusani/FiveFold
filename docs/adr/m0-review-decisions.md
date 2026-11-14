# M0 review: questions and decisions

During the M0 design review on 14 Nov 2026, a close read of the docs raised 44 questions. The maintainer asked for a recommended answer to each. Each answer below sits inside a decision record. The maintainer accepted all nine records on 14 Nov 2026, and their changes are applied to the docs.

## Changes to items the maintainer owns

CLAUDE.md reserves these for the maintainer, who accepted these changes on 14 Nov 2026:

- **States:** a new state, PendingApproval (0003).
- **Invariants:** a new invariant, I7, allowing one live or confirmed action per intent. I5 extends to the final report. I1 to I6 are restated with exact checks, and every "I1 to I6" in the docs becomes "I1 to I7" (0003).
- **Tool names and return shapes:**
  - Write tools gain `intent_id` (0001).
  - Read tools return `read_id` (0003).
  - There is a new result, `failed`, and a new `refused` shape (0003).
  - `get_action_status` gains `wait_ms`, and G2 checks final reports against its records (0003).
  - B0 gets its own tool surface, including `request_approval` (0008).
- **Property definitions (0004):**
  - Property 1 becomes the upper bound only.
  - Property 2 is judged from the approvals service, and each effect cites one approval.
  - Property 3 uses exact and sufficient dependency rules, with a basis on every write.
  - Property 4 gains excuses.
  - Property 5 is judged against the evidence the stack had.
- **Grant lifecycle:** approvals move to the simulator as the system of record. The gateway's grant becomes a binding of one approval to one action (0002).
- **Tool contract levels (0005):**
  - C3's conditional write also checks the approval.
  - Destinations publish a commit deadline and a status lag.
  - C2 retries skip the sufficient rules.
- **Fault IDs (0006):**
  - F12 is new.
  - F1 splits into variants a and b.
  - F6 is renamed and fires in the approvals service.
  - F9 is renamed.
  - F8 is deferred to v0.2.
- **Plan and product:**
  - Demos are pinned.
  - M1 gains F11, and M2 gains F1 and F12 (0006).
  - The rule "every fault fails on B0 and passes on G2" becomes an expected-results matrix (0006).
  - The gateway has no policy file in v0.1 (0002).
  - The repo layout moves to `src/fivefold/` (0007).
  - The trust-boundary hard rule is reworded for baselines (0008).
  - The evaluator hard rule now says it decides from simulator and harness records only (0004).

## Questions and decisions

| # | Question | Decision | Record |
| --- | --- | --- | --- |
| 1 | B0 writes directly, against the trust boundary | Baselines write directly by design. G modes enforce the boundary with roles, privileges and networks. | 0008 |
| 2 | Properties 2 and 3 cannot score B0 | Score only from simulator and harness records, with one definition for every configuration | 0004 |
| 3 | Four homes for authority | The simulator's approvals service is the system of record, the scenario holds the policy, and the gateway's grant is a binding | 0002 |
| 4 | Demo 1 does not match M1 | Demo 1 is F2 plus F11 at C2. F11 moves to M1. | 0006 |
| 5 | Demo 1's contract level | C2. M1 builds C2 only. | 0005, 0006 |
| 6 | "Fails on B0, passes on G2" cannot hold for every fault | An expected-results matrix with documented G2 limits | 0006 |
| 7 | Properties 1 and 4 overlap | Property 1 is the upper bound (at most one per intent, nothing unintended). Property 4 is the lower bound. | 0004 |
| 8 | No state for pending approval | New state PendingApproval | 0003 |
| 9 | Synchronous or asynchronous dispatch | Authorized actions form an outbox. Tool calls wait a bounded time. | 0003 |
| 10 | F1 recovery undefined | Authorized actions return to the outbox, and open attempts become OutcomeUnknown. F1 gets variants a and b. | 0003, 0006 |
| 11 | C0 escalation has no exit | An escalation note, then human attestation, counted as an intervention. Otherwise unresolved. | 0003 |
| 12 | Re-proposing after Rejected or Failed | Finished actions stay finished. A retry is a new action. A rule covers basis updates when proposals attach. | 0001 |
| 13 | When a grant is consumed | At first dispatch. Released if the action ends before then. | 0002 |
| 14 | Checks before a retry | C2 re-checks authority and the exact rules, and if the check fails the action stays unknown and is escalated. C3 relies on the destination's atomic check. | 0002, 0003, 0005 |
| 15 | C1 "Failed" under F5 and F10 | A settle time of D plus maximum latency. Contract violations are documented limits. Failed stays final. | 0005 |
| 16 | Transient errors and get_action_status | Only definitive answers end an attempt. Status reads gateway state only, and can wait. | 0003 |
| 17 | Where task intent comes from | Scenario-defined intents (requisitions). The agent passes intent_id. One live action per intent. | 0001 |
| 18 | Volatile fields | A table per operation. Price is part of the key. | 0001 |
| 19 | Empty based_on | based_on must cover the operation's dependencies. The evaluator fails undeclared inputs. | 0001, 0004 |
| 20 | "Declared harmless" | Exact and sufficient rules in code, listed in 0004. Never declared by the agent. | 0004 |
| 21 | Property 3 needs fact history | `sim.fact_version` | 0004 |
| 22 | Grant scope format | Field lists per operation; one approval per action | 0002 |
| 23 | Dispatch time versus commit time | Score at commit. The race at C0 to C2 is a documented limit, and C3 closes it. | 0002, 0004, 0005 |
| 24 | Matching ledger rows to required effects | By the intent_id that every write carries | 0004 |
| 25 | Extra effects | Unintended effects fail property 1 | 0004 |
| 26 | Whether an honest "unknown" passes | A separate verdict, pass_unresolved | 0004 |
| 27 | Final report schema | One claim per intent. The last valid report counts. No report fails property 5. | 0004 |
| 28 | Effect granularity | One write is one effect. Budget changes are fact changes. No compensation in v0.1. | 0004 |
| 29 | Virtual clock | One per run, owned by the scheduler, used by every component including the gateway | 0009 |
| 30 | Event counters and fault points | Per point, per run, with a fixed list of named points | 0006 |
| 31 | Determinism needs a model stand-in | A scripted policy behind the same LangGraph graph | 0008, 0009 |
| 32 | What `make demo` does | Executes demo 1 with the scripted model. Replay re-executes runs from cassettes. | 0009 |
| 33 | F4's hop and the MCP transport | F4 takes a hop parameter. MCP runs in-process (inproc) or over streamable HTTP (split). | 0006, 0009 |
| 34 | What "two workers" means in F9 | Two agent workers on the same task, the second started at a step count, with the same or different args | 0006, 0009 |
| 35 | F8 timing | Deferred to v0.2. The fact model already fits memory entries. | 0006 |
| 36 | Whether contract levels are cumulative | Not cumulative. Three capability flags, with presets exactly as the spec lists them. | 0005 |
| 37 | G1 and B0 behaviour | Pinned and checked against library source | 0008 |
| 38 | Gaps in the data model | Fields defined across 0001 to 0004 and 0009. Consolidated into the spec on acceptance. | 0001 to 0004, 0009 |
| 39 | Schemas and roles | Five schemas, six roles, and a privilege table | 0008 |
| 40 | Shared transactions | Impossible by privileges. Access only through APIs. | 0008 |
| 41 | What a branch restores | Replay to event N. One database per run. | 0009 |
| 42 | No record for interface schemas | Tool results in 0003, the report in 0004 and the scenario format in 0009. Two new records, 0008 and 0009. | 0003, 0004, 0009 |
| 43 | Package naming | `src/fivefold/...` | 0007 |
| 44 | One ticket per pull request | One branch with sequential pull requests, one ticket each | 0007 |

## Changes after the independent review

An independent reviewer read all nine drafts against the original docs and walked through concrete failure cases. It reported 17 findings. All are fixed in the current drafts:

1. C2 retries no longer re-check the sufficient rules. The action's own earlier commit would fail them, so demo 1 could never reconcile (0002, 0003, 0005).
2. The scripted agent waited too little:
   - It now waits with `get_action_status(wait_ms)`, covers every state and refusal, and tries again after Failed.
   - Each model call takes `think_ms` of virtual time.
   - Demo 1 keeps the agent down for 10,000 virtual ms, so the gateway reconciles while it is down (0003, 0006, 0008, 0009).
3. G1's actions are exempt from I7, and its `gateway_timeout` is raised as a server error (0003, 0008).
4. The documented G2 limits now list every failing property on `dev-001` (0006).
5. The dispatcher runs its check without holding a lock, then confirms and commits (0003).
6. Fault actions apply at their point instead of arriving as delayed messages (0006).
7. Extra citations are recorded but not checked, and the scripted agent cites only dependencies (0001, 0008).
8. G2 checks final reports against its records and closes the run after the agents report, so timing alone cannot fail property 5 (0003, 0004).
9. F9 workers, their budgets and the counted report are defined (0006, 0009).
10. F10 cells pair it with F2 and set L, and the probe timeout is defined. F4 has a copy delay, and scenarios get a `params` block (0005, 0006, 0009).
11. The budget rule uses `limit_minor` minus `committed_minor` (0004).
12. Missing spec-change entries are now listed (0003, 0004, 0006, this file).
13. The timing of `expected.yaml` is stated precisely (0006).
14. Idempotency keys are described as unique within a run (0005).
15. Grants can leave pending as released, revoked or expired. The approval poll interval is set, and B0 waits for approvals with `wait_ms` (0002, 0005, 0008).
16. The infeasible excuse ignores the run's own spending (0004).
17. Style: the excuse rule is split into steps, long sentences are split, and preprints cite arXiv IDs (0001, 0002, 0004, 0006).

## Decided alongside the records

- Name: Fivefold. License: Apache-2.0. The maintainer chose both on 14 Nov 2026.
- AliceMemory: v0.2, as record 0006 sets.

## Still open

- Live-study models and the spend cap, after the pilot.
