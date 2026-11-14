# CLAUDE.md

Rules for every coding agent working in this repo.

## What this is

Fivefold (working name) is an open-source test suite and reference gateway. It checks whether AI agents act correctly under failure: every real-world action happens exactly once, with valid authority, on fresh inputs, is actually committed, and is honestly reported.

Read these before writing code, in this order:

1. `docs/product.md`: what and why, scope, non-goals
2. `docs/spec.md`: the contracts you build against
3. `docs/plan.md`: milestones, teams, tickets

## Who decides what

- **The maintainer owns the semantics:** action identity, grant lifecycle, states and transitions, invariants I1 to I6, the five property definitions, tool names and return shapes, tool contract levels, and fault IDs.
- To change any of those, write a decision record in `docs/adr/` with status **Proposed** and stop. Do not mark it Accepted yourself.
- Everything else (internal module structure, helper names, test layout) is yours, as long as it serves the spec.

## Hard rules

- **No invented APIs.** Before calling any library function, confirm it in the official docs or the installed package source. If you are unsure a function exists, say so in the PR instead of guessing.
- **The agent never holds write credentials.** Only the gateway writes to the simulator.
- **The evaluator never calls a model.** It reads ground truth from the effects ledger and the action store.
- **Every state transition is one database transaction** with row locks, and is logged append-only in ActionEvent.
- **Determinism:** the simulator, fault scheduler and evaluator use the virtual clock and the run seed. No wall-clock time and no unseeded randomness in those paths.
- **Held-out scenarios are off-limits.** They live outside this repo. Never search for, read or recreate them.
- **No live model calls in CI.** Live runs sit behind an explicit flag and a budget cap.
- **No secrets in the repo.** Keys come from environment variables.
- **Stay in scope.** The non-goals in `docs/product.md` are binding: no agent framework, no execution engine, no model router, no receipts or TEE in v0.1.

## Tests

- Every fault type gets a deterministic test that fails on baseline B0 and passes on gateway G2.
- Invariants I1 to I6 are asserted in code and exercised by Hypothesis rule-based state machine tests.
- A PR that changes behaviour includes a test that would have caught the old behaviour.

## Pull requests

- One ticket per PR, kept small.
- The description says which spec section it implements, what it verified in library docs, and anything left uncertain.
- Run the full deterministic suite before asking for review.

## Commits and attribution

- This repository is public. Never commit secrets, personal names, email addresses, local paths or machine details. The only personal identifier allowed is the maintainer's GitHub username, `samrusani`.
- No tool attribution in commits, pull requests or docs: no "Generated with" footers, no AI co-author trailers and no session links.
- Agents working for the maintainer commit as `samrusani <samrusani@users.noreply.github.com>`. Other contributors use their own GitHub noreply address.

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
