# Contributing

FiveFold is working toward its first release, v0.1. The Status section of [README.md](README.md) says what is built and what is left. To report a bug or question a design, open an issue; to review a change, comment on its pull request. To report a vulnerability, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

Follow the [Code of Conduct](CODE_OF_CONDUCT.md) when taking part in the community.

## How decisions are made

- The contracts live in `docs/spec.md`. The maintainer owns their meaning: action identity, the grant lifecycle, states and transitions, invariants, the five property definitions, tool names and return shapes, contract levels and fault IDs.
- To change any of these, open a pull request that adds a decision record in `docs/adr/`. Copy `0000-template.md`, set the status to **Proposed**, state the rule so that a test could check it, and list the alternatives you considered.
- Only the maintainer marks a record **Accepted**. Accepted records are binding, like the spec. The pull request that accepts a record also updates every document it affects.

## Development

You need Python 3.12, [uv](https://docs.astral.sh/uv/) 0.13.8 or later, and PostgreSQL 16. Put `uv` on your path. Two packaging tests run `uv build` and need network access for build dependencies. Docker Compose can run the database for you. The viewer's frontend also needs Node 26 and npm; the Python tests never do.

```
make setup      # install the locked dependencies into .venv
make db-up      # start PostgreSQL 16 on 127.0.0.1:5432
make check      # lint, type check and every test
make test-fast  # only the tests that need no database
make demo       # the four demos, for B0 and G2, from a seed
make help       # every target
```

CI also builds the wheel, installs it outside the checkout and runs demo 1 against PostgreSQL 16.

The demos, the tests and every command that runs an episode connect with `FIVEFOLD_ADMIN_DSN`, which defaults to the Compose database, `postgresql://postgres@127.0.0.1:5432/postgres`. Any PostgreSQL 16 server works if that connection is a superuser and the component roles can log in. `db/bootstrap.sql` creates those roles without a password; the Compose database and CI trust local connections, so the roles need none there. Test runs that share one server at the same time need different `FIVEFOLD_TEST_DB_PREFIX` values.

Every run database is a copy of one migrated template database, `fivefold_template` (set `FIVEFOLD_TEMPLATE_DB` for another; the tests use their own). The demo, the study, the recorder and the split topology's `start` make the template if it is missing, migrate it forward if it is behind, and copy it; commands that copy at once take turns. PostgreSQL copies a database only while no other session is connected to it, so keep no session open on the template: a copy waits up to 5 seconds for one to end, then is refused. A template whose applied migrations no longer match `db/migrations`, such as one an edited migration was applied to, is refused too; drop it, and the next run makes it again.

## Pull requests

- Run `make check` before asking for review. The Development section below explains the setup.
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
