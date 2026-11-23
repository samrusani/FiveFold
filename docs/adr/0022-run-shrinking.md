# 0022: Shrink a failing scripted run

Status: Proposed
Date: 2026-11-23
Spec sections affected on acceptance: Playback and branching, Testing, Later releases

## Context

The spec calls for shrinking a failing run to the fewest events that still
reproduce it. The fault plan already names the events a user controls.
The simulator, gateway, agent and evaluator produce the rest from that plan
(0006, 0009). Shrinking should leave a small runnable plan and a record of
how it was found.

## Decision

### Reproducing a failure

For the first implementation, require the same `fail` verdict, the same
failing property numbers, and the same set of `(property, reason code,
intent ID)` triples from the failures. Keep the missing intent as its own
value. Property numbers alone could accept a different failure of the
same check. Effect IDs, timestamps, log sequence numbers, counts and text
can change when earlier events are removed. Keep those details in the
reports so a reader can compare them.

An infrastructure error, a run stopped before scoring, `pass` and
`pass_unresolved` do not reproduce a scored failure. An I1 to I7 violation
is a separate kind of failure. The gateway raises `InvariantViolation`
with `Violation(invariant, subject, message)` records. Matching only an
invariant number is too broad. Subjects can name transient action and event
IDs. Defer invariant shrinking until a decision defines a stable witness
and records where the check failed. If a candidate fails before scoring,
stop and report its error.

### What can change

Take the ordered, fully materialized plan in `harness.run.params.plan`
(0006, 0009). Remove plan entries, lower a trigger's occurrence, and lower
numeric parameters that have valid smaller values. Leave the fault ID,
variant, point and match fields fixed. Preserve the order of entries.
Use the catalog and the existing configuration and run validators so a
reduction stays within the fault variant's timing bounds. For F5 beyond
the deadline, for example, `delay_ms` must stay above D.

Hold the scenario digest, configuration, contract, seed, execution
topology, initial worker count and scripted policy fixed. A worker the
plan starts through F9 can disappear if its F9 entry is removed and the
failure still reproduces. Keep an explicitly configured second worker.
Reducing the actual message or ledger rows would give a different history
without a way to run that history from a valid plan.

### Search

Use delta debugging on contiguous chunks of plan entries. Try deletions
in input order, then increase the granularity down to single entries.
Reduce occurrences and numeric parameters in entry order. Try the legal
minimum, then a fixed sequence of smaller values toward the current one.
After a successful change, recheck deletions because a smaller parameter
can make another fault redundant. Accept a step only after a fresh run
matches the original signature. Cache the result for an identical plan
within the search.

Compare plans by entry count first, then by reductions in occurrence and
parameter values. Hold the trial order fixed when values tie. After the
single-entry checks complete, the report can say that no single deletion
preserves the failure at those parameter values. A failed candidate gives
no guarantee about other combinations. The report should record the best
plan found and which checks completed.

### Supported inputs and output

Start with in-process, scripted B0, G1 and G2 runs, including scripted
recordings and runs from studies or random-plan tests that save the input
and result. Use the materialized plan, including drawn parameters. A seed
and the current fault list alone cannot show that the same plan was used.
The successful shrink is a new scripted run. Its requests can change, so
it cannot use a cassette for the original requests. Refuse replayed,
branched and live runs with a message that explains the cassette mismatch
and points to ordinary playback. A separate, freshly scored scripted
reproduction can be shrunk. B1, B2 and the split topology wait for
separate verification of repeated candidates and their cleanup.

Proposed interface `python -m fivefold.shrink --result FILE --scenario FILE
--out DIR`. Accept a registered run database through `--database NAME` in
place of `--result`. Require the original scenario with its matching digest,
the recorded model and run parameters, and the complete scored result.
Read a registered original as `ff_viewer`. If the evidence or materialized
plan is missing, refuse before creating any database. Rerun the original
plan and compare its signature with the source before the search starts.

Write `faults.yaml` as the list the fault plan loader accepts, `result.json`
as the ordinary evaluator result and `shrink.json` with the source and
final plan digests, signatures, the sequence of candidate plans and their
outcomes, counts and budget status. Require a new output directory. Preserve
the final run's standard evidence with the record projection used by
playback. An explicit keep option can leave the final database and registry
entry for the viewer. Keep provenance in the report, with the original and
new database or artifact identifiers. Leave the original run untouched.

### Cost and cleanup

Start with a cap of 100 distinct search candidates and a 120 s wall-time
budget. Include setup and cleanup in wall time and stop starting candidates
when the budget is exceeded. Finish and clean up any candidate in progress.
Reserve one final rerun to check the selected plan and save its evidence.
On exhaustion, return the best verified plan with `budget_exhausted` and
which work remains. Label elapsed time as a metric. A host pause can change
where a wall-time-limited search stops. The plan, deterministic audit fields
and record digests must agree when repeating the same completed search or
stopping at the same candidate count (0009).

Create each candidate as a fresh copy of the migrated template. Use a
unique database name, refuse taken names and do not request replacement.
Clean up in `finally`, using the database's OID and the same lock as the
registry update. Preserve only the final run if requested. A database
whose OID no longer matches stays untouched. Report cleanup errors and
stop if a trial cannot be scored. Do not count that trial as reproduction.

## Alternatives considered

- Match the verdict alone, or the verdict plus failing properties. Both
  can replace a failure with another reason or move it to a different intent.
- Compare whole records or require the same request or effect IDs. These
  values are artifacts of the steps that the search is trying to remove.
- Enumerate plans by length. This can prove minimality in a finite search
  space but grows quickly with plan length and parameter choices. Keep it
  as an option for a small enough candidate space.
- Use Hypothesis's shrinking directly. The prototype compared its `find`
  over the same small set of ordered subsequences and discrete parameter
  choices. It shrank those cases, but relies on a strategy's encoding and
  exploration order. Delta debugging gives a candidate order a reader can
  reconstruct from the input, and does not enumerate all plans up front.
- Shrink the seed or record live responses again. Either changes things
  beyond the fault plan. A fresh live response can vary with the same request.

## Prototype measurements

The private prototype used `dev-001-single-po`, B0, C2 and seed 7.
Both faults fired in the two-entry cases. Each original ran twice.
Each algorithm searched twice and reran the winner in a fresh database.
Both searches considered ordered subsequences. The F5 case offered the
same three delay values to both algorithms, including the original and
the minimum D + 1 ms. The prototype did not change occurrences.

| Input | Shrunk plan | Delta debugging, fresh candidates / calls / time | Hypothesis, fresh candidates / calls / time |
| --- | --- | ---: | ---: |
| F2 and harmless F7 | F2 | 3 / 3 / 1.825–2.129 s | 3 / 5 / 2.655–2.989 s |
| F2 | F2 | 1 / 1 / 0.854–1.173 s | 1 / 4 / 0.971–1.268 s |
| F5 beyond deadline and harmless F7 | F5, reduced from 20,000 to 10,001 ms | 4 / 4 / 3.732–4.302 s | 3 / 5 / 4.589–5.037 s |

Search time includes creating, executing, reading and dropping candidate
databases on a local PostgreSQL 16 instance. Calls include cache hits.
The table excludes the original runs and final reruns. There were 48
fresh runs in all, including 6 originals and 12 winner reruns. Repeat
searches matched on the chosen plans and counts. Repeat runs matched on
the evaluation with wall-clock fields ignored and on SHA-256 digests of
the canonical playback records. The B0 cases are too small to establish
a performance advantage for either algorithm or to estimate costs in
other configurations.

The prototype used `create_run_database` in
`src/fivefold/harness/migrations.py`, `drop_own_database` in
`src/fivefold/harness/study.py`, `InprocRun.execute` and `run_plan` in
`src/fivefold/harness/run.py`, and `records` in
`src/fivefold/recorder/playback.py`. `comparable_json` in
`src/fivefold/evaluator/result.py` removes wall-clock fields from the
evaluation. `find` is documented by its installed Hypothesis 6.170.15
source at `hypothesis/core.py`. The prototype used `sampled_from` in
`hypothesis/strategies/_internal/core.py` and checked `derandomize`,
`database`, `max_examples` and `deadline` in `hypothesis/_settings.py`.
Hypothesis is already a development dependency.

## Consequences

- A plan with an irrelevant fault that fired shrinks to the single
  fault. A minimal single-fault plan stays unchanged.
- Tests check that differences in reason codes or affected intents
  prevent reproduction. They check occurrence and parameter reductions
  against the original catalog and verify that every accepted plan reruns.
- The same input and candidate cap produce the same chosen plan and
  candidate audit fields. Repeat tests compare the selected run's records
  after removing the ordinary wall-clock fields.
- Tests cover a taken name, cleanup after a failed candidate, no writes
  to the original run, and a budget stop with a verified candidate.
- The search may retain a fault whose removal only works in combination
  with other changes. It reports the reductions tried.

## Spec changes on acceptance

- Define the scored-failure signature, supported inputs and search bounds
  under Playback and branching. Describe the smallest verified plan found
  within the search budget.
- Add replay, determinism, ownership and budget checks under Testing.
  Keep implementation scheduled for a later v0.2 release.
