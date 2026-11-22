# 0021: A TLA+ model of the gateway

Status: Proposed
Date: 2026-11-22
Spec sections affected on acceptance: Action state machine, Testing, Stack, Later releases

## Context

The spec calls for a formal model checked with TLC. Record 0007 reserves
`tla/` for it. The PostgreSQL tests exercise real transactions, and a
model can enumerate bounded orderings of dispatch, delivery, recovery
and reporting. This record proposes how to check the existing design.
Its implementation follows acceptance in a later sprint.

## Decision proposed

### Scope and environment

Model G2's action identity and admission (0001), grant and approval
lifecycle (0002 and 0017), and action transitions (0003 and 0010) under
C0 to C3 (0005). Use `src/fivefold/gateway/state_machine.py` for legal
transitions, reasons and recheck rules. Model durable state and its event
journal, and keep destination effects separate from what the gateway
knows. A network action can occur between recording an attempt and
recording its answer. C0 and C1 can commit duplicates. C2 replays use
the same key. C3 also checks authorization and input conditions at
commit.

Treat business rules as abstract rejection or commit and treat the
authority as an input that can approve, deny, revoke or expire. Model
the final approval poll as the point where the gateway observes a
decision or no decision. Keep wall time, procurement data, agent
strategy, the transport implementation and baseline execution outside
the model. G1 does not enforce G2's action identity or admission rule.
A G1 profile can be added later with its own checks.

| Fault (0006) | Environment step |
| --- | --- |
| F1a and F1b | Crash before the first durable attempt or after it is recorded, with no send required. |
| F2 and F3 | Commit and drop the reply, or crash after an answer is ready but before the gateway records it. |
| F4 | Deliver a second copy at the destination. Represent a repeat agent proposal at admission without giving it a second live action for the same intent. |
| F5 | Let a delivered request commit after the timeout. A bounded settle step represents the stated deadline. The model also permits the destination to commit after that step. |
| F6 and F7 | Revoke authority or change a basis before the check or between the check and commit. An exact or sufficient dependency can go stale. |
| F9 | Offer another action for the same intent. The larger prototype profile exercises the admission guard with two ordered slots. |
| F10 | At C1, give no usable status answer, expose a commit, or present a settled-empty view even when the destination has committed. This abstracts the status lag and its violation. |
| F12 | Drop the outbound request before delivery. |
| F8 and F11 | Outside the model because memory changes and agent checkpoint recovery belong to the agent. |

The scratch prototype abstracts one intent, a single progressing action
and one outstanding commit per action. It represents the gateway-to-
destination transport. Following acceptance, model both argument forms
at admission and let a second admitted action proceed to dispatch
after the first releases the intent. Preserve the real catalog in
the integration tests, which exercise exact points, delays and
combinations that the abstraction omits.

### Invariants and progress

The left column is the spec's contract. The right column describes the
corresponding prototype check. Each quantifier over actions applies to
every initialized slot. Journal entries include the transition ID,
source and target, reason, grant delta and evidence at that step.

| Spec invariant | TLA+ check |
| --- | --- |
| **I1.** An action records at most one effect id, set once, and it never changes. | `I1` restricts a recorded effect to unset or the action's one effect ID. In the journal only T8 and T11 can set it. Once set, every later entry retains it. The primitive destination ledger can still contain two effects under C0 and C1. |
| **I2.** An action has at most one grant, every attempt of the action uses it, and a grant is consumed at most once and never moves. | `I2` gives each action slot one grant ID. Every attempt uses that ID. Every journal grant delta is legal and at most one entry changes the grant from active to consumed. |
| **I3.** Before every send, the gateway checks the grant and the basis. A first send checks every dependency rule. A C2 retry checks only the exact rules. C3 retries are exempt, because the destination checks atomically. | `I3` checks each attempt's saved approval and basis against `Needed(n)` and `Passes`. Attempt 1 uses the full check and T6; later attempts use T13 and the contract-specific check. A send requires the durable attempt's outbox. |
| **I4.** Retries happen only at C2 and C3, and every attempt of an action carries the same idempotency key. At C0 and C1, an action has at most one attempt. | `I4` requires at most one attempt at C0 and C1. At C2 and C3, all attempts carry the action slot's stable key. |
| **I5.** Every agent-facing result reports the action's state when it is produced, never reports OutcomeUnknown as confirmed, failed or rejected, and the gateway accepts no final report that contradicts its records. | `I5` compares the response's state to the journal entry it observed. An accepted `done` cites the known confirmed evidence; `not_done` requires an undispatched or negative state; `unknown` requires Dispatched or OutcomeUnknown. Report acceptance and run closure are one step. |
| **I6.** Every state change writes exactly one `action_event` row in the same transaction, holding a row lock on the action. `action_event` is append-only. | `I6` chains each journal edge to the preceding state and checks it against the generated transition table. A transaction updates the action and appends one event together. The temporal safety predicate `HistoryPreserved` checks that every old journal is a prefix of the next. PostgreSQL tests still verify actual row locking and atomicity. |
| **I7.** At most one action per intent is live or Confirmed. G1's actions are exempt. | `I7` requires `Cardinality({i \in Ids : actions[i].state \in Holding}) <= 1` for the modeled G2 intent. The larger profile can offer a second slot. |

Enable TLC's deadlock check. An explicitly halted run may stutter once
all of its actions are terminal or escalated. An unresolved effect can
require a human decision, and an unreachable authority can keep the
approval poll pending (0017). Neither requires an unconditional
eventual outcome. Start with safety and deadlock checks. Consider a
separate liveness profile requiring eventual terminal state or
escalation after admission and closure. It would give enabled
gateway work, timers and restart weak fairness and assume bounded
crashes and eventual authority and destination progress. It would
require no eventual human attestation.

### Drift checks

Generate the legal action and grant edges and the full, exact or exempt
send-check table from `TRANSITIONS`, `check_transition`,
`GRANT_TRANSITIONS` and `required_check`. Regenerate before the check,
and compare against any version of the tables kept for reading. Run
a small set of positive and rejected traces through the Python machine,
and replay any TLC counterexample that maps to Python inputs. Check
the transition ID, reason, state, grant and the appended event at each
step. The generated tables avoid duplicating edge definitions.
Replay covers guards and side effects outside those definitions. Keep
the database integration tests for the transaction implementation.

### Measured bounds

The local prototype covered all four contracts in each run. Both
profiles allow one crash and at most two attempts. In the two-slot
profile the second slot can be admitted after the first releases the
intent. Only the first slot can progress past Proposed. This still
exercises simultaneous admission as a safety condition. With one
abstract recorded effect ID per slot, I1 detects a reset after
confirmation. A later model needs distinct effect IDs to exercise
replacement of one ID by another. Both extensions need new bounds
and another measurement.

| Proposed profile | Slots for one intent | C1 probes | TLC workers | Generated states | Distinct states | Elapsed |
| --- | --- | --- | --- | --- | --- | --- |
| CI | 1 | 1 | 1 | 2,151,667 | 658,490 | 31 s |
| Nightly | 2 | 2 | 2 | 4,216,436 | 1,426,166 | 49 s |

The measurements use breadth-first search and `-fp 0` on macOS
with Eclipse Adoptium Java 25.0.2. The heap limits are 1 GiB for CI
and 2 GiB for nightly. The limits describe the measured prototype,
so measure the final implementation on GitHub Actions before
setting a runtime budget.

### Tooling

Pin the official [TLA+ tools v1.7.3 release](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.3)
and its [MIT licence](https://github.com/tlaplus/tlaplus/blob/v1.7.3/LICENSE).
MIT is in Apache's [Category A](https://www.apache.org/legal/resolved.html#category-a).
The upstream jar also contains an Eclipse Public License 2.0 notice
in `META-INF/LICENSE.txt`. Apache lists EPL 2.0 under
[Category B](https://www.apache.org/legal/resolved.html#category-b).
Fetch and run the unmodified jar as a separate development tool.
Keep it out of FiveFold's source and package releases.
The release's `tla2tools.jar` has SHA-256
`ae7a33bbe99e5a3783c28d826d20e0028fc87f5a8cc8f9520afab00eabbc0bb1`.
The jar manifest identifies source revision `3ea322254974cd39a4dd336fff12b203a6743dd1`.
The jar's own `tlc2.TLC -help` identifies TLC2 2.18, revision
`3ea3222`, and documents `-config`, `-workers`, `-metadir` and `-fp`.
The project's [tool usage guide](https://github.com/tlaplus/tlaplus/blob/master/USE.md)
specifies Java 11 or later. Use Java 17 LTS in CI and verify it on
the implementation PR.

Add `make tla-setup` to download that jar from the release and verify
its checksum before installation. Install Java and the jar on CI
before running the check, using GitHub release assets as the Temporal
CLI setup already does. Run the bounded profile through `make check`
and the larger profile in the scheduled workflow. If Java or the verified
jar is missing, fail with the setup command. The model checker needs
no download while it runs.

## Alternatives considered

- Model the full simulator and agent. This could expose wider effects,
  but it mixes the gateway contract with one scenario, policy and
  provider and substantially enlarges the state space.
- Model only the transition graph. It checks legal graph edges and
  misses interactions between durable attempts, responses and
  independent effects.
- Copy the transition table by hand. It is easy to read but can drift
  from the running code. Generating it from Python and checking
  representative trace replay covers both forms of drift.
- Check unconditional liveness. This would reject permitted indefinite
  waits and unresolved escalations. Explicit progress assumptions
  belong in any later liveness profile.
- Run the two-slot profile on every PR. Keep the bounded one-slot
  search on each PR and use nightly for the larger graph. Continue
  checking intent contention in the existing PostgreSQL tests on PRs.
- Follow the latest TLC release or vendor a jar in the repo. Latest
  would change checks without review. Vendoring would add a binary to
  the repo. Use the official release artifact with a fixed checksum.

## Findings and consequences

TLC exhausted both bounded queues without an invariant violation or
unexpected deadlock. The prototype deliberately permits underlying
duplicate effects and an incorrect settled-empty answer under a
contract violation. The gateway invariants compare its decisions with
its records, while the evaluator's five properties use the simulator's
evidence. The model does not establish those properties for real
services or prove unbounded safety.

If a later check finds a gateway invariant violation or a stuck state,
save the trace, map its steps to Python and propose a separate
decision record for any design change.

## Spec changes on acceptance

- Define the checked scope and the journal projection under Action
  state machine and Testing. Keep I1 to I7 as written.
- Record the pinned TLC and Java setup under Stack and reserve
  the implementation for a later v0.2 release.
