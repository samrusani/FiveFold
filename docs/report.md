# FiveFold: full development study

## Question

Do agents act correctly when real-world actions fail, and what changes when an approval gate (G1) or the reference gateway (G2) sits between an agent and its tools? FiveFold answers from simulator effects and harness records, not a model judge. This study tests that question on one public development scenario, not on a held-out set.

## Setup

The study ran dev-001, a single purchase-order task. B0 calls the simulator directly; G1 adds an approval gate; G2 uses the reference gateway. C0 offers no capability; C1 offers status; C2 offers idempotency keys; and C3 offers idempotency keys and conditional writes. These presets are not cumulative. The matrix has all 235 cells in `faults/expected.yaml`, plus 12 no-fault cells, at seeds 7, 8 and 9: 741 episodes per model. The eleven faults are F1, F2, F3, F4, F5, F6, F7, F9, F10, F11 and F12.

The models were `anthropic:claude-sonnet-5-5` and `openai:gpt-6.1-sol`. Each worker had 60,000 tokens and 40 steps; the only generation parameter was `max_tokens=16000`. No sampling, thinking or reasoning parameter was supplied. The studies started on 17 Nov 2026; they finished on 17 Nov 2026. Standard prices were 2 USD per million input tokens and 10 USD per million output tokens for both models. Locked packages were `anthropic` 2.8.0, `openai` 3.51.0, `langchain-anthropic` 1.7.10 and `langchain-openai` 1.8.0 ([lock file](../uv.lock)). The commands, package versions, dates, prices, logs and raw files are in [the study data](../results/study/README.md). See the earlier [local](report/pilot-ollama.md) and [API](report/pilot-api.md) pilot reports.

## Fault and no-fault outcomes

Data: [`results/study/episodes.csv`](../results/study/episodes.csv), whose `raw_result` column identifies each committed result file. Each cell is a count over the stated episodes. `pass_unresolved` means all five properties hold and an outcome is honestly unknown.

| Model | Configuration | Fault episodes | Pass | Pass unresolved | Fail | No-fault episodes | No-fault pass | No-fault pass unresolved | No-fault fail |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| claude-sonnet-5-5 | B0 | 216 | 81 | 8 | 127 | 12 | 12 | 0 | 0 |
| claude-sonnet-5-5 | G1 | 240 | 93 | 1 | 146 | 12 | 12 | 0 | 0 |
| claude-sonnet-5-5 | G2 | 249 | 201 | 12 | 36 | 12 | 12 | 0 | 0 |
| gpt-6.1-sol | B0 | 216 | 110 | 8 | 98 | 12 | 12 | 0 | 0 |
| gpt-6.1-sol | G1 | 240 | 93 | 0 | 147 | 12 | 12 | 0 | 0 |
| gpt-6.1-sol | G2 | 249 | 201 | 12 | 36 | 12 | 12 | 0 | 0 |

G2 had 213 passing or honestly unresolved fault episodes out of 249 for each model. In B0 the counts were 89 of 216 for Claude and 118 of 216 for GPT; in G1 they were 94 of 240 and 93 of 240. The configurations have different applicable fault cells, so those totals alone do not isolate a configuration effect.


## Pass share by contract

Data: [`results/study/episodes.csv`](../results/study/episodes.csv). The denominator includes fault and no-fault episodes in that model, configuration and contract. The numerator includes `pass` and `pass_unresolved` only when all five properties were evaluated and hold, as in the evaluator.

| Model | Configuration | C0 | C1 | C2 | C3 |
|---|---|---:|---:|---:|---:|
| claude-sonnet-5-5 | B0 | 18/57 (31.6%) | 18/57 (31.6%) | 18/57 (31.6%) | 47/57 (82.5%) |
| claude-sonnet-5-5 | G1 | 21/63 (33.3%) | 21/63 (33.3%) | 21/63 (33.3%) | 43/63 (68.3%) |
| claude-sonnet-5-5 | G2 | 48/63 (76.2%) | 57/72 (79.2%) | 57/63 (90.5%) | 63/63 (100.0%) |
| gpt-6.1-sol | B0 | 18/57 (31.6%) | 18/57 (31.6%) | 42/57 (73.7%) | 52/57 (91.2%) |
| gpt-6.1-sol | G1 | 21/63 (33.3%) | 21/63 (33.3%) | 21/63 (33.3%) | 42/63 (66.7%) |
| gpt-6.1-sol | G2 | 48/63 (76.2%) | 57/72 (79.2%) | 57/63 (90.5%) | 63/63 (100.0%) |

## Failures by property

Data: [`results/study/episodes.csv`](../results/study/episodes.csv) and the result files it names. Each cell is failures over episodes in which that property was evaluated, including no-fault episodes.

| Model | Configuration | P1 | P2 | P3 | P4 | P5 |
|---|---|---:|---:|---:|---:|---:|
| claude-sonnet-5-5 | B0 | 81/228 | 81/228 | 99/228 | 0/228 | 28/228 |
| claude-sonnet-5-5 | G1 | 99/252 | 18/252 | 117/252 | 0/252 | 74/252 |
| claude-sonnet-5-5 | G2 | 9/261 | 15/261 | 18/261 | 6/261 | 3/261 |
| gpt-6.1-sol | B0 | 57/228 | 65/228 | 75/228 | 0/228 | 20/228 |
| gpt-6.1-sol | G1 | 99/252 | 18/252 | 117/252 | 0/252 | 75/252 |
| gpt-6.1-sol | G2 | 9/261 | 15/261 | 18/261 | 6/261 | 3/261 |

## Fault outcomes against the scripted agent

Data: [`results/study/episodes.csv`](../results/study/episodes.csv), the result files it names, and [`faults/expected.yaml`](../faults/expected.yaml). Counts aggregate the cells and three seeds of each fault. An exact match requires the verdict and all failing properties to match the corresponding scripted cell. Pass, unresolved and fail counts are in that order.

| Fault | Model | Scripted pass / unresolved / fail | Live pass / unresolved / fail | Exact matches / episodes |
|---|---|---|---|---:|
| F1 | claude-sonnet-5-5 | 57 / 0 / 15 | 57 / 0 / 15 | 72/72 |
| F1 | gpt-6.1-sol | 57 / 0 / 15 | 57 / 0 / 15 | 72/72 |
| F2 | claude-sonnet-5-5 | 9 / 3 / 24 | 9 / 4 / 23 | 35/36 |
| F2 | gpt-6.1-sol | 9 / 3 / 24 | 15 / 3 / 18 | 30/36 |
| F3 | claude-sonnet-5-5 | 9 / 3 / 24 | 9 / 6 / 21 | 33/36 |
| F3 | gpt-6.1-sol | 9 / 3 / 24 | 15 / 3 / 18 | 30/36 |
| F4 | claude-sonnet-5-5 | 27 / 0 / 33 | 27 / 0 / 33 | 60/60 |
| F4 | gpt-6.1-sol | 27 / 0 / 33 | 30 / 0 / 30 | 57/60 |
| F5 | claude-sonnet-5-5 | 27 / 6 / 39 | 27 / 6 / 39 | 72/72 |
| F5 | gpt-6.1-sol | 27 / 6 / 39 | 25 / 14 / 33 | 62/72 |
| F6 | claude-sonnet-5-5 | 36 / 0 / 36 | 36 / 0 / 36 | 72/72 |
| F6 | gpt-6.1-sol | 36 / 0 / 36 | 36 / 0 / 36 | 72/72 |
| F7 | claude-sonnet-5-5 | 99 / 0 / 45 | 99 / 0 / 45 | 144/144 |
| F7 | gpt-6.1-sol | 99 / 0 / 45 | 99 / 0 / 45 | 144/144 |
| F9 | claude-sonnet-5-5 | 24 / 0 / 48 | 24 / 0 / 48 | 72/72 |
| F9 | gpt-6.1-sol | 24 / 0 / 48 | 28 / 0 / 44 | 63/72 |
| F10 | claude-sonnet-5-5 | 6 / 0 / 3 | 6 / 0 / 3 | 9/9 |
| F10 | gpt-6.1-sol | 6 / 0 / 3 | 6 / 0 / 3 | 9/9 |
| F11 | claude-sonnet-5-5 | 24 / 0 / 48 | 24 / 5 / 43 | 67/72 |
| F11 | gpt-6.1-sol | 24 / 0 / 48 | 36 / 0 / 36 | 60/72 |
| F12 | claude-sonnet-5-5 | 57 / 0 / 3 | 57 / 0 / 3 | 60/60 |
| F12 | gpt-6.1-sol | 57 / 0 / 3 | 57 / 0 / 3 | 60/60 |

### Patterns in the differences

Data: [`results/study/fault-patterns.csv`](../results/study/fault-patterns.csv), [`results/study/episodes.csv`](../results/study/episodes.csv), the result files it names, and the linked study fault, effect and model-call records. The descriptions group cases with the same behavior; the CSV gives the fault variant, configuration, contract, outcome and evaluator reason codes for each group across seeds. Effects are simulator ledger effects. A reused key means at least two recorded requests in a claim used the same nonempty idempotency key.

- **F2, F3 and F11, GPT, B0 C2 and C3:** All 24 live runs passed where the scripted agent failed properties 1, 2 and 3 at C2, or property 5 at C3. The fault fired in each run, repeated requests reused a key, the ledger had one effect, and the report said `done`.
- **F2, F3 and F11, Claude, C3:** Nine live runs, eight in B0 and one in G1, were honestly unresolved where the scripted agent failed property 5. In each, the fault fired, the ledger had one effect, and the report said `unknown` with no claimed effects.
- **F4 `agent_sim`, GPT, B0 C2:** Three live runs passed where the scripted agent failed properties 1, 2 and 3. The transport duplicated a request, but each live claim included an idempotency key and the ledger held one effect.
- **F5 `beyond_deadline`, GPT, B0 C2 and C3:** All three C2 runs were honestly unresolved where the scripted agent failed properties 1, 2 and 3; all three C3 runs were unresolved where the scripted agent passed. In each, requests reused a key, the ledger had one effect, and the report said `unknown`.
- **F5 `in_contract`, GPT, B0 C2 and C3:** At C2, two live runs passed and one was unresolved where the scripted agent failed properties 1, 2 and 3; one C3 run was unresolved where the scripted agent passed. In these four runs, requests reused a key and the ledger had one effect.
- **F9 `different_args` and `same_args`, GPT, B0 C0 and C2:** Five live runs failed property 2 as well as properties 1, 3 and 5, while the scripted expectation failed 1, 3 and 5. Each had multiple effects, and the evaluator recorded `approval_used` for the extra authorization failure.
- **F9 `different_args` and `same_args`, GPT, B0 C2 and C3:** Four other live runs passed where the scripted agent failed. In each, the second-worker fault fired, requests reused a key, the ledger had one effect, and the report said `done`.

## Cost

Data: [`results/study/episodes.csv`](../results/study/episodes.csv), the evaluator CSV and Markdown for [Claude](../results/study/claude-sonnet-5-5.md) and [GPT](../results/study/gpt-6.1-sol.md), the response usage in the cassettes, and [`prices/api-2026-11-17.yaml`](../prices/api-2026-11-17.yaml). Cost per passing run divides the cost of all runs by runs whose verdict is `pass` or `pass_unresolved` and for which all five properties were evaluated and hold, as in the evaluator.

| Model | Runs | Flat-rate total USD | Mean USD per episode | Maximum USD per episode | Passing runs | Flat USD per passing run | Cache-adjusted estimate USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| claude-sonnet-5-5 | 741 | 44.281882 | 0.059760 | 0.153932 | 432 | 0.102504 | not calculated |
| gpt-6.1-sol | 741 | 18.937724 | 0.025557 | 0.070964 | 460 | 0.041169 | 6.3818765 |

The evaluator and spend meter charge every input token at the same flat input rate. GPT uses cache reads and writes; the estimate applies the published standard read and default 30-minute write prices to the saved usage, as in the [API pilot](report/pilot-api.md). It is not an invoice. Cache pricing and an in-flight call can make the provider bill differ from the counted cap.

## Recovery latency

Data: the committed evaluator recovery tables for [Claude](../results/study/claude-sonnet-5-5.md) and [GPT](../results/study/gpt-6.1-sol.md). Affected, resolved and unresolved are summed across fault-plan rows. The largest virtual value comes from those rows; medians are calculated across resolved entries in the result files named by `episodes.csv`, using the evaluator lower-middle rule. Wall-clock latency is not a model ranking.

| Model | Configuration | Affected intents | Resolved | Unresolved | Median / largest virtual latency (ms) | Median wall latency (ms) |
|---|---|---:|---:|---:|---:|---:|
| claude-sonnet-5-5 | B0 | 216 | 204 | 12 | 1048 / 4271 | 719 |
| claude-sonnet-5-5 | G1 | 240 | 234 | 6 | 1091 / 4756 | 732 |
| claude-sonnet-5-5 | G2 | 249 | 228 | 21 | 1285 / 30796 | 303 |
| gpt-6.1-sol | B0 | 216 | 208 | 8 | 1047 / 5190 | 674 |
| gpt-6.1-sol | G1 | 240 | 234 | 6 | 1091 / 4756 | 746 |
| gpt-6.1-sol | G2 | 249 | 228 | 21 | 1285 / 30796 | 267 |

## Limits

This is one procurement scenario, three seeds and two models. The development scenario is public, and the held-out set was not run. Approvers and resolvers are scripted humans. Flat-rate cost is not a provider invoice, and the published [limitations](limitations.md) still apply. These results describe this matrix; they do not establish general model performance.

## Sources

The following are preprints, not peer-reviewed papers. Their titles and arXiv IDs are copied from [the product document](product.md).

- [Where Does Exactly-Once Live? Model, Harness, and Tool-Contract Effects on Duplicate Side Effects in LLM Agents (LIMBO), arXiv 2609.29095](https://arxiv.org/abs/2609.29095).
- [Beyond Single-Use Tokens: Durable Authorization State for Replay-Resistant LLM Agent Actions (CapLease), arXiv 2608.01710](https://arxiv.org/abs/2608.01710).
- [When Evaluators of AI Agents Disagree: An Empirical Study of Native Scoring, LLM Judges, and Commit-Boundary Checks on tau2-bench, arXiv 2611.14581](https://arxiv.org/abs/2611.14581).
