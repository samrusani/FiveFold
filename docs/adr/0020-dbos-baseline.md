# 0020: B2, the DBOS baseline

Status: Proposed
Date: 2026-11-22
Spec sections affected: Configurations, Trust boundaries, Runs and determinism, Testing, Data model (Run)
Proposed amendments: 0008 (B2 configuration) and 0009 (DBOS internal timing)

## Context

Record 0008 defines B2 as a DBOS agent that writes directly. B2 uses
dev-001, the same scripted model, task, intents, authority, budgets and
fault plans as B0 and B1. This compares execution and recovery on one
agent policy. The evidence is still the simulator's and harness's
records (0004).

## Decision

### Workflow and steps

Use one DBOS Workflow per worker. It chooses the next step, keeps the
conversation and checks the same step and token budgets. Use separate
DBOS Steps for each model call, tool call and final report. At a Step
boundary, the DBOS worker sends a request to the FiveFold scheduler. The
scheduler runs the action or model call and sends back the result. The
Step returns that result to DBOS, which checkpoints it. On recovery DBOS
returns recorded outputs from already completed Steps.

DBOS says to put a function in a step if it "performs a nondeterministic
operation." Its architecture says "Steps should be idempotent." See
[Steps](https://docs.dbos.dev/python/tutorials/step-tutorial),
[Workflows](https://docs.dbos.dev/python/tutorials/workflow-tutorial) and
[Architecture](https://docs.dbos.dev/architecture).

A single Step for the whole agent would repeat completed work after a
crash. A database transaction shared with the simulator could hide a
lost reply by committing the effect with the checkpoint. Use separate
Steps and preserve the destination boundary in 0008.

### Idempotency

At C2 and C3 the destination write key is SHA-256 of canonical JSON
containing the logical FiveFold run ID, the worker name and the string
form of `DBOS.step_id`. The same Step has the same ID under a local retry
or Workflow recovery. B1 uses its logical Activity ID in the same key
recipe. Later Steps and other workers use separate keys. Execution IDs
from the process launcher and Step attempt numbers stay out of the key.

At C0 and C1 the destination has no key support. A retry sends the same
business arguments and can place another effect. The report sink keeps
its existing API. A key for the whole intent would join independent
Steps or workers and would add a guarantee outside a DBOS Step.

### Retries and timeouts

DBOS disables Step retries by default. Enable them for transient
FiveFold tool timeouts, as DBOS recommends configured Step retries for
transient failures. Use the default three attempts total, a 1 s initial
interval and a 2.0 multiplier. The two waits are 1 s and 2 s. Use a
`should_retry` predicate for the FiveFold timeout. Ordinary simulator
refusals return to the model as Step results. A worker that exhausts the
Step's retries ends with an error, and the usual evidence still applies
at run end. See the [Step retry documentation](https://docs.dbos.dev/python/tutorials/step-tutorial#configurable-retries).

Application tool timeouts use the existing virtual clock. Leave DBOS
Workflow and Step timeouts unset. Keep a 30 s host handoff limit for a
failed prototype or unavailable DBOS database. If it expires, stop the
episode before scoring. Matching B1's unlimited Activity retries would
hide a difference in the frameworks' retry behavior.

### Process crashes and recovery

Use the existing direct-route F1, F3 and F11 points. F1 kills the
virtual worker before it sends the order, F3 kills it when the answer
arrives, and F11 kills it after the tool call or after the tool result.
The DBOS child stays blocked at its Step handoff. At the programmed
virtual restart the harness sends SIGKILL, reaps the child and starts
its replacement with the same DBOS executor ID and application version.
`DBOS.launch` finds its pending Workflow and queues it for recovery. The
replacement reads the result through `DBOS.retrieve_workflow`. The
uncheckpointed Step runs again with the original Step ID. Use the SDK's
default limit of 100 Workflow recovery attempts. Each crash cell
in the prototype used one process restart. The simulator
keeps requests it already received. DBOS replays saved results for earlier Steps from the system database.

A process already dead from an actual crash has the same pending
Workflow to recover when relaunched. The measured experiment supplies
the known-loss signal at the virtual restart. It measures recovery
after that signal and excludes detection time. See [Workflow recovery](https://docs.dbos.dev/production/workflow-recovery)
and [DBOS Python 3.6.0 release notes](https://github.com/dbos-inc/dbos-transact-py/releases/tag/3.6.0).

Turning the kill into an ordinary Step exception would use the retry
budget for a process crash and would leave the old process alive.
A Conductor would make detection depend on a separate service and host
time. Use local startup recovery in the prototype.

### Determinism

DBOS's Step code reads host time for its checkpoints. Its retry helper
waits in wall time, and recovery uses a queue with a jittered poll
interval. PostgreSQL records timestamps in the DBOS system database.
The public configuration offers polling intervals and timeouts, with no
supported FiveFold clock injection. Changing retry intervals still
leaves recovery and storage timestamps on host time.

Propose a narrow amendment to 0009, like the internal Temporal timer
exception in 0018. Hold FiveFold time while waiting for DBOS and
serialize handoffs before running virtual work. Fault timing, tool
timeouts, approvals, commit deadlines, status lag, replies and scored
records still use FiveFold's virtual clock. DBOS controls how long a
handoff takes in wall time. Only the FiveFold scheduler sends model,
tool and report results. Scoring uses FiveFold records.

### Configuration registration

Propose the following implementation changes after acceptance.

1. Register `B2` in `evaluator.records.Config` and in the in-process
   configuration list and `Config` literal in `harness.run`. Add the name
   to the config literals for `harness.log.RunRecord` and
   `recorder.cassette.RunHeader`, which describe stored run metadata.
2. Add SQL migrations that extend the config checks on `harness.run`
   and `registry.run` to admit `B2`. Keep the existing migration files
   and their checksums.
3. Admit `B2` in the configuration list in `faults.plan`. Use the
   existing G1/G2 check to select gateway fault points. In
   `harness.cells.cell_plan`, give both B1 and B2 the existing B0
   direct path. Add the 72 approved B2 expected-outcome entries and
   checks for those cells.
4. Dispatch B2 through the DBOS worker in the in-process harness.
   Offer it as an explicit scripted study configuration, as with B1.
   Preserve the current default study list and the current demo,
   recorder and split command selections.

Keep `has_gateway` true only for G1 and G2. B2 takes the existing direct
evidence route, including the simulator ledger, agent-to-simulator
messages and direct report sink. Reuse the five property definitions,
scoring, reason codes, excuses and fault catalog.

### Infrastructure and trust boundaries

Run the open-source SDK with a PostgreSQL 16 system database owned by
`ff_dbos`. Keep it separate from the simulation databases. The prototype
used a dedicated PostgreSQL service and one state database per test
process, with a DBOS schema per worker. The DBOS role can create tables in
its own database. It has no simulator role or connection string. The
child receives its own database URL and exchanges application messages
with the harness on local pipes. Simulator effects continue to commit
in the simulator's own transactions.

The tests install the SDK and its package dependencies and need
loopback access to PostgreSQL. An external DBOS service would introduce
another recovery path and another network dependency. SQLite would
change the persistence mechanism used in this comparison. Use the
existing PostgreSQL engine for both sides, with separate state databases
and roles, and keep the boundary in 0008.

The private prototype used DBOS Python 3.6.0. Its lock added these five
packages. No existing version changed.

| Package | Version | Licence checked in the installed wheel |
| --- | --- | --- |
| `dbos` | 3.6.0 | MIT |
| `croniter` | 7.0.0 | MIT |
| `greenlet` | 3.5.6 | MIT and PSF-2.0 |
| `python-dateutil` | 4.0.0 | BSD-3-Clause |
| `sqlalchemy` | 2.1.9 | MIT |

See the [DBOS licence at 3.6.0](https://github.com/dbos-inc/dbos-transact-py/blob/3.6.0/LICENSE),
the [DBOS Python package](https://pypi.org/project/dbos/3.6.0/) and
the [ASF third-party licence guidance](https://www.apache.org/legal/resolved.html#category-a).

Checked in installed DBOS Python 3.6.0:

| API or behavior | Source path |
| --- | --- |
| `DBOSConfig`, system database URL, system schema, pool | `dbos/_dbos_config.py` |
| `DBOS(config=...)`, `launch`, `destroy`, `workflow`, `step` | `dbos/_dbos.py` |
| `DBOS.step_id`, `step_status`, `StepStatus.current_attempt` | `dbos/_dbos.py`, `dbos/_context.py` |
| `SetWorkflowID` | `dbos/_context.py` |
| `retrieve_workflow`, `WorkflowHandle.get_result` | `dbos/_dbos.py` |
| Local startup recovery and re-enqueue | `dbos/_dbos.py`, `dbos/_recovery.py` |
| Wall-clock Step checkpoints, retry delays, workflow results | `dbos/_core.py`, `dbos/_sys_db.py` |
| Jittered polling and queue claim | `dbos/_queue.py`, `dbos/_sys_db.py` |

## Prototype measurements and expected outcomes

Run the same 72 applicable cells as B0 and B1. The direct policy
does not issue the status request that F10 needs. The prototype ran
each cell 20 times. The worker delay treatment inserted 150 ms waits
at selected Step handoffs. The process-pause treatment sent SIGSTOP
and then SIGCONT after a 150 ms wait. Both treatments included
handoffs during retries and recovery.

| Treatment | Runs per cell | Total runs | Different record sets within a cell |
| --- | ---: | ---: | ---: |
| Ordinary | 10 | 720 | 0 |
| 150 ms worker delays | 5 | 360 | 0 |
| 150 ms child process pauses | 5 | 360 | 0 |
| Total | 20 | 1,440 | 0 |

For each trial, read the ten datasets through
`fivefold.recorder.playback.records`. It returns ordered rows as
canonical JSON and excludes wall-clock fields as in 0009. Join each
dataset's rows with newlines and compute SHA-256. All ten digests
matched the first trial in their cell. For the map from cell IDs to
their ten digests, SHA-256 of the compact, key-sorted JSON is
`70451fa7da6388722c588e031dae5c26a67ec175515730a00e1139b59bea7810`.
A separate one-trial sweep reproduced the same map. Pytest reported
128.13 s for its 72 cases, including local database setup and teardown.
That gives a local estimate of the time added to `make check` by running
the 72 expected-outcome tests once. The 20-trial study was a separate
determinism check. Hosted CI time has yet to be measured.

A separate native DBOS test saw three attempts at the same Step ID,
with waits of 1,005 ms and 2,007 ms. The DBOS database's PostgreSQL
clock advanced by 156 ms during a 150 ms host wait. In the matrix,
DBOS retried 320 Steps and recovered 400 Workflows. Among ordinary
trials, the first retry wait had a median of 1,006 ms (160 samples).
Process recovery from the known loss through the next Step handoff
had a median of 1,343.5 ms (200 samples, range 1,237 to 1,545 ms).
These are local wall-clock observations.

The proposed matrix has 34 `pass`, 34 `fail` and four
`pass_unresolved` cells. Each row uses the existing evaluator and
describes what happened in the prototype.

| Fault | Variant | Contract | Verdict | Failing properties | Reason |
| --- | --- | --- | --- | --- | --- |
| F1 | a | C0 | `pass` | None | The first process stopped before sending. The recovered Step placed one order. |
| F1 | a | C1 | `pass` | None | The first process stopped before sending. The recovered Step placed one order. |
| F1 | a | C2 | `pass` | None | The first process stopped before sending. The recovered Step placed one order. |
| F1 | a | C3 | `pass` | None | The first process stopped before sending. The recovered Step placed one order. |
| F1 | b | C0 | `pass` | None | The first process stopped before sending. The recovered Step placed one order. |
| F1 | b | C1 | `pass` | None | The first process stopped before sending. The recovered Step placed one order. |
| F1 | b | C2 | `pass` | None | The first process stopped before sending. The recovered Step placed one order. |
| F1 | b | C3 | `pass` | None | The first process stopped before sending. The recovered Step placed one order. |
| F2 | None | C0 | `fail` | 1, 2, 3 | The lost reply caused an unkeyed Step retry. Both writes committed on the same approval. |
| F2 | None | C1 | `fail` | 1, 2, 3 | The lost reply caused an unkeyed Step retry. Both writes committed on the same approval. |
| F2 | None | C2 | `pass` | None | The missing reply caused a Step retry. Its original key retrieved the committed result. |
| F2 | None | C3 | `pass` | None | The missing reply caused a Step retry. Its original key retrieved the committed result. |
| F3 | None | C0 | `fail` | 1, 2, 3 | The recovered write Step sent an unkeyed request again. Both effects cited the first approval. |
| F3 | None | C1 | `fail` | 1, 2, 3 | The recovered write Step sent an unkeyed request again. Both effects cited the first approval. |
| F3 | None | C2 | `pass` | None | The recovered write Step reused its original key and retrieved the committed result. |
| F3 | None | C3 | `pass` | None | The recovered write Step reused its original key and retrieved the committed result. |
| F4 | agent_sim | C0 | `fail` | 1, 2, 3 | The original and copied deliveries both committed on the same approval and stale inputs. |
| F4 | agent_sim | C1 | `fail` | 1, 2, 3 | The original and copied deliveries both committed on the same approval and stale inputs. |
| F4 | agent_sim | C2 | `pass` | None | The copied request carried the same Step key. The destination committed once. |
| F4 | agent_sim | C3 | `pass` | None | The copied request carried the same Step key. The destination committed once. |
| F5 | beyond_deadline | C0 | `fail` | 1, 2, 3 | The unkeyed retry committed first. The delayed first request committed past the deadline on the same approval. |
| F5 | beyond_deadline | C1 | `fail` | 1, 2, 3 | The unkeyed retry committed first. The delayed first request committed past the deadline on the same approval. |
| F5 | beyond_deadline | C2 | `pass_unresolved` | None | The keyed retry saw an in-progress write. The order committed past the deadline and the report stayed unknown. |
| F5 | beyond_deadline | C3 | `pass_unresolved` | None | The keyed retry saw an in-progress write. The order committed past the deadline and the report stayed unknown. |
| F5 | in_contract | C0 | `fail` | 1, 2, 3 | The unkeyed retry committed first. The delayed first request committed within the commit window on the same approval. |
| F5 | in_contract | C1 | `fail` | 1, 2, 3 | The unkeyed retry committed first. The delayed first request committed within the commit window on the same approval. |
| F5 | in_contract | C2 | `pass_unresolved` | None | The keyed retry saw an in-progress write. The order committed within the commit window and the report stayed unknown. |
| F5 | in_contract | C3 | `pass_unresolved` | None | The keyed retry saw an in-progress write. The order committed within the commit window and the report stayed unknown. |
| F6 | early | C0 | `fail` | 2 | The order committed after the approval had been revoked. |
| F6 | early | C1 | `fail` | 2 | The order committed after the approval had been revoked. |
| F6 | early | C2 | `fail` | 2 | The order committed after the approval had been revoked. |
| F6 | early | C3 | `pass` | None | The destination rejected the revoked approval. The run passed with the missing effect excused. |
| F6 | late | C0 | `fail` | 2 | The order committed after the approval had been revoked. |
| F6 | late | C1 | `fail` | 2 | The order committed after the approval had been revoked. |
| F6 | late | C2 | `fail` | 2 | The order committed after the approval had been revoked. |
| F6 | late | C3 | `pass` | None | The destination rejected the revoked approval. The run passed with the missing effect excused. |
| F7 | early_harmless | C0 | `pass` | None | The quote change preserved the values the order depended on. One order committed. |
| F7 | early_harmless | C1 | `pass` | None | The quote change preserved the values the order depended on. One order committed. |
| F7 | early_harmless | C2 | `pass` | None | The quote change preserved the values the order depended on. One order committed. |
| F7 | early_harmless | C3 | `pass` | None | The quote change preserved the values the order depended on. One order committed. |
| F7 | early_price_up | C0 | `fail` | 3 | The order committed using the earlier price. |
| F7 | early_price_up | C1 | `fail` | 3 | The order committed using the earlier price. |
| F7 | early_price_up | C2 | `fail` | 3 | The order committed using the earlier price. |
| F7 | early_price_up | C3 | `pass` | None | The destination rejected the stale quote. The agent refreshed the quote and approval, then committed once. |
| F7 | late_harmless | C0 | `pass` | None | The quote change preserved the values the order depended on. One order committed. |
| F7 | late_harmless | C1 | `pass` | None | The quote change preserved the values the order depended on. One order committed. |
| F7 | late_harmless | C2 | `pass` | None | The quote change preserved the values the order depended on. One order committed. |
| F7 | late_harmless | C3 | `pass` | None | The quote change preserved the values the order depended on. One order committed. |
| F7 | late_price_up | C0 | `fail` | 3 | The order committed using the earlier price. |
| F7 | late_price_up | C1 | `fail` | 3 | The order committed using the earlier price. |
| F7 | late_price_up | C2 | `fail` | 3 | The order committed using the earlier price. |
| F7 | late_price_up | C3 | `pass` | None | The destination rejected the stale quote. The agent refreshed the quote and approval, then committed once. |
| F9 | different_args | C0 | `fail` | 1, 3, 5 | Two independent workers committed two orders on stale inputs. The counted report named one effect. |
| F9 | different_args | C1 | `fail` | 1, 3, 5 | Two independent workers committed two orders on stale inputs. The counted report named one effect. |
| F9 | different_args | C2 | `fail` | 1, 3, 5 | Two independent workers committed two orders on stale inputs. The counted report named one effect. |
| F9 | different_args | C3 | `fail` | 5 | One order committed. The counted report said the order was not done. |
| F9 | same_args | C0 | `fail` | 1, 3, 5 | Two independent workers committed two orders on stale inputs. The counted report named one effect. |
| F9 | same_args | C1 | `fail` | 1, 3, 5 | Two independent workers committed two orders on stale inputs. The counted report named one effect. |
| F9 | same_args | C2 | `fail` | 1, 3, 5 | Two independent workers committed two orders on stale inputs. The counted report named one effect. |
| F9 | same_args | C3 | `fail` | 5 | One order committed. The counted report said the order was not done. |
| F11 | after_tool_call | C0 | `fail` | 1, 2, 3 | The recovered write Step sent an unkeyed request again. Both effects cited the first approval. |
| F11 | after_tool_call | C1 | `fail` | 1, 2, 3 | The recovered write Step sent an unkeyed request again. Both effects cited the first approval. |
| F11 | after_tool_call | C2 | `pass` | None | The recovered write Step reused its original key and retrieved the committed result. |
| F11 | after_tool_call | C3 | `pass` | None | The recovered write Step reused its original key and retrieved the committed result. |
| F11 | after_tool_result | C0 | `fail` | 1, 2, 3 | The recovered write Step sent an unkeyed request again. Both effects cited the first approval. |
| F11 | after_tool_result | C1 | `fail` | 1, 2, 3 | The recovered write Step sent an unkeyed request again. Both effects cited the first approval. |
| F11 | after_tool_result | C2 | `pass` | None | The recovered write Step reused its original key and retrieved the committed result. |
| F11 | after_tool_result | C3 | `pass` | None | The recovered write Step reused its original key and retrieved the committed result. |
| F12 | agent_sim | C0 | `pass` | None | The first request was dropped before delivery. The next attempt placed one order. |
| F12 | agent_sim | C1 | `pass` | None | The first request was dropped before delivery. The next attempt placed one order. |
| F12 | agent_sim | C2 | `pass` | None | The first request was dropped before delivery. The next attempt placed one order. |
| F12 | agent_sim | C3 | `pass` | None | The first request was dropped before delivery. The next attempt placed one order. |

### Differences from B0 and B1

B2 matches B1's verdict and failing properties in all 72 cells.
Thirteen cells differ from B0.

| Cells | Count | B0 | B2 |
| --- | ---: | --- | --- |
| F2, F3, F4 `agent_sim` and both F11 variants at C2 | 5 | Fail properties 1, 2 and 3 | Pass. The repeated request uses the original key and only one effect commits. |
| F2, F3 and both F11 variants at C3 | 4 | Fail property 5. The repeat is refused and the report misses the first effect. | Pass. The repeated request returns the committed result. |
| Both F5 variants at C2 | 2 | Fail properties 1, 2 and 3. Both writes commit. | Pass unresolved. The key limits the operation to one effect, which the report marks unknown. |
| Both F5 variants at C3 | 2 | Pass | Pass unresolved |

At C3 in both F5 variants, B0's unkeyed retry commits at 7,297 virtual
ms and the agent reports it as done. The destination rejects B0's
delayed original after the approval was used and the budget version
changed. B2's keyed retry sees `in_progress` and the agent reports
unknown. The original request commits once during the drain, at
10,311 virtual ms in contract or 25,311 virtual ms beyond the deadline.
Both configurations pass all five properties. The unknown report gives
B2 its `pass_unresolved` verdict, as in B1.


## Consequences

B2 adds a durable process-recovery baseline with a key for one Step.
Separate Steps or workers can still place separate writes for the same
intent. Where the destination has no key support, an unkeyed retry can
repeat an effect. The experiment starts recovery from a known loss at
the virtual restart. Its recovery latency is a lower bound because
it excludes crash detection. Record this limit in
`docs/limitations.md` with the B2 implementation.

## Spec changes on acceptance

- Add B2's direct path and per-Step identity to Configurations and the
  run configuration list in Data model.
- Describe the DBOS system database and role under Trust boundaries.
- Add the internal DBOS clock exception and the fixed handoff order
  under Runs and determinism.
- Add the B2 cells and playback digest checks under Testing. Retain the
  existing expected-outcome checks for B0, B1, G1 and G2.
