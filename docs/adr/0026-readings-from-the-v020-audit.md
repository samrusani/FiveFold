# 0026: Readings from the v0.2.0 audit

Status: Proposed
Date: 2026-11-26
Spec sections affected on acceptance: Authority, Trust boundaries, Simulator, Evaluator, Testing

## Context

Each item can be decided on its own. The v0.2.1 patch leaves the five
properties, protocols, grants and expected outcomes as they are.

### 1. Memory and feasibility (H5)

Record [0004](0004-five-properties.md) excuses a missing effect when no
allowed option is feasible, with the run's own effects removed.
`domain.scenario.options` and `feasible_together` use the quote, budget
and inventory rules. Record [0023](0023-memory-correction.md) adds an
exact memory dependency but does not extend the feasibility rule. The
audit found a development-scenario variant whose only remaining option
had no memory fact. The loader allowed it and property 4 marked the intent
missing, although an order from that option could not pass property 3.

**Options.** Keep feasibility independent of the evidence requirements, or
require the memory fact to exist when testing any memory-enabled order's
feasibility. The latter changes both initial scenario validation and the
run-end `infeasible` check. A channel classification would require a
separate rule. Record 0023 describes checking the exact fact read.

**Recommendation.** Amend 0023 to include the needed memory fact in
`options` and `feasible_together`. Apply it to purchase orders in
memory-enabled scenarios. Use the same fact set for the existing
initial and run-end checks. No current expected cells change. A new
memory scenario test would exercise the missing-fact case.

### 2. `FORBIDDEN` as settlement evidence (M5)

The evidence reader counts a definitive same-key rejection after the
hop bound at C2 and C3. This follows the settlement argument in
[0005](0005-tool-contract-levels.md) and property 5 of
[0004](0004-five-properties.md). A `FORBIDDEN` response occurs before the
destination looks up the key. It says nothing about an earlier request
with that key. The evaluator also infers capabilities from `harness.run`'s
contract label, while `sim.run.capabilities` records what the simulator
provided.

**Options.** Keep the current inference for configured presets, or require
evidence of an authoritative key lookup and consult the destination's
recorded capabilities. Label and capability mismatches can be surfaced as
inconsistent records if the evaluator also relies on the label elsewhere.

**Recommendation.** Exclude `FORBIDDEN` from same-key settlement and use
the capabilities recorded by the destination for the lookup rule, including
custom capability combinations. Keep the tool reply and reason. No expected
cells change because valid preset runs cannot reach the mismatch.

### 3. A reused key with other arguments (M9)

[0005](0005-tool-contract-levels.md) defines a write retry by its key.
[0002](0002-authority-approvals-and-grants.md), rule 4, defines approval request
reuse by `client_request_key`. Today the simulator returns the stored
effect or approval on a matching key, even when the new request's caller,
operation, intent or arguments differ.

**Options.** Keep key-only reuse and document that the original outcome is
returned, or bind a key to its original caller, operation, intent and
canonical business arguments. For approval requests, bind the normalized
approval scope. In the second option, a refusal such as
`idempotency_key_conflict` would name a mismatch without returning the
earlier result. It would provide no evidence that the earlier write never
committed. A normal retry would still return the earlier result or
`in_progress`, even if its transport reference changed.

**Recommendation.** Bind both kinds of key to their original request under
a later contract revision. Add mismatch cases for both kinds. No existing
expected cell is expected to change because copies and retries repeat the
same business request. Verify the cell matrix and saved playback before
acceptance.

### 4. Gateway tables (H9)

The gateway role can update and delete rows in `gw.action`, `gw.attempt`,
`gw.escalation`, `gw."grant"` and `gw.tool_call` under
[0008](0008-configurations-and-trust-boundaries.md). The event and report
histories, reads and proposals have append-only protections. The attempt
trigger protects its original send fields. The answer fields can still
change after the first answer. These gateway tables do not supply scoring
evidence under record 0004.

**Options.** Keep the current trust boundary and rely on the gateway state
machine, or tighten database grants and transitions. Remove `DELETE` for
`ff_gw` on these tables and allow an attempt answer and an escalation
resolution to be written once. Keep legitimate action and grant updates.

**Recommendation.** Tighten the grants and write-once fields in a later
migration after checking the gateway's normal writers, replay and existing
database upgrades. Runtime tool shapes and reasons should stay as defined.
No expected cell is expected to change under a conforming gateway.

### 5. Limits on otherwise valid tool arguments (M2)

The tool contracts in [0001](0001-action-identity.md),
[0004](0004-five-properties.md) and
[0008](0008-configurations-and-trust-boundaries.md) set no independent
length limit for `based_on` or `effect_ids`. They also allow transactions
below C3 to change a budget or inventory beyond its normal bounds.
Checking that an individual order's derived amount is encodable is a
format check. An otherwise encodable request can still make a cumulative
fact impossible to encode. Refusing that request would add a write rule
below C3. A shorter list or a fixed nesting limit for otherwise encodable
arguments would also limit currently valid inputs.

**Options.** Keep validating the current encodings and document their
limits, choose explicit maxima for otherwise valid inputs and resulting
facts, or support bigger values end to end with a common encoding.

**Recommendation.** Keep the existing schemas in 0.2.1. Measure the
largest useful inputs before proposing maxima. Specify what a refused
write means for an already-sent attempt before adding any rule at commit.
No existing expected cell requires a new limit.

## Spec changes on acceptance

Amend only the sections and earlier records for the items the maintainer
accepts. Add focused tests and verify the expected cells before changing
the implementation. Keep the original published study as a historical
result.
