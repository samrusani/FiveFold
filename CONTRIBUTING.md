# Contributing

FiveFold is working toward its first release, v0.1. The Status section of [README.md](README.md) says what is built and what is left. To report a bug or question a design, open an issue; to review a change, comment on its pull request. To report a vulnerability, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

Follow the [Code of Conduct](CODE_OF_CONDUCT.md) when taking part in the community.

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

## Releases

Releases go to PyPI through `.github/workflows/publish.yml`, with PyPI's Trusted Publishing, so no token is stored anywhere. To release:

1. In a pull request, set or confirm the version in `pyproject.toml`, such as `0.1.0a1`.
2. Once it merges, publish a GitHub release whose tag is `v` plus that version, such as `v0.1.0`.
3. The workflow checks the tag against the version, builds the sdist and the wheel, installs the wheel outside the repository and runs demo 1 from it. Only then does the publish job run, in the GitHub environment `pypi`, whose protection rules apply first.

## Writing style

Short sentences, plain words, specific numbers with units. No em dashes. Cite papers by full title and arXiv ID, and label them as preprints. The full rules are in [CLAUDE.md](CLAUDE.md).

## What never goes in the repository

- Secrets. Keys come from environment variables.
- Personal names, personal email addresses, local paths and machine details. GitHub noreply addresses in commit metadata, and links to other people's public work in citations, are fine.
- Held-out evaluation scenarios. They live outside this repository.

CI checks that every commit in a pull request uses a GitHub noreply address as author and committer.

## License

FiveFold is licensed under the Apache License 2.0. By contributing, you agree that your contributions are licensed under the same terms.
