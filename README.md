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
- the gateway: the state machine with invariants I1 to I7, the PostgreSQL store, the dispatcher and the agent-facing tools over MCP, as G2 and as G1, authorization only (`src/fivefold/gateway/`);
- the agent: a LangGraph agent with a scripted model, for the gateway (G1 and G2) and the baseline (B0) (`src/fivefold/agents/`);
- the evaluator, which scores every run from ground truth on all five properties, and writes a CSV and a Markdown report across runs (`src/fivefold/evaluator/`).

All four demos of record 0006 run from a seed (see Demo below). The development scenario has 221 expected-outcome cells: faults F1 to F7 and F9 to F12, at each contract level (C0 to C3) and configuration (the baseline B0, and the gateway as G1 and G2) they apply to. F10 needs a status probe, so it applies at C1 in G2 only. Each cell runs from the committed seed and gets the verdict `faults/expected.yaml` expects, and every G2 cell passes except the limits record 0006 documents. Record 0006 fixes no G1 outcome, so G1's cells come from its first runs. The scripted approver decides approvals, and approvals can be revoked (F6) or expire. When a scenario names a resolver, a scripted human settles each escalated action through the gateway's admin path, after the settle time, and says truthfully whether it committed; G1 never escalates, so the resolver never acts there. CI runs each demo twice from its seed, in B0, G1 and G2, and compares every record by digest.

Milestone M3 is nearly done: faults F3 (a gateway crash after the reply arrives, before it is recorded), F4 (a request delivered twice), F5 (a commit after the caller's timeout) and F10 (status answers delayed or stale), demos 2 and 3, and recorded playback (see below) are in. Viewer v1 waits for record 0012, and the held-out scenarios are kept outside this repository. Two of F10's cells contradict record 0006; record 0013 proposes how to resolve them, and they wait for the maintainer.

Milestone M4, the live study, has started: configuration G1, the evaluator's CSV and Markdown report (see Results below) and the live model adapter (see A live episode with Ollama below) are in. The live study waits for the maintainer's choice of models, API keys and a spend cap.

## Demo

`make demo` runs the four demos of record 0006, each for the baseline B0 and the gateway G2 on the same seed. `uv run python -m fivefold.harness.demo 2a` runs one of them. Time is virtual, so each demo takes a few seconds. The demos need PostgreSQL but no API key.

1. **Order placed, reply lost** (C2). The purchase order's reply is lost (fault F2), and the agent is killed right after sending the order and resumed from its checkpoint 10 seconds later (F11). Without the gateway, the resumed agent orders again: two orders, one approval, an overdrawn budget. With the gateway, it retries with the same idempotency key while the agent is down, and the resumed agent's repeated proposal attaches to the confirmed order: one order. The evaluator scores both runs: B0 fails properties 1 (a duplicate order), 2 (the second order reuses the first approval) and 3 (the budget no longer covers it); G2 passes all five.
2. **Approval goes stale** (C2). A human approves the order; before it is sent, the quote's price goes up (2a, fault F7) or the approval is revoked (2b, F6). G2 refuses to send and names the stale fact (`quote/S-1/W-100`, version 1 to 2, `unit_price_minor`) or the revoked approval. In 2a the agent reads again and orders at the new price on a new approval. B0 orders anyway, on the old price or the revoked approval.
3. **Outcome unknown** (C0). The order's reply is lost, and the destination offers no way to ask. G2 tells the agent that the outcome is unknown and what a human must reconcile, the report says "unknown", and the verdict is pass_unresolved. B0 orders twice.

## Recorded playback

A run can record every model call into a cassette, and be played back from it into a fresh database (record 0009). The replay must ask the model exactly what the recording did, and its logs, effects ledger and evaluation must match the original run's byte for byte, apart from wall-clock times. Playback needs PostgreSQL but no API key.

```
uv run python -m fivefold.recorder record --demo 1 --config G2 --cassette demo-1-g2.json
uv run python -m fivefold.recorder play demo-1-g2.json --scenario scenarios/dev/dev-001-single-po.yaml
```

## A live episode with Ollama

Live runs are manual and never run in CI. A live run is refused unless the environment sets `FIVEFOLD_LIVE=1` and the command gets a spend cap in USD and a price file that prices the model (records 0004 and 0009). Ollama is the one provider so far. To run demo 1 in G2 with a local model:

1. Install [Ollama](https://ollama.com) and pull a model that can call tools: `ollama pull MODEL`.
2. Copy `prices/example.yaml` to `prices.yaml`, and add `MODEL` under `ollama:` at 0 USD, quoted, as the examples are.
3. Record the episode. `CAP` is the spend cap in USD; a local model costs nothing, so any cap above 0 lets it run. If the Ollama server is not at `http://127.0.0.1:11434`, set `OLLAMA_HOST`.

   ```
   FIVEFOLD_LIVE=1 uv run python -m fivefold.recorder record --demo 1 --config G2 \
       --cassette live-1-g2.json --live ollama:MODEL --spend-cap-usd CAP --prices prices.yaml
   ```

4. Play it back, with no network and no flag: `uv run python -m fivefold.recorder play live-1-g2.json --scenario scenarios/dev/dev-001-single-po.yaml`.

The record command prints the verdict, the cassette's digest and what the run spent. Each model call takes the scenario's `think_ms` of virtual time, however long the model really takes, and `harness.model_call` logs its tokens. `--param NAME=VALUE`, such as `--param temperature=0`, sets a model parameter; the cassette records it. Before each call, the spend so far is checked against the cap, and a refused call ends that agent worker as `budget`. A call the provider has not answered after `--call-limit-s` wall-clock seconds (default 600) fails, and its worker ends with an error, so a stalled server cannot hold the run.

## A study

`python -m fivefold.harness.study` runs a matrix of episodes, the runs of milestone M4, and saves each run's result in a directory for the report (see Results). An episode is a cell of `faults/expected.yaml`, or a cell with no fault, run with one seed. By default the study runs every expected cell of dev-001, in B0, G1 and G2 at C0 to C3, once each, with the scripted model. Each episode runs in a fresh database, dropped once it ends.

```
uv run python -m fivefold.harness.study --out study --cells none F2 --contracts C0 C2 --episodes 2
uv run python -m fivefold.evaluator report study --csv study.csv --markdown study.md
```

`--cells` takes `all`, `none` (no fault), a fault (`F2`), a fault and variant (`F11-after_tool_call`) or a cell (`F2-C2-G2`). `--episodes N` runs each cell with the seeds S to S+N-1, where `--seed S` defaults to 7. Episodes run in a fixed order, by seed and then by cell, and each result is saved as `CELL-seed-SEED.json`, such as `F2-C0-G2-seed-7.json`. The same inputs with the scripted model give the same bytes. The output directory must be new or empty.

A live study takes the options of a live episode (see above) and is refused without them, before it touches any database. It also saves each episode's cassette in `cassettes/`; `python -m fivefold.recorder play` compares a replay with the original run's database, so give `--keep-databases` to play one back. One spend cap holds across the study, and the study stops once the spend reaches it, since every later model call would be refused. An episode the cap cut short keeps its cassette, and its result goes to `spend-cap/`, which a report over the directory leaves out.

The pilot of 10 to 20 live episodes is the maintainer's to size (`docs/plan.md`, M4). For example, no fault and F2, at C2, in B0, G1 and G2, with three seeds each, is 18 episodes. `MODEL` and `CAP` are placeholders: the maintainer has chosen neither yet.

```
FIVEFOLD_LIVE=1 uv run python -m fivefold.harness.study --out pilot \
    --cells none F2 --contracts C2 --episodes 3 \
    --live ollama:MODEL --spend-cap-usd CAP --prices prices.yaml
uv run python -m fivefold.evaluator report pilot --csv pilot.csv --markdown pilot.md
```

The study prints each episode's verdict next to the one `faults/expected.yaml` expects for its cell, how each agent worker ended, and in a live study the episode's model calls and the spend so far.

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

## Results

The evaluator writes each run's result as JSON. `uv run python -m fivefold.harness.demo --out results` saves one file per demo run. `uv run python -m fivefold.evaluator report results --csv runs.csv --markdown report.md` turns them into a CSV with one row per run and a Markdown report: what was run, verdicts and safe and live runs by configuration and contract level, failures by property and verdicts by fault plan, each count over its denominator, and metric totals. The report needs no database, and the same results always give the same bytes.

## Maintainer

[samrusani](https://github.com/samrusani)

## License

Apache License 2.0. See [LICENSE](LICENSE).
