# Product spec

Fivefold (working name) is an open-source test suite and reference gateway that checks whether AI agents act correctly when things fail. A task passes only if every real-world action happens exactly once, with valid authority, on fresh inputs, is actually committed, and is honestly reported. Version 0.1 ships a procurement simulator, three demos and a published comparison report, with no GPUs and no model training.

Status: draft, 14 Nov 2026. Companion docs: [spec.md](spec.md) (technical spec) and [plan.md](plan.md) (build plan).

## Problem

Agents now write to real systems through tools, and ordinary failures break them in ways a transcript hides.

- **Duplicates after a lost reply.** In the LIMBO study, agents still reported success in 90% of runs that produced a duplicate. Idempotency keys cut duplicates from 28% to 4%.
- **Stale approvals.** In the CommitGuard study, 207 of 216 runs whose approval had been invalidated still committed.
- **Judges miss it.** LLM judges passed 6 to 11 of 11 forbidden database writes; a rule-based check of the writes caught all 11.
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
| 1 | Exactly once | Each intended effect commits at most once, and once if the task requires it | Lost reply, redelivery, crash after commit |
| 2 | Authorized at commit | Each committed effect maps to one grant that was valid and in scope at commit time | Revoked approval, expired grant, replayed approval |
| 3 | Inputs still fresh | Every fact the decision used is unchanged at commit, or the change is declared harmless | Quote change, memory correction |
| 4 | Actually committed | Every effect the task requires exists in the destination's ledger | Crash before dispatch, silent failure |
| 5 | Truthfully reported | The agent's final report matches the ledger, including "outcome unknown" | Success claimed after a lost reply |

Scoring reads ground truth from the simulator's ledger, never the transcript, and uses no LLM judge.

## Scope for v0.1

In scope:

- One domain: procurement, with quotes, suppliers, budgets, approvals, purchase orders and inventory reservations.
- A reference action gateway and a durable action state machine with an explicit "outcome unknown" state.
- A fault scheduler with a fixed catalog of fault types.
- A deterministic evaluator for the five properties, plus cost and recovery metrics.
- One agent framework (LangGraph), run with API models and local models.
- An evidence viewer, three demos and one comparison report.

Out of scope (non-goals): an agent framework, a durable execution engine, a model router or a memory product. No receipts or signing, no TEE, no training, no GPU rental, no general chaos tool and no hosted service.

## Demos

1. **Order placed, reply lost.** The purchase service commits, the reply is dropped and the agent crashes. On restart the gateway reconciles, and no second order is placed. The viewer shows the agent's view beside the ledger.
2. **Approval goes stale.** After approval, the quote changes or a permission is revoked. The gateway refuses to dispatch and names which fact invalidated which action.
3. **Outcome unknown.** The same lost reply hits a tool with no idempotency key and no status query. The system reports "outcome unknown" and what must be reconciled, instead of guessing.

Each demo runs in about three minutes from a recorded mode that needs no API key.

## Success measures

These are proposed targets for the maintainer to confirm, not commitments.

- A stranger reproduces demo 1 from the README in under 15 minutes on a clean machine.
- The report publishes raw results, denominators, configurations and model versions.
- Every fault type has a deterministic test that fails on the naive baseline and passes on the gateway.
- At least one issue or contribution from an outside person within a month of launch.

## Positioning

Every piece has 2026 prior work, but nobody scores all five properties together, and most of the strongest work has no released code.

| Prior work | What it covers | What Fivefold adds |
| --- | --- | --- |
| LIMBO (Microsoft preprint) | Duplicates and false success after lost replies | Authority and freshness checks; runnable code (LIMBO's release unconfirmed) |
| CapLease | One approval executed more than once | Freshness and reporting checks |
| CommitGuard | Stale or revoked authority at commit | Duplicates and reporting checks |
| Cordon | Outbox, idempotency keys, scoped approval | Released code, a reporting check, baselines |
| IdempotencyBench, UndoBench | Duplicate-effect benchmarks | Authority and freshness, a reference gateway |
| tau2-bench | Business tasks scored on final database state | Fault injection |
| ActionProxy | Gateway with single-use grants | Fault injection and evaluation |

Two openings are untaken: an MCP conformance test for lost replies (requested in MCP issue #3394) and published baselines on Temporal and DBOS.

## Open decisions

- **Name:** Fivefold is a placeholder. Check GitHub, PyPI and npm before the first public commit.
- **License:** Apache-2.0 (includes a patent grant) or MIT.
- **AliceMemory:** in v0.1 or v0.2. It drives the memory-correction fault (F8).
- **Live study:** which models, and the spend cap after the pilot.

## Sources

Checked 14 Nov 2026. All papers are preprints.

- [Where Does Exactly-Once Live? (LIMBO), arXiv 2609.29095](https://arxiv.org/abs/2609.29095)
- [Beyond Single-Use Tokens (CapLease), arXiv 2608.01710](https://arxiv.org/pdf/2608.01710)
- [When Evaluators of AI Agents Disagree, arXiv 2611.14581](https://arxiv.org/abs/2611.14581)
- [ReliabilityBench, arXiv 2601.06112](https://arxiv.org/abs/2601.06112v1)
- [Temporary Authority, Permanent Effects (CommitGuard), arXiv 2607.10487](https://arxiv.org/pdf/2607.10487)
- [Cordon, arXiv 2606.17573](https://arxiv.org/pdf/2606.17573)
- [UndoBench, arXiv 2610.05622](https://arxiv.org/html/2610.05622)
- [IdempotencyBench](https://github.com/gssanjana4/idempotencybench)
- [ActionProxy](https://github.com/ActionProxy/actionproxy)
- [MCP issue #3394](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394) and [idempotencyKey proposal, closed](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3182)
- [Temporal activity docs](https://docs.temporal.io/activity-definition)
- [LangGraph time travel docs](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
- [tau2-bench](https://github.com/sierra-research/tau2-bench)
