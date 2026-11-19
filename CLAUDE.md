# CLAUDE.md

Rules for every coding agent working in this repo.

## What this is

FiveFold is an open-source test suite and reference gateway. It checks whether AI agents act correctly under failure: every real-world action happens exactly once, with valid authority, on fresh inputs, is actually committed, and is honestly reported.

Read these before writing code, in this order:

1. `docs/product.md`: what and why, scope, non-goals
2. `docs/spec.md`: the contracts you build against
3. `docs/plan.md`: milestones, teams, tickets
4. `docs/adr/`: the accepted decision records, which hold the detail and reasoning behind the spec

## Who decides what

- **The maintainer owns the semantics:** action identity, grant lifecycle, states and transitions, invariants I1 to I7, the five property definitions, tool names and return shapes, tool contract levels, and fault IDs.
- To change any of those, write a decision record in `docs/adr/` with status **Proposed** and stop. Do not mark it Accepted yourself.
- Everything else (internal module structure, helper names, test layout) is yours, as long as it serves the spec.

## Hard rules

- **No invented APIs.** Before calling any library function, confirm it in the official docs or the installed package source. If you are unsure a function exists, say so in the PR instead of guessing.
- **In gateway configurations (G1, G2), the agent holds no simulator credential,** and only the gateway writes to the simulator. Baselines (B0, B1, B2) write directly by design; that is what they measure (0008).
- **The gateway and the simulator never share a database transaction.** They connect with separate roles that cannot touch each other's tables (0008).
- **The evaluator never calls a model.** It decides pass or fail only from simulator and harness records, and reads the gateway's tables only to explain failures (0004).
- **Every state transition is one database transaction** with row locks, and is logged append-only in ActionEvent. No network call happens inside a database transaction (0003).
- **Determinism:** every FiveFold component, the gateway included, reads time only from the run's virtual clock and randomness only from the run's seeded generators (0009). Wall-clock time is recorded for metrics only. B1 allows Temporal's own clock for internal scheduling, retries and infrastructure timers (0018). FiveFold's clock governs every scored record. Lint bans wall-clock calls and global randomness outside the clock and generator modules (0007).
- **Held-out scenarios are off-limits.** They live outside this repo. Never search for, read or recreate them.
- **No live model calls in CI.** Live runs sit behind an explicit flag and a budget cap.
- **No secrets in the repo.** Keys come from environment variables.
- **Stay in scope.** The non-goals in `docs/product.md` are binding: no agent framework, no execution engine, no model router, no receipts or TEE in v0.1.

## Tests

- Every fault variant has deterministic tests for every applicable (contract, configuration) cell, asserting the verdict and failing properties in `faults/expected.yaml` (0006). For every fault except F1, F10 and F12, at least one cell shows B0 failing where G2 passes.
- Invariants I1 to I7 are asserted in code and exercised by Hypothesis rule-based state machine tests.
- A PR that changes behaviour includes a test that would have caught the old behaviour.

## Pull requests

- One ticket per PR, kept small. Keep one open PR per branch, and restart the branch from `main` after each merge. When a reviewer merges a sprint's PRs together at its end, stack them: each ticket's branch starts from the previous ticket's branch, and every PR targets `main`.
- The description names the spec section and decision records it implements, lists each library API it uses with where it was checked (a docs URL, or the installed source path and version), and states anything left uncertain.
- Dependency upgrades are separate PRs. An upgrade of LangGraph, langgraph-prebuilt or the MCP SDK re-checks the facts that 0008 and 0009 pin.
- Run the full deterministic suite before asking for review.

## Commits and attribution

- Treat this repository as public. Never commit secrets, personal names, personal email addresses, local paths or machine details. Allowed: the maintainer's GitHub username, `samrusani`; GitHub noreply addresses in commit metadata; the contact address in `CODE_OF_CONDUCT.md`, which the maintainer chose as the project's public contact; and links to other people's public work in citations.
- No tool attribution in commits, pull requests or docs: no "Generated with" footers, no AI co-author trailers and no session links.
- Agents working for the maintainer commit as `samrusani <samrusani@users.noreply.github.com>`. Other contributors use their own GitHub noreply address.
- Before every push, check each new commit's author and committer. CI's commit identity check (`scripts/check_identities.py`) fails a pull request whose commits use any address that is not a GitHub noreply address, or the maintainer's address under another name.

## Working wiki

- Agents keep working notes in `wiki/` at the repo root: status, roadmap, processes, learnings and a log. It is gitignored and never published.
- If `wiki/` exists, read `wiki/index.md` and `wiki/status.md` before starting, and follow `wiki/AGENTS.md`.
- Before finishing a session, update the pages you touched, `wiki/status.md` and `wiki/log.md`.
- Nothing moves from the wiki into the repository unless it passes the rules above.

## Writing style for docs and the report

- Short sentences, plain words, specific numbers with units.
- No em dashes. Use commas, semicolons, colons, parentheses or separate sentences.
- Avoid stock phrases such as "It's worth noting", "Notably", "In an era of", "The landscape of", "This underscores" and "a testament to".
- Cite papers by full title and arXiv ID, and label them as preprints.
