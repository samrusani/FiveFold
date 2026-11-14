# Fivefold (working name)

An open-source test suite and reference gateway that checks whether AI agents act correctly when things fail.

A task passes only if every real-world action:

1. happens exactly once,
2. was authorized at commit time,
3. used inputs that were still fresh,
4. was actually committed, and
5. was honestly reported, including "outcome unknown".

## Status

Design phase, milestone M0 (design freeze). There is no code yet, and nothing here is usable.

Decision records 0001 to 0009 propose the design details. They stay Proposed until the maintainer accepts them. Until then, `docs/spec.md` is the reference, and each record lists how it would change the spec.

## Documents

- [docs/product.md](docs/product.md): what and why, scope and non-goals
- [docs/spec.md](docs/spec.md): the contracts to build against
- [docs/plan.md](docs/plan.md): milestones, workstreams and first tickets
- [docs/adr/](docs/adr/README.md): decision records, with a suggested review order
- [CONTRIBUTING.md](CONTRIBUTING.md): how to propose changes
- [CLAUDE.md](CLAUDE.md): rules for coding agents working in this repository

## Maintainer

[samrusani](https://github.com/samrusani)

## License

To be decided: Apache-2.0 or MIT.
