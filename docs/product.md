# Product spec

FiveFold is an open-source test suite and reference gateway that checks whether AI agents act correctly when things fail. A task passes only if every real-world action happens exactly once, with valid authority, on fresh inputs, is actually committed, and is honestly reported. Version 0.1 ships a procurement simulator, three demos and a published comparison report, with no GPUs and no model training.

Status: M0 decisions accepted, 14 Nov 2026. Companion docs: [spec.md](spec.md) (technical spec), [plan.md](plan.md) (build plan) and the [decision records](adr/README.md).

## Problem

Agents now write to real systems through tools, and ordinary failures break them in ways a transcript hides.

- **Duplicates after a lost reply.** In the LIMBO study, agents still reported success in 90% of runs that produced a duplicate. Idempotency keys cut duplicates from 28% to 4%.
- **Stale approvals.** In the CommitGuard study, Qwen chose to commit 207 of 215 executions flagged as invalid. The controlled study did not let it revalidate.
- **Judges miss it.** In five of six judge settings, LLM judges passed 6 to 11 of 11 forbidden database writes. The sixth passed none but failed 40 of 55 fully correct episodes. A rule-based check caught all 11 forbidden writes.
- **The plumbing does not solve it.** Temporal warns activities may run more than once. LangGraph re-runs model and API calls after a checkpoint. MCP has no idempotency field, and a proposal to add one was closed unmerged.

All of this evidence comes from 2026 preprints that have not been peer-reviewed. See Sources at the end.

## Users

| User | What they do with it |
| --- | --- |
| Agent platform engineers | Run the suite against their agent stack before granting write access |
| MCP server and API authors | Check that their tools survive lost replies (conformance test, v0.2) |
| Teams on Temporal, DBOS or LangGraph | Compare their setup against published baselines |
| Researchers | Reproduce the 2026 failure modes in one place and extend them |

## The five properties

| # | Property | Passes when | Typical fault |
| --- | --- | --- | --- |
| 1 | Exactly once | Each intent has at most one committed effect, and nothing commits that the task did not intend | Lost reply, redelivery, crash after commit |
| 2 | Authorized at commit | Each committed effect cites one approval that was valid and in scope when it committed | Revoked approval, expired approval, replayed approval |
| 3 | Inputs still fresh | Every input the operation depends on was read, and still holds at commit under the dependency rules | Quote change; memory correction in v0.2 |
| 4 | Actually committed | Every required effect exists in the ledger, unless the authority withdrew it or it became infeasible | Crash before dispatch, request lost |
| 5 | Truthfully reported | The final report matches the ledger and the evidence the stack had, including "outcome unknown" | Success claimed after a lost reply |

Properties 1, 2, 3 and 5 are safety properties: nothing bad happened. Property 4 is a liveness property: the required effect happened. A run passes when all five hold. It passes unresolved when all five hold but some outcome is honestly reported as unknown.

Scoring reads ground truth from the simulator's and the harness's records, never the transcript or the gateway's own tables, and uses no LLM judge. Decision record 0004 defines every check.

## Scope for v0.1

In scope:

- One domain: procurement, with quotes, suppliers, budgets, approvals, purchase orders and inventory reservations.
- A reference action gateway and a durable action state machine with an explicit "outcome unknown" state.
- A fault scheduler with a fixed catalog of fault types, and a deterministic runner with a virtual clock, so a seed and recorded model outputs reproduce a run exactly.
- A deterministic evaluator for the five properties, plus cost and recovery metrics.
- One agent framework (LangGraph), run with API models and local models.
- An evidence viewer, three demos and one comparison report.

Out of scope (non-goals): an agent framework, a durable execution engine, a model router or a memory product. No receipts or signing, no TEE, no training, no GPU rental, no general chaos tool and no hosted service.

## Demos

1. **Order placed, reply lost.** The purchase service commits, the reply is dropped and the agent is killed. While the agent is down, the gateway retries with the same idempotency key and gets the stored result. The resumed agent re-sends its proposal, which attaches to the confirmed action, so no second order is placed. Without the gateway, the resumed agent places a second order. The viewer shows the agent's view beside the ledger.
2. **Approval goes stale.** After approval, the quote changes or the approval is revoked. The gateway refuses to send and names which fact or approval invalidated which action. Without the gateway, the order goes through anyway.
3. **Outcome unknown.** The same lost reply hits a destination with no idempotency key and no status query (contract level C0). The system reports "outcome unknown" and what must be reconciled, instead of guessing.

Each demo runs the baseline and the gateway side by side on the same seed. A run takes seconds; walking through a demo in the viewer takes about three minutes. Demos use a scripted model, so they need no API key. Decision record 0006 pins each demo's faults and contract level.

## Success measures

These are working targets, not commitments.

- A stranger reproduces demo 1 from the README in under 15 minutes on a clean machine.
- The report publishes raw results, denominators, configurations and model versions.
- Every fault variant has deterministic tests that match a published expected-results matrix. For every fault except those that test the gateway's own recovery (F1, F10, F12), at least one test shows the naive baseline failing where the gateway passes.
- At least one issue or contribution from an outside person within a month of launch.

## Positioning

Every piece has 2026 prior work, but nobody scores all five properties together, and most of the strongest work has no released code.

| Prior work | What it covers | What FiveFold adds |
| --- | --- | --- |
| LIMBO (preprint) | Duplicates and false success after lost replies | Authority and freshness checks; runnable code (LIMBO's release unconfirmed) |
| CapLease | One approval executed more than once | Freshness and reporting checks |
| CommitGuard | Stale or revoked authority at commit | Duplicates and reporting checks |
| Cordon | Outbox, idempotency keys, scoped approval | Released code, a reporting check, baselines |
| IdempotencyBench, UndoBench | Duplicate-effect benchmarks | Authority and freshness, a reference gateway |
| tau2-bench | Business tasks scored on final database state | Fault injection |
| ActionProxy | Gateway with single-use grants | Fault injection and evaluation |

Two openings remain: an MCP conformance test for lost replies (requested in MCP issue #3394) and published baselines on Temporal and DBOS. Record 0018 defines the Temporal baseline for v0.2.

## Decisions and open questions

- **Name:** FiveFold; the Python package and its commands are `fivefold`. The name was unclaimed on PyPI and npm on 14 Nov 2026. The repository was renamed from AgentCheck to FiveFold on 15 Nov 2026.
- **License:** Apache-2.0, which includes a patent grant.
- **AliceMemory:** v0.2. It drives the memory-correction fault (F8), which decision record 0006 defers.
- **Open: live study.** Which models, and the spend cap after the pilot.

## Sources

All papers are preprints that have not been peer-reviewed. Titles, IDs and the figures above were checked against arXiv on 16 Nov 2026.

- [Where Does Exactly-Once Live? Model, Harness, and Tool-Contract Effects on Duplicate Side Effects in LLM Agents (LIMBO), arXiv 2609.29095](https://arxiv.org/abs/2609.29095)
- [Beyond Single-Use Tokens: Durable Authorization State for Replay-Resistant LLM Agent Actions (CapLease), arXiv 2608.01710](https://arxiv.org/abs/2608.01710)
- [When Evaluators of AI Agents Disagree: An Empirical Study of Native Scoring, LLM Judges, and Commit-Boundary Checks on tau2-bench, arXiv 2611.14581](https://arxiv.org/abs/2611.14581)
- [ReliabilityBench: Evaluating LLM Agent Reliability Under Production-Like Stress Conditions, arXiv 2601.06112](https://arxiv.org/abs/2601.06112)
- [Temporary Authority, Permanent Effects: Commit-Time Authorization for LLM Agents (CommitGuard), arXiv 2607.10487](https://arxiv.org/abs/2607.10487)
- [Cordon: Semantic Transactions for Tool-Using LLM Agents, arXiv 2606.17573](https://arxiv.org/abs/2606.17573)
- [UndoBench: Separating Task Competence from Recovery Capability in Tool-Using AI Agents, arXiv 2610.05622](https://arxiv.org/abs/2610.05622)
- [IdempotencyBench](https://github.com/gssanjana4/idempotencybench)
- [ActionProxy](https://github.com/ActionProxy/actionproxy)
- [MCP issue #3394](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394) and [idempotencyKey proposal, closed](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3182)
- [Temporal activity docs](https://docs.temporal.io/activity-definition)
- [LangGraph time travel docs](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
- [tau2-bench](https://github.com/sierra-research/tau2-bench)
