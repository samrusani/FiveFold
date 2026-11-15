# Contributing

Fivefold is working toward its first release, v0.1. The Status section of [README.md](README.md) says what is built and what is left. To report a bug or question a design, open an issue; to review a change, comment on its pull request. To report a vulnerability, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## How decisions are made

- The contracts live in `docs/spec.md`. The maintainer owns their meaning: action identity, the grant lifecycle, states and transitions, invariants, the five property definitions, tool names and return shapes, contract levels and fault IDs.
- To change any of these, open a pull request that adds a decision record in `docs/adr/`. Copy `0000-template.md`, set the status to **Proposed**, state the rule so that a test could check it, and list the alternatives you considered.
- Only the maintainer marks a record **Accepted**. Accepted records are binding, like the spec. The pull request that accepts a record also updates every document it affects.

## Pull requests

- Run `make check` before asking for review. The Development section of README.md explains the setup.
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
- Personal names, personal email addresses, local paths and machine details. GitHub noreply addresses in commit metadata, and links to other people's public work in citations, are fine.
- Held-out evaluation scenarios. They live outside this repository.

## License

Fivefold is licensed under the Apache License 2.0. By contributing, you agree that your contributions are licensed under the same terms.
