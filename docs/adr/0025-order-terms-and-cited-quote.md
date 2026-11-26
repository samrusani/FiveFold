# 0025: Order terms and the cited quote

Status: Proposed
Date: 2026-11-26
Spec sections affected on acceptance: Evaluator, Testing
Other documents affected: `docs/limitations.md`, `docs/adr/0004-five-properties.md`

## Context

An order cites a supplier quote. Property 3 checks that the relevant
fields of that quote are unchanged since the cited version. Property 4
checks that a committed order satisfies the intent, including its maximum
unit price. Neither check requires the order's unit price or currency to
equal the quote's, its quantity to meet the quote's minimum, or the quote
to be active. The simulator's C3 check uses the same dependency rule.
[Properties 3 and 4](0004-five-properties.md) and [destination contracts](0005-tool-contract-levels.md)

The v0.2.0 audit (H4) found that a mismatched price could pass all five
properties in the development scenario. It also found that the policy
threshold in the human-approval scenario uses the submitted amount, so an
order whose terms differ from the quote can take a different approval
path. These runs follow the current definitions. The published study's
committed unit prices were 540 or 567 minor units, both quoted prices
in the study. A check against the quote at commit could still reclassify
some effects when F7 changed the quote after the read.

## Options

Terms match when the price and currency equal the quote's, the quantity
meets its minimum, and the quote is active. The options differ in which
version they check and whether they measure or prevent a mismatch.

| Option | Properties and runtime | Refusal, reason and tool contracts | Expected cells and published results |
| --- | --- | --- | --- |
| A. Score the terms from the cited quote | Add the terms check to property 3 after evidence shows that the quote was read. Keep the comparison between the cited and pre-commit versions. Properties 1, 2, 4 and 5 and runtime behavior stay as defined. | Keep all runtime refusals, destination reasons and tool shapes. Add `quote_terms_mismatch` as a property 3 failure reason. | A new scripted mismatch cell would fail property 3. No existing scripted verdict is expected to change. The late F7 failure still follows from the change after the read. Verify every cell and re-score saved study evidence before deciding whether to adopt this option. |
| B. Refuse mismatched terms at G2 admission | Check the served quote before filing for approval. Keep the five scoring rules. A refused order that is never corrected can leave property 4 unmet. G1 and the direct baselines keep their current behavior. | Keep write arguments and response shapes. Add a G2 `quote_terms_mismatch` refusal. | No existing scripted verdict is expected to change because the scripted agent uses the terms it read. Existing study records and their scores stay as published. A new G2 study could change approvals, effects and outcomes for any proposals whose terms differ. |
| C. Refuse mismatched terms at the simulator | At C0 through C3, compare against the current quote at commit. Keep the five scoring rules. A refusal can change whether property 4 is met and the evidence for a property 5 report. This adds a destination check at C0 through C2. | Keep request and response fields. Add the definitive `quote_terms_mismatch` reason to `WriteRejected` for fresh requests. Keep the existing idempotency lookup ahead of the check. | The 27 `early_price_up` and `late_price_up` F7 cells whose currently committed stale effects fail property 3 would follow a refusal path. Rerun those cells to determine if the agent recovers and which verdicts change. The study's F7 episodes could also change on a rerun. Keep the original artifacts as historical results. |
| D. Document the current limit | Document that freshness compares versions and that intent acceptance uses a maximum. Keep the five properties and all runtime behavior. | Keep every refusal, reason and tool shape. | Keep all expected cells and published results. |

## Recommendation

Choose A for a later release, after validating the expected cells and the
saved study evidence. It measures the same terms against the same
evidence in all five configurations. The separate freshness check still
detects a change after the read. A destination refusal would change the
contracts FiveFold compares, and a G2 admission check would cover only one
configuration. Document the limit in the scoring contract so readers can
distinguish detecting a mismatch from preventing the write or constraining
the authority's auto-approval policy.

## Consequences

This proposal leaves the v0.2.1 code, scoring, expected cells and study
exports unchanged. The 27 F7 cells above are the stale-commit paths in
`faults/expected.yaml` at C0 through C2. The F7 harmless variants and the
C3 cases are controls for any experiment with the simulator option.

## Spec changes on acceptance

- **Evaluator.** Define the additional check against the served quote and
  its failure reason. Amend record 0004 to match.
- **Testing.** Add a mismatch cell and verify the existing cell outcomes
  and saved study evidence under the accepted definition.
- **Limitations.** Explain the scope of the terms check and the separate
  approval and destination contracts.
