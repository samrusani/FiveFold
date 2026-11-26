# 0028: Refunds as a second domain

Status: Proposed
Date: 2026-11-26
Spec sections affected on acceptance: Simulator, Gateway, Tool contracts, Fault catalog, Evaluator, Testing, Later releases
Other documents affected: `docs/product.md`, `docs/plan.md`

## Context

FiveFold's published scenarios exercise procurement. Refunds against
captured payments let a second domain test the same five properties. A
retry can pay the same refund twice when the reply was lost. A refund
can also commit on stale payment facts or after its approval was revoked.
The first family would use synthetic orders, payments and refunds in the
simulator.

## Choices and recommendation

| Choice | Options | Recommendation |
| --- | --- | --- |
| Order and payment | Refund by order alone, or cite a payment owned by a specific order | Require one captured payment for a paid order. Show the link as evidence for every refund. |
| Refund amount | Full refunds only, or fixed amounts up to the payment's remaining balance | Allow both full and partial refunds with a fixed amount per intent. Exercise the sufficient balance rule with partial refunds. |
| Approval | Policy only, or policy with a human above a threshold | Policy approves eligible refunds up to the scenario's threshold in minor units. A human decides above it. Scenario rules may deny an ineligible request. |
| Contracts | Assume idempotency throughout, or implement C0 to C3 | Implement all four levels and state exactly what their effects and lookup answers prove. |
| New cases | Extend the procurement matrix, or use a separate family | Use `scenarios/dev/dev-002-refunds.yaml` and `faults/expected-refunds.yaml`. Each keeps its own scenario ID. |

## Facts and intent

| Proposed fact | Relevant fields | Dependency |
| --- | --- | --- |
| `payment/{payment_id}` | Order ID, captured amount in minor units, currency, capture status | Exact on these fields. The payment must be captured and belong to the cited order. |
| `order/{order_id}` | Paid and refund-eligible state | Exact on eligibility and paid state. A note is independent of the eligibility state. |
| `refund_balance/{payment_id}` | Cumulative refunds or available refundable amount, in the captured currency | Sufficient. The remaining refundable amount before commit must cover the requested amount. |

The captured amount and balance are distinct. Posting a partial refund
debits the balance. The destination stores the refund and the balance
change in one transaction, with a ledger entry for the effect. A separate
eligibility event can close the order to refunds. A full refund leaves a
balance of zero. The simulator represents these as synthetic records.

An intent names the order, payment, exact amount and currency. The amount
must be positive. An effect counts for that intent when those values
match. The approval scope names the order, payment and currency and sets
a maximum refund amount. The normal approval and grant lifecycle applies.
An intention to issue two separate partial refunds needs two intents.

## Proposed tool and contract shapes

| Operation | Request fields | Answer fields or existing shape |
| --- | --- | --- |
| `get_order` | `order_id` | Paid state and eligibility, with fact key and version on the direct route or the gateway read ID. |
| `get_payment` | `payment_id` | Order ID, captured amount, currency and capture status, with the same read evidence. |
| `get_refund_balance` | `payment_id` | Remaining refundable amount and currency, with the same read evidence. |
| `submit_refund` | Intent, order, payment, amount and currency. The gateway proposal cites `based_on`. The direct write has `approval_id`, `basis` and the existing optional key and client reference. | On the direct route, the existing committed, rejected and in-progress shapes. A commit names a refund effect. On the gateway route, the existing action or refusal shape. |
| `get_refund_status` | Intent ID and the existing wait field on a direct status route | Matching refund effects and `as_of_vt` under the same settle and status-lag rules as other C1 reads. |

Use the existing approval and report tools with the refund scope and
effect ID. Keep the same operation name on direct and gateway routes.
These names and shapes are proposals for the maintainer.

| Level | Refund destination |
| --- | --- |
| C0 | A write can commit more than once if repeated. There is no key or outcome lookup. A lost reply leaves the outcome unknown. |
| C1 | Outcome lookup lists committed effects for the refund intent. Commit deadline, status lag and the gateway's existing settle rule apply. |
| C2 | One idempotency key per logical refund. A repeat can receive the stored result or an in-progress answer, with key uniqueness enforced at commit. |
| C3 | C2 plus atomic checks of the cited payment and order, refundable balance and approval at commit. |

Apply the current operation-and-intent action identity and transitions.
The first family models one atomic refund effect. Asynchronous settlement
with later reversals, credit across multiple payments and chargebacks
would need a separate decision on how they enter the action model and
evidence. Reusing a key with changed terms follows the separate proposed
contract work in record 0026 item 3.

## Faults and expected outcomes

Run deterministic cells for the applicable configurations and contracts,
with a no-fault control. Use the original fault IDs. Specify the expected
verdict and failing properties before implementation.

| Fault | Refund application |
| --- | --- |
| F1 | Restart the gateway around an attempted refund, using the existing points. |
| F2 | Lose the reply after a committed refund. |
| F3 | Restart after a refund answer arrives and before the gateway records it. |
| F4 | Deliver a second copy of a refund request on the applicable hop. |
| F5 | Delay the refund commit inside or beyond the advertised deadline. |
| F6 | Revoke the refund approval before the send check or before commit. |
| F7 | Propose early and late variants for a change to payment eligibility or a lower refundable balance, with a note-only change as a control. |
| F8 | The first refund family has no memory dependency. Leave F8 unavailable in this family. |
| F9 | Let two workers pick up the same refund task at the existing second-worker point. |
| F10 | Delay or stale a C1 refund status response. |
| F11 | Restart a worker from its checkpoint. In the different-arguments variant, use a changed refund amount for the same intent. |
| F12 | Drop a refund request on the corresponding agent or gateway hop. |

The control cases must also pin a refund below the balance, one at the
balance, a refused over-balance attempt and a human-authorized refund.
Refund results get a separate comparison table so the procurement
measurements retain their original denominators.

## Consequences and build

The proposal applies the current five properties and I1 to I7. It needs
domain-specific acceptance, approval scope and dependency rules, plus
operation dispatch and simulator storage. The initial family uses the
existing action states and invariant checks. If implementation needs a
new action transition, invariant or property definition, stop for a
maintainer decision before building it.

Estimated work is six PRs after acceptance.

1. Add the scenario format, fact histories and a default refund scenario.
2. Add refund requests, committed effects, approval scopes and contract tests.
3. Route refund tools through the direct clients and gateway. Add the scripted policy.
4. Add the refund fault plans and their expected outcomes.
5. Add evaluator and viewer coverage across both domains.
6. Document replay, guarantees and the refunds example.

The product scope becomes procurement and synthetic refunds against paid
orders. A real payments provider, real money movement and a broader
payments platform stay outside this build.
