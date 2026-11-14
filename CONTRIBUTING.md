# Contributing

The project is in its design phase. Most contributions today are reviews of the design documents: open an issue, or comment on a pull request.

## How decisions are made

- The contracts live in `docs/spec.md`. The maintainer owns their meaning: action identity, the grant lifecycle, states and transitions, invariants, the five property definitions, tool names and return shapes, contract levels and fault IDs.
- To change any of these, open a pull request that adds a decision record in `docs/adr/`. Copy `0000-template.md`, set the status to **Proposed**, state the rule so that a test could check it, and list the alternatives you considered.
- Only the maintainer marks a record **Accepted**. The pull request that accepts a record also updates every document it affects.

## Pull requests

- One change per pull request, kept small.
- Name the spec section or decision record it implements.
- List every library API you used and where you checked it: the official docs, or the installed package source with its version. If you are unsure an API exists, say so instead of guessing.
- Say what is still uncertain.
- A behaviour change includes a test that would have caught the old behaviour.
- Live model calls never run in CI.

## Writing style

Short sentences, plain words, specific numbers with units. No em dashes. Cite papers by full title and arXiv ID, and label them as preprints. The full rules are in [CLAUDE.md](CLAUDE.md).

## What never goes in the repository

- Secrets. Keys come from environment variables.
- Personal names, email addresses, local paths and machine details.
- Held-out evaluation scenarios. They live outside this repository.
