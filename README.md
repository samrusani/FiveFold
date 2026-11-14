# Fivefold

An open-source test suite and reference gateway that checks whether AI agents act correctly when things fail.

A task passes only if every real-world action:

1. happens exactly once,
2. was authorized at commit time,
3. used inputs that were still fresh,
4. was actually committed, and
5. was honestly reported, including "outcome unknown".

## Status

Design phase. Decision records 0001 to 0009 were accepted on 14 Nov 2026, and the spec reflects them. The next step is the repository scaffold. There is no code yet, and nothing here is usable.

## Documents

- [docs/product.md](docs/product.md): what and why, scope and non-goals
- [docs/spec.md](docs/spec.md): the contracts to build against
- [docs/plan.md](docs/plan.md): milestones, workstreams and first tickets
- [docs/adr/](docs/adr/README.md): decision records, with a suggested reading order
- [CONTRIBUTING.md](CONTRIBUTING.md): how to propose changes
- [CLAUDE.md](CLAUDE.md): rules for coding agents working in this repository

## Maintainer

[samrusani](https://github.com/samrusani)

## License

Apache License 2.0. See [LICENSE](LICENSE).
