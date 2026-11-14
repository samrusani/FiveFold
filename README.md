# Fivefold

An open-source test suite and reference gateway that checks whether AI agents act correctly when things fail.

A task passes only if every real-world action:

1. happens exactly once,
2. was authorized at commit time,
3. used inputs that were still fresh,
4. was actually committed, and
5. was honestly reported, including "outcome unknown".

## Status

Milestone M0, the design freeze, is done: decision records 0001 to 0009 were accepted on 14 Nov 2026, and the scaffold, CI and Docker Compose work. Milestone M1 has started with the gateway's state machine (`src/fivefold/gateway/`): the transition tables, the decision steps, invariants I1 to I7 as checks, and a PostgreSQL store for the records, tested by Hypothesis against an in-memory oracle. Record 0010, accepted on 14 Nov 2026, corrects the state machine; its gateway changes are implemented, and its transport rule waits for the harness. Nothing here runs a benchmark yet.

## Documents

- [docs/product.md](docs/product.md): what and why, scope and non-goals
- [docs/spec.md](docs/spec.md): the contracts to build against
- [docs/plan.md](docs/plan.md): milestones, workstreams and first tickets
- [docs/adr/](docs/adr/README.md): decision records, with a suggested reading order
- [CONTRIBUTING.md](CONTRIBUTING.md): how to propose changes
- [CLAUDE.md](CLAUDE.md): rules for coding agents working in this repository

## Development

You need Python 3.12, [uv](https://docs.astral.sh/uv/) 0.13.8 or later, and PostgreSQL 16. Docker Compose can run the database for you.

```
make setup      # install the locked dependencies into .venv
make db-up      # start PostgreSQL 16 on 127.0.0.1:5432
make check      # lint, type check and every test
make test-fast  # only the tests that need no database
make help       # every target
```

Database tests connect with `FIVEFOLD_ADMIN_DSN`, which defaults to the Compose database. Any PostgreSQL 16 server works, as long as the connection is a superuser.

## Maintainer

[samrusani](https://github.com/samrusani)

## License

Apache License 2.0. See [LICENSE](LICENSE).
