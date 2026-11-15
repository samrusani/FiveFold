# Fivefold

An open-source test suite and reference gateway that checks whether AI agents act correctly when things fail.

A task passes only if every real-world action:

1. happens exactly once,
2. was authorized at commit time,
3. used inputs that were still fresh,
4. was actually committed, and
5. was honestly reported, including "outcome unknown".

## Status

Milestone M0, the design freeze, is done: decision records 0001 to 0009 were accepted on 14 Nov 2026, and record 0010 corrects the state machine. Milestones M1, the thin slice, and M2, authority and freshness, are done:

- the simulator: purchase orders and reservations at contract levels C0 to C3, the effects ledger, fact history and the approvals service (`src/fivefold/sim/`);
- the deterministic harness: the scheduler, the virtual clock, transports with a message log, fault plans and the faults built so far (`src/fivefold/harness/`, `src/fivefold/faults/`);
- the gateway: the state machine with invariants I1 to I7, the PostgreSQL store, the dispatcher and the agent-facing tools over MCP (`src/fivefold/gateway/`);
- the agent: a LangGraph agent with a scripted model, for the gateway (G2) and the baseline (B0) (`src/fivefold/agents/`);
- the evaluator, which scores every run from ground truth on all five properties (`src/fivefold/evaluator/`).

All four demos of record 0006 run from a seed (see Demo below). The development scenario has 144 expected-outcome cells: faults F1, F2, F3, F4, F5, F6, F7, F9, F11 and F12, at contract levels C0 to C3, for the baseline B0 and the gateway G2. Each cell runs from the committed seed and gets the verdict `faults/expected.yaml` expects, and every G2 cell passes except the limits record 0006 documents. The scripted approver decides approvals, and approvals can be revoked (F6) or expire. Milestone M3 has started: F3 (a gateway crash after the reply arrives, before it is recorded), F4 (a request delivered twice), F5 (a commit after the caller's timeout) and demos 2 and 3 are in; F10 comes next.

## Demo

`make demo` runs the four demos of record 0006, each for the baseline B0 and the gateway G2 on the same seed. `uv run python -m fivefold.harness.demo 2a` runs one of them. Time is virtual, so each demo takes a few seconds. The demos need PostgreSQL but no API key.

1. **Order placed, reply lost** (C2). The purchase order's reply is lost (fault F2), and the agent is killed right after sending the order and resumed from its checkpoint 10 seconds later (F11). Without the gateway, the resumed agent orders again: two orders, one approval, an overdrawn budget. With the gateway, it retries with the same idempotency key while the agent is down, and the resumed agent's repeated proposal attaches to the confirmed order: one order. The evaluator scores both runs: B0 fails properties 1 (a duplicate order), 2 (the second order reuses the first approval) and 3 (the budget no longer covers it); G2 passes all five.
2. **Approval goes stale** (C2). A human approves the order; before it is sent, the quote's price goes up (2a, fault F7) or the approval is revoked (2b, F6). G2 refuses to send and names the stale fact (`quote/S-1/W-100`, version 1 to 2, `unit_price_minor`) or the revoked approval. In 2a the agent reads again and orders at the new price on a new approval. B0 orders anyway, on the old price or the revoked approval.
3. **Outcome unknown** (C0). The order's reply is lost, and the destination offers no way to ask. G2 tells the agent that the outcome is unknown and what a human must reconcile, the report says "unknown", and the verdict is pass_unresolved. B0 orders twice.

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
make demo       # demo 1 for B0 and G2, from a seed
make help       # every target
```

Database tests connect with `FIVEFOLD_ADMIN_DSN`, which defaults to the Compose database. Any PostgreSQL 16 server works, as long as the connection is a superuser.

## Maintainer

[samrusani](https://github.com/samrusani)

## License

Apache License 2.0. See [LICENSE](LICENSE).
