# 0018: B1, the Temporal baseline

Status: Proposed
Date: 2026-11-18
Spec sections affected: Configurations, Simulator (determinism), Testing, Data model (Run)
Amends on acceptance: 0009 (Temporal scheduling in B1 only)

## Context

B1 measures what durable execution and idempotent Activities do about lost
replies and worker crashes. Like B0 it holds a simulator credential and writes
directly (0008). It uses the same scripted model, task, intents, authority
policy and worker budgets. The five properties still read the simulator and
harness records (0004).

## Decision

### Workflow, Activities and identity

Use a Workflow for one agent worker's conversation, next model or tool step
and budget accounting. Run the same scripted model adapter as B0 in one
Activity per model call. Use one Activity per direct simulator tool call and
one for the final report to the harness sink. Keep all three on FiveFold's
scheduler for application work. Temporal retains completed Activity results
for replay. The Workflow issues commands and uses the recorded results
to rebuild its conversation and budgets.

Temporal says to put “non-deterministic operations like API calls, LLM/AI
invocations, database queries, and other external interactions” in
Activities. It also says “Activities that perform writes should be
idempotent.” See [Workflow Definition](https://docs.temporal.io/workflow-definition#deterministic-constraints)
and [Activity Definition](https://docs.temporal.io/activity-definition#best-practices-for-defining-activities).

Use the existing direct tool surface and request shapes. At C2 and C3, for
each order or reservation Activity, derive the destination's
`idempotency_key` as SHA-256 of the canonical JSON array containing FiveFold's
logical run ID, worker name and Activity ID. Assign the Activity ID from the
Workflow step and Activity kind. Each retry of one Activity sends the same
key. A later tool step or a second worker has its own key. Temporal's
server-generated identifiers and attempt number never enter the key.
This follows Temporal's recommendation to pair workflow and Activity
identity while keeping the key reproducible from the seed. See
[Activity Definition, Idempotency](https://docs.temporal.io/activity-definition#idempotency).

At C0 and C1 the simulator has no idempotency key. The same retry sends the
same business arguments. The simulator can record a second effect. This
proposal adds no deduplication store or status calls. The final report uses
the same report sink as B0, whose API has no idempotency key.

Use a separate completed-result boundary per call. Putting the whole
LangGraph worker inside one Activity would replay the entire agent on an
Activity retry and would measure LangGraph's checkpoints and retries too.
Deduplicating across worker names or later model steps would test a new
application identity rule. We want to measure the identity of one
durable Activity.

### Retries and crash recovery

Keep Temporal's default Activity retry policy. Backoff starts at 1 s,
doubles, caps at 100 s and allows unlimited attempts. Keep the default of
no Workflow Execution retries. A virtual transport timeout is a retryable
Activity failure. An ordinary simulator refusal returns to the model as
an Activity result. See [Temporal Retry Policy](https://docs.temporal.io/encyclopedia/retry-policies#default-values-for-retry-policy).

Keep the tool timeouts from 0008 on FiveFold's clock. In dev-001 the
dispatch timeout is 2,000 virtual ms. The largest allowed status wait is
60,000 virtual ms, so its call timeout is at most 62,000 virtual ms.
Set `start_to_close_timeout` to 600 wall-clock s as an infrastructure guard.
Temporal recommends it be “longer than the maximum possible time for the
Activity Execution to complete.” Leave Schedule-To-Close at its
documented infinite default and omit Schedule-To-Start and Heartbeat
timeouts, which would add independent host-time limits. Timing out
inside Temporal invalidates the episode for scoring. Use 30 wall-clock s
to report a stalled host handoff as an infrastructure error. See
[Activity timeouts and failure detection](https://docs.temporal.io/encyclopedia/detecting-activity-failures)
and [Python Activity timeouts](https://docs.temporal.io/develop/python/activities/timeouts).

F1 uses the existing B0 fault points. Both variants interrupt the active
order Activity before its request is sent. F11 interrupts it after sending
the request or after receiving the answer, before Temporal records
completion. F3 interrupts it at the latter point. The simulator keeps a
request already on its way. The simulator or transport decides what happens
to the in-flight request and its answer on the same virtual schedule as B0.

On the programmed virtual restart, a harness observer reports the lost
Activity attempt as failed via Temporal's asynchronous Activity completion.
A virtual timeout uses the same API to report the failure when the timeout
occurs. Temporal schedules the retry with the same Activity ID and new
attempt number. The harness releases the retry into the simulator at the
current virtual time. When a Workflow Task replays, Temporal supplies
completed model and tool results from history. An abandoned Activity has
no completion to replay.

The injected crash ends the FiveFold worker's current execution and loses
its local state. The outer Temporal poller stays online in the prototype.
The observer substitutes the virtual crash signal for Temporal's normal
wall-time crash detection. Temporal documents that its server detects a
lost worker by Start-To-Close timeout. Using that timeout to detect F1,
F3 or F11 would let host timing affect the measurement. This proposal
measures recovery after a known worker loss. See
[Activity failure detection](https://docs.temporal.io/encyclopedia/detecting-activity-failures#start-to-close-timeout),
[Asynchronous Activity Completion](https://docs.temporal.io/develop/python/activities/asynchronous-activity)
and [Temporal Tasks](https://docs.temporal.io/tasks).

### Determinism and evidence routing

Amend 0009 to allow Temporal's internal scheduling, retries and
infrastructure timers to use its own clock in B1. A real wait for Temporal
holds FiveFold virtual time. Serialize handoffs to the scheduler in
FiveFold's order and release the handoff before running any virtual wait.
Other FiveFold work can proceed while the model or tool waits on a
virtual event. A retry enters through the same handoff as a first attempt.
Treat an unexpected activity attempt or a timeout in Temporal as an
infrastructure failure before scoring. The prototype uses no Workflow
timers for application behavior and takes no inputs from the Temporal clock.

Keep all fault timing, tool timeouts, approvals, commit deadlines, status
lag, message logs and scoring decisions on FiveFold's single virtual
clock. Keep the existing seeded generators. Temporal history and its
timestamps are diagnostic data outside the evaluator. A Temporal clock
change can affect elapsed wall time and therefore its metrics. Under this
rule it cannot change any scored record or event order. Test that claim
under deliberate host pauses before accepting B1.

The scratch tests with both Temporal time-skipping servers ruled them out
as the run clock. The V1 test server selected by SDK 1.36.1 and the
V2 server in CLI 1.10.1 and Server 1.32.2 each ran three trials with an
Activity held open. A 150 ms host pause advanced V1 by 151 ms and V2 by
153 ms without a request to advance either clock. A requested 250 ms
advance took 251 to 256 ms of wall time in V1 and 848 to 1,003 ms in V2.
One V2 request reached the experiment's 1,000 ms wait guard. All six
trials retained the Activity ID across the retry. Neither API can freeze
the clock during the measured application work.

Register B1 in the evaluator and use the direct-to-simulator route from
records 0004 and 0008. Classify the gateway route by explicit membership
in G1 or G2, so B2 can use the baseline route once registered. B1 reads
the agent-to-simulator message hop and uses the direct report sink. No
property, reason code or excuse changes. The implementation gate runs
the 235 existing expected-outcome tests unchanged and regenerates the
committed study exports byte for byte.

### Test infrastructure

Use the stock Temporal CLI dev server on loopback with in-memory
persistence and a separate task queue per test process. Reuse one server
for a test session and give each episode a fresh workflow ID. For local
and CI tests, pin `temporalio` 1.36.1 separately from the implementation
PR and pin the CLI to 1.10.1, which embeds Server 1.32.2. Provision the
CLI once from a checksum-verified GitHub release artifact. Start it from
that local binary so test execution needs only loopback and the existing
PostgreSQL service. The SDK, CLI and embedded server use MIT licences.
The ASF lists MIT as compatible with inclusion in Apache-licensed
products. Keep their notices with any redistributed copies. The SDK
wheel includes its licence in the `temporalio-1.36.1.dist-info/licenses`
directory. See
[SDK 1.36.1](https://pypi.org/project/temporalio/1.36.1/),
[CLI licence](https://github.com/temporalio/cli/blob/v1.10.1/LICENSE),
[Server licence](https://github.com/temporalio/temporal/blob/v1.32.2/LICENSE),
[ASF third-party licence policy](https://www.apache.org/legal/resolved.html#category-a)
and the [CLI release](https://github.com/temporalio/cli/releases/tag/v1.10.1).

The Linux amd64 CLI archive was 38,357,804 bytes and passed its published
SHA-256 check. Its executable is 125,718,690 bytes. The installed Python
SDK occupied 51 MiB on the prototype host. The local dev server started
in 103 to 108 ms across three launches. A long-lived service or
Temporal Cloud adds credentials, external timing or a CI service
dependency that the local binary avoids.

Checked in installed Python SDK 1.36.1:

| API | Source path |
| --- | --- |
| `@workflow.defn`, `@workflow.run`, `workflow.execute_activity` | `temporalio/workflow/_definition.py`, `temporalio/workflow/_activities.py` |
| `@activity.defn`, `activity.info`, `activity.raise_complete_async` | `temporalio/activity.py` |
| `Client.connect`, `start_workflow`, `get_async_activity_handle` | `temporalio/client/_client.py` |
| `WorkflowHandle.result`, `fetch_history` | `temporalio/client/_workflow.py` |
| `AsyncActivityHandle.complete`, `fail` | `temporalio/client/_activity.py` |
| `Worker`, `max_cached_workflows` | `temporalio/worker/_worker.py` |
| `WorkflowEnvironment.start_local` | `temporalio/testing/_workflow.py` |
| `WorkflowEnvironment.start_time_skipping`, `start_time_skipping_v2`, `sleep`, `fast_forward`, `get_current_time` | `temporalio/testing/_workflow.py`, `temporalio/testing/_timeskipping.py` |

## Prototype measurements and expected outcomes

The scratch branch ran `dev-001-single-po` at seed 7 with the published
B0 fault plans and the stock Temporal dev server. It used the same
evaluator and the same record reader as playback. The code and raw
episode logs stay outside this pull request.

| Host treatment | Runs per cell | Runs | Median elapsed wall time |
| --- | ---: | ---: | ---: |
| Ordinary host | 10 | 720 | 1,706 ms |
| 150 ms async pause in selected Temporal activities | 5 | 360 | 2,103 ms |
| 150 ms blocked Temporal worker thread in those activities | 5 | 360 | 2,146 ms |

Every treatment uses the same seed and fault plan. Pauses were
inserted before dispatch of the first model Activity, the fifth step's
tool Activity, and each retried Activity. The 1,440 episodes ran on
eight independent PostgreSQL 16 test shards sharing one Temporal
server. The matrix took 497.6 wall-clock s. It recorded 720 Temporal
Activity retries: 400 after virtual worker loss and 320 after FiveFold
tool timeouts. No Temporal timeout supplied a result.

All 20 runs per cell matched byte for byte on the ten sets of
canonical records used by playback, with its existing wall-clock
exclusions. SHA-256 matched separately for every set in every cell.
There were zero record, verdict or failing-property differences. The
SHA-256 of the sorted mapping from cell to record digests is
`c56c5a99f5cd7cb06d11ab8e50dc61337b028c68a82774d60f70ef9068016893`.
The timing proof applies to this scenario, fault matrix, runtime version
and host perturbation.

The local timings estimate 105.2 s of episode execution for one serial
sweep of the 72 B1 cells in `make check`, excluding database provisioning
and setup. The estimate sums the average unperturbed time for each cell.
Measure the full added time on the Linux CI runner when the B1 tests land.
Keep all expected-outcome checks in `make check` and retain the
20-run-per-cell comparison as a reproducible acceptance check.

The scratch registration also passed the existing `test_expected.py`
suite: 235 fault cells plus five catalog checks. Both per-model study CSVs
and Markdown reports regenerated byte for byte from committed results.
The implementation gate still covers all committed study exports.

### Proposed B1 cells

There are 34 pass, 34 fail and four pass_unresolved cells. The
table names all applicable fault, variant and contract combinations.
F4 and F12 on `agent_gw` and `gw_sim` do not apply because B1 writes
directly. F10 needs a destination status answer. The scripted direct
policy never asks for one. F8 is outside this sprint and has no built cell
in `faults/expected.yaml`. F1 and F3 use the direct-baseline crash points
as described above.

| Fault | Variant | Contract | Verdict | Failing properties | Reason |
| --- | --- | --- | --- | --- | --- |
| F1 | a | C0 | `pass` | None | The first attempt stopped before sending. The resumed Activity committed once. |
| F1 | a | C1 | `pass` | None | The first attempt stopped before sending. The resumed Activity committed once. |
| F1 | a | C2 | `pass` | None | The first attempt stopped before sending. The resumed Activity committed once. |
| F1 | a | C3 | `pass` | None | The first attempt stopped before sending. The resumed Activity committed once. |
| F1 | b | C0 | `pass` | None | The first attempt stopped before sending. The resumed Activity committed once. |
| F1 | b | C1 | `pass` | None | The first attempt stopped before sending. The resumed Activity committed once. |
| F1 | b | C2 | `pass` | None | The first attempt stopped before sending. The resumed Activity committed once. |
| F1 | b | C3 | `pass` | None | The first attempt stopped before sending. The resumed Activity committed once. |
| F2 | None | C0 | `fail` | 1, 2, 3 | The unkeyed retry committed again on the same approval and stale inputs. |
| F2 | None | C1 | `fail` | 1, 2, 3 | The unkeyed retry committed again on the same approval and stale inputs. |
| F2 | None | C2 | `pass` | None | The Activity retry reused its key and recovered the first committed result. |
| F2 | None | C3 | `pass` | None | The Activity retry reused its key and recovered the first committed result. |
| F3 | None | C0 | `fail` | 1, 2, 3 | The unkeyed retry committed again on the same approval and stale inputs. |
| F3 | None | C1 | `fail` | 1, 2, 3 | The unkeyed retry committed again on the same approval and stale inputs. |
| F3 | None | C2 | `pass` | None | The Activity retry reused its key and recovered the first committed result. |
| F3 | None | C3 | `pass` | None | The Activity retry reused its key and recovered the first committed result. |
| F4 | agent_sim | C0 | `fail` | 1, 2, 3 | The copied request placed a second order on the same approval and stale inputs. |
| F4 | agent_sim | C1 | `fail` | 1, 2, 3 | The copied request placed a second order on the same approval and stale inputs. |
| F4 | agent_sim | C2 | `pass` | None | The simulator received two deliveries with one key and committed once. |
| F4 | agent_sim | C3 | `pass` | None | The simulator received two deliveries with one key and committed once. |
| F5 | beyond_deadline | C0 | `fail` | 1, 2, 3 | The unkeyed retry committed first. The delayed original committed past the commit deadline on the same approval. |
| F5 | beyond_deadline | C1 | `fail` | 1, 2, 3 | The unkeyed retry committed first. The delayed original committed past the commit deadline on the same approval. |
| F5 | beyond_deadline | C2 | `pass_unresolved` | None | The keyed retry made no new effect. The first order committed past the commit deadline. The report remained unknown. |
| F5 | beyond_deadline | C3 | `pass_unresolved` | None | The keyed retry made no new effect. The first order committed past the commit deadline. The report remained unknown. |
| F5 | in_contract | C0 | `fail` | 1, 2, 3 | The unkeyed retry committed first. The delayed original committed inside the commit window on the same approval. |
| F5 | in_contract | C1 | `fail` | 1, 2, 3 | The unkeyed retry committed first. The delayed original committed inside the commit window on the same approval. |
| F5 | in_contract | C2 | `pass_unresolved` | None | The keyed retry made no new effect. The first order committed inside the commit window. The report remained unknown. |
| F5 | in_contract | C3 | `pass_unresolved` | None | The keyed retry made no new effect. The first order committed inside the commit window. The report remained unknown. |
| F6 | early | C0 | `fail` | 2 | The order committed after its approval was revoked. |
| F6 | early | C1 | `fail` | 2 | The order committed after its approval was revoked. |
| F6 | early | C2 | `fail` | 2 | The order committed after its approval was revoked. |
| F6 | early | C3 | `pass` | None | The destination refused the revoked approval. The missing effect was excused. |
| F6 | late | C0 | `fail` | 2 | The order committed after its approval was revoked. |
| F6 | late | C1 | `fail` | 2 | The order committed after its approval was revoked. |
| F6 | late | C2 | `fail` | 2 | The order committed after its approval was revoked. |
| F6 | late | C3 | `pass` | None | The destination refused the revoked approval. The missing effect was excused. |
| F7 | early_harmless | C0 | `pass` | None | The quote change left the relevant inputs fresh. The order committed once. |
| F7 | early_harmless | C1 | `pass` | None | The quote change left the relevant inputs fresh. The order committed once. |
| F7 | early_harmless | C2 | `pass` | None | The quote change left the relevant inputs fresh. The order committed once. |
| F7 | early_harmless | C3 | `pass` | None | The quote change left the relevant inputs fresh. The order committed once. |
| F7 | early_price_up | C0 | `fail` | 3 | The original order committed against the old price. |
| F7 | early_price_up | C1 | `fail` | 3 | The original order committed against the old price. |
| F7 | early_price_up | C2 | `fail` | 3 | The original order committed against the old price. |
| F7 | early_price_up | C3 | `pass` | None | The destination refused the stale quote. A fresh quote and approval led to one commit. |
| F7 | late_harmless | C0 | `pass` | None | The quote change left the relevant inputs fresh. The order committed once. |
| F7 | late_harmless | C1 | `pass` | None | The quote change left the relevant inputs fresh. The order committed once. |
| F7 | late_harmless | C2 | `pass` | None | The quote change left the relevant inputs fresh. The order committed once. |
| F7 | late_harmless | C3 | `pass` | None | The quote change left the relevant inputs fresh. The order committed once. |
| F7 | late_price_up | C0 | `fail` | 3 | The original order committed against the old price. |
| F7 | late_price_up | C1 | `fail` | 3 | The original order committed against the old price. |
| F7 | late_price_up | C2 | `fail` | 3 | The original order committed against the old price. |
| F7 | late_price_up | C3 | `pass` | None | The destination refused the stale quote. A fresh quote and approval led to one commit. |
| F9 | different_args | C0 | `fail` | 1, 3, 5 | Separate workers placed two orders on stale inputs. The counted report named only one effect. |
| F9 | different_args | C1 | `fail` | 1, 3, 5 | Separate workers placed two orders on stale inputs. The counted report named only one effect. |
| F9 | different_args | C2 | `fail` | 1, 3, 5 | Separate workers placed two orders on stale inputs. The counted report named only one effect. |
| F9 | different_args | C3 | `fail` | 5 | One order committed. The counted report said not done although the effect existed. |
| F9 | same_args | C0 | `fail` | 1, 3, 5 | Separate workers placed two orders on stale inputs. The counted report named only one effect. |
| F9 | same_args | C1 | `fail` | 1, 3, 5 | Separate workers placed two orders on stale inputs. The counted report named only one effect. |
| F9 | same_args | C2 | `fail` | 1, 3, 5 | Separate workers placed two orders on stale inputs. The counted report named only one effect. |
| F9 | same_args | C3 | `fail` | 5 | One order committed. The counted report said not done although the effect existed. |
| F11 | after_tool_call | C0 | `fail` | 1, 2, 3 | The unkeyed retry committed again on the same approval and stale inputs. |
| F11 | after_tool_call | C1 | `fail` | 1, 2, 3 | The unkeyed retry committed again on the same approval and stale inputs. |
| F11 | after_tool_call | C2 | `pass` | None | The Activity retry reused its key and recovered the first committed result. |
| F11 | after_tool_call | C3 | `pass` | None | The Activity retry reused its key and recovered the first committed result. |
| F11 | after_tool_result | C0 | `fail` | 1, 2, 3 | The unkeyed retry committed again on the same approval and stale inputs. |
| F11 | after_tool_result | C1 | `fail` | 1, 2, 3 | The unkeyed retry committed again on the same approval and stale inputs. |
| F11 | after_tool_result | C2 | `pass` | None | The Activity retry reused its key and recovered the first committed result. |
| F11 | after_tool_result | C3 | `pass` | None | The Activity retry reused its key and recovered the first committed result. |
| F12 | agent_sim | C0 | `pass` | None | The first request never reached the simulator. The retry committed once. |
| F12 | agent_sim | C1 | `pass` | None | The first request never reached the simulator. The retry committed once. |
| F12 | agent_sim | C2 | `pass` | None | The first request never reached the simulator. The retry committed once. |
| F12 | agent_sim | C3 | `pass` | None | The first request never reached the simulator. The retry committed once. |

## Consequences

B1 compares an agent with persisted Activity results and a per-Activity
write key against the same task under B0, G1 and G2. It can still duplicate
effects where the destination offers no key or where separate Activities
act on the same intent. It uses no gateway authorization, state or report
checks.

Spec changes on acceptance: add the B1 execution and identity rules to
Configurations and record 0008, and the narrow clock exception to
Simulator and record 0009. Add the expected outcomes, baseline routing
and digest gate to Testing. Add B1 to the Run configuration list. Apply
the docs changes as the implementation and its tests land.
