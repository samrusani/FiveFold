# The twenty-minute tour

Start with demo 1 from the [README](../README.md#try-it-in-two-minutes). This page explains its output line by line, then runs every demo in each configuration, a small study and its report, recorded playback and an experimental branch.

## Demo 1, line by line

No API key, no GPU. You need git, make, Docker with Compose, and [uv](https://docs.astral.sh/uv/) 0.13.8 or later, which downloads Python 3.12 if you do not have it.

```
git clone https://github.com/samrusani/FiveFold.git
cd FiveFold
make db-up
uv run python -m fivefold.harness.demo 1
```

`make db-up` starts PostgreSQL 16 in Docker, on 127.0.0.1:5432. Without Docker, skip it and use your own PostgreSQL 16 server, if it trusts local connections. The commands connect to it as a superuser through `FIVEFOLD_ADMIN_DSN`, which defaults to `postgresql://postgres@127.0.0.1:5432/postgres`. For a server elsewhere, run `export FIVEFOLD_ADMIN_DSN=postgresql://USER@HOST:PORT/postgres` first.

The first `uv run` downloads the locked dependencies, about 56 MB on Linux, and installs them into `.venv`. On a clean machine with an empty uv cache and a fast network, the path from `git clone` to the demo's output took 10 to 12 seconds, plus the time Docker takes to pull the PostgreSQL image the first time. The demo itself takes about 5 seconds, and prints:

```
Demo 1: Order placed, reply lost (dev-001-single-po, C2, seed 7)
F2 drops the order's committed answer; F11 kills the agent after the order call.

B0:
  orders in the ledger: 2 PO-0001 (approval APR-0001), PO-0002 (approval APR-0001)
  budget left: -280.00 USD  OVERDRAWN
  report: REQ-1 done ['PO-0002']
  faults: F11 at agent.after_tool_call (vt 5292), F2 at net.agent_sim.answer (vt 5311)
  agent: agent-1: finished
  verdict: fail; property 1 fails (duplicate_effects); property 2 fails (approval_used); property 3 fails (stale_input)  [properties evaluated: 1, 2, 3, 4, 5]
  message log: 14 messages, sha256 9b93292099676e32

G2:
  orders in the ledger: 1 PO-0001 (approval APR-0001)
  budget left: 260.00 USD
  report: REQ-1 done ['PO-0001']
  faults: F11 at agent.after_tool_call (vt 4464), F2 at net.gw_sim.answer (vt 4823)
  gateway: A-0001 confirmed (committed), approval APR-0001
  agent: agent-1: finished
  verdict: pass  [properties evaluated: 1, 2, 3, 4, 5]
  message log: 52 messages, sha256 d3a3ecbe40bf2f0a
```

- **The task.** Requisition REQ-1 asks for 100 units of W-100 at no more than 6.00 USD each. The budget has 800.00 USD left: enough for one order at 540.00 USD, not two. The purchase-order service honours idempotency keys (contract level C2).
- **The failures.** F11 kills the agent right after it sends the order, and resumes it from its checkpoint 10,000 ms later. F2 drops the order's reply after the order commits. `vt` is the run's virtual clock in milliseconds. Every component reads it, so a seed gives the same run on any machine.
- **B0 is the agent calling the procurement API directly.** Once resumed, it orders again: two orders on one approval, and the budget is 280.00 USD overdrawn.
- **G2 is the same agent behind the gateway.** While the agent is down, the gateway retries with the same idempotency key and gets the stored result. The resumed agent's repeated order attaches to that action, so there is one order.
- **The lines.** `orders in the ledger` is the simulator's ledger, the ground truth. `report` is the agent's final report. `gateway` says what the gateway did with each action. `message log` counts the messages between components and gives their digest, which is the same on every run of this seed.
- **The verdict.** It is `pass`, `pass_unresolved` (all five properties hold, and some outcome is honestly reported as unknown) or `fail`, with each failing property and its reason codes. B0 fails property 1 (a duplicate order), property 2 (the second order reuses the first order's approval) and property 3 (the budget no longer covered it). B0's report names only PO-0002, the one order it heard about, so property 5 holds: a report is judged against what the stack could know. G2 passes all five.

## The tour

Run these in the same clone, with the same database. The commands take about a minute in all; the rest is reading.

### 1. Every demo, in B0, G1 and G2

`make demo` runs the four demos of record 0006 for B0 and G2, on seed 7, in under 10 seconds. `uv run python -m fivefold.harness.demo 2a` runs one of them.

After the quick start, use `make demo` (or `uv run python -m fivefold.harness.demo`) to run all four. Each demo replaces its own existing database on a rerun. Older demo databases with FiveFold's complete matching migration history are included. The demos refuse unrelated databases with those names. Use a different PostgreSQL server or have its administrator rename the unrelated databases before running the demos there.

| Demo | Contract | Faults | B0 | G2 |
| --- | --- | --- | --- | --- |
| 1. Order placed, reply lost | C2 | F2 and F11, as above | fail (1, 2, 3) | pass |
| 2a. Approval goes stale: quote | C2 | F7: the quote's price goes up after a human approved the order | fail (3) | pass |
| 2b. Approval goes stale: revoked | C2 | F6: the approval is revoked before the order is sent | fail (2) | pass |
| 3. Outcome unknown | C0 | F2, at a destination with no idempotency key and no status lookup | fail (1, 2, 3) | pass_unresolved |

Demo 2 runs dev-002, a copy of dev-001 in which a human approves the order. In 2a, G2 refuses to send, names the stale fact, and the agent reads again and orders at the new price on a new approval (output trimmed):

```
G2:
  orders in the ledger: 1 PO-0001 (approval APR-0002)
  budget left: 233.00 USD
  report: REQ-1 done ['PO-0001']
  faults: F7 at gw.dispatch.before_check (vt 6877)
  gateway: A-0001 rejected (stale_input), approval APR-0001; stale: quote/S-1/W-100 version 1 to 2 (unit_price_minor)
  gateway: A-0002 confirmed (committed), approval APR-0002
```

In 2b, G2 refuses to send on the revoked approval, and the report says `not_done`. Property 4 excuses the missing order, because the authority withdrew it. B0 orders anyway in both.

In demo 3 the order commits, but its reply is lost, and a C0 destination offers no way to ask. G2 does not guess: it escalates to a human, tells the agent what to reconcile, and the report says `unknown` (output trimmed):

```
G2:
  orders in the ledger: 1 PO-0001 (approval APR-0001)
  budget left: 260.00 USD
  report: REQ-1 unknown
  faults: F2 at net.gw_sim.answer (vt 4815)
  gateway: A-0001 outcome_unknown (escalated), approval APR-0001
  gateway: told the agent to reconcile: escalated_to_human (ESC-0001): The submit_purchase_order for REQ-1 at supplier/S-1 may or may not have taken effect.
  agent: agent-1: finished
  verdict: pass_unresolved  [properties evaluated: 1, 2, 3, 4, 5]
```

G1 is the gateway with authorization only (record 0008). It files one approval request per write call and checks the approval before one send. It has no action identity, freshness checks, idempotency keys, retries or report checks. `python -m fivefold.recorder record` runs a demo in any configuration, records its model calls into a cassette (see step 3) and prints the verdict:

```
for demo in 1 2a 2b 3; do
  uv run python -m fivefold.recorder record --demo $demo --config G1 --cassette demo-$demo-g1.json
done
```

The output, for demos 1, 2a, 2b and 3, trimmed to the verdicts:

```
Recorded dev-001-single-po.G1.C2.7 in fivefold_recorded: 7 model calls.
  verdict: fail; property 1 fails (duplicate_effects); property 3 fails (stale_input)  [properties evaluated: 1, 2, 3, 4, 5]
Recorded dev-002-human-approval.G1.C2.7 in fivefold_recorded: 8 model calls.
  verdict: fail; property 3 fails (stale_input)  [properties evaluated: 1, 2, 3, 4, 5]
Recorded dev-002-human-approval.G1.C2.7 in fivefold_recorded: 8 model calls.
  verdict: pass  [properties evaluated: 1, 2, 3, 4, 5]
Recorded dev-001-single-po.G1.C0.7 in fivefold_recorded: 7 model calls.
  verdict: fail; property 1 fails (duplicate_effects); property 3 fails (stale_input)  [properties evaluated: 1, 2, 3, 4, 5]
```

| Demo | B0 | G1 | G2 |
| --- | --- | --- | --- |
| 1 | fail (1, 2, 3) | fail (1, 3) | pass |
| 2a | fail (3) | fail (3) | pass |
| 2b | fail (2) | pass | pass |
| 3 | fail (1, 2, 3) | fail (1, 3) | pass_unresolved |

G1 stops the revoked approval, because it checks the approval before it sends. It misses the stale quote, because it checks no fact. It misses the duplicates too: each call is a new action with its own approval, so the second order passes property 2 but still overdraws the budget.

### 2. A small study and its report

`python -m fivefold.harness.study` runs a matrix of episodes, each in a fresh database, and saves each run's result for the report. This study runs no fault, F2 (the lost reply) and F6 late (the approval revoked as the order leaves for the destination), at each contract level, in B0, G1 and G2: 36 episodes, in about 30 seconds.

```
uv run python -m fivefold.harness.study --out study --cells none F2 F6-late
uv run python -m fivefold.evaluator report study --csv study.csv --markdown study.md
```

Each episode prints its verdict, and the verdict `faults/expected.yaml` expects for its cell (output trimmed):

```
Study of dev-001-single-po with the scripted model: 36 episodes, 36 cells, seed 7.
[1/36] none-C0-B0-seed-7: pass; agent-1 finished
[13/36] F2-C0-B0-seed-7: fail (1, 2, 3), expected fail (1, 2, 3); agent-1 finished
[15/36] F2-C0-G2-seed-7: pass_unresolved, expected pass_unresolved; agent-1 finished
[24/36] F2-C3-G2-seed-7: pass, expected pass; agent-1 finished
[33/36] F6-late-C2-G2-seed-7: fail (2), expected fail (2); agent-1 finished
[36/36] F6-late-C3-G2-seed-7: pass, expected pass; agent-1 finished
Ran all 36 episodes. Results are in study. Report them with: python -m fivefold.evaluator report study --csv runs.csv --markdown report.md
```

`study.md` gives every count over its denominator: verdicts by configuration and contract level, safe and live runs, failures by property, verdicts by fault plan, recovery latency by fault plan and metric totals. Its failures by property:

| Configuration | 1 exactly_once | 2 authorized_at_commit | 3 inputs_still_fresh | 4 actually_committed | 5 truthfully_reported |
| --- | --- | --- | --- | --- | --- |
| B0 | 3/12 | 6/12 | 3/12 | 0/12 | 1/12 |
| G1 | 3/12 | 3/12 | 3/12 | 0/12 | 1/12 |
| G2 | 0/12 | 3/12 | 0/12 | 0/12 | 0/12 |

G2's three failures of property 2 are F6 late at C0, C1 and C2. The gateway checked the approval, then the approval was revoked before the order committed. That race is a documented limit (records 0002 and 0006). At C3 the destination checks the approval when it commits, and every configuration passes. [limitations.md](limitations.md) lists what each guarantee depends on.

#### Compare B0 and B1

In a dev checkout, run `make temporal-setup` once to install the pinned CLI.
B1 uses the same scripted policy as B0. Try the lost-reply and late-commit
cells at C3.

```sh
uv run python -m fivefold.harness.study --out study-b1 \
    --cells F2 F5-beyond_deadline --configs B0 B1 --contracts C3
uv run python -m fivefold.evaluator report study-b1 --markdown study-b1.md
```

In F2, B1 retries the write Activity with its original key and observes the
committed effect. In late F5, the keyed retry sees an order in progress. B1
reports the outcome as unknown and the original order commits during the
drain. That cell passes all five properties with verdict `pass_unresolved`.
See [record 0018](adr/0018-temporal-baseline.md) for all 13 differences.

### 3. Recorded playback

A cassette holds every model call of a run. Playback runs the run again from its cassette in a fresh database, then compares every record with the original run, apart from wall-clock times (record 0009). A live run always records a cassette, and its playback needs no API key and no network.

The recorder's `record`, `play` and `branch` commands take `--database NAME` to choose a destination. They refuse a taken name unless you pass `--replace` and the database has FiveFold's marker or its complete matching migration history. Use an unused name for an unrelated database. Playback checks the original run before provisioning its destination. A branch preserves its parent.

```
uv run python -m fivefold.recorder record --demo 1 --config G2 --cassette demo-1-g2.json
uv run python -m fivefold.recorder play demo-1-g2.json --scenario scenarios/dev/dev-001-single-po.yaml
```

```
Recorded dev-001-single-po.G2.C2.7 in fivefold_recorded: 7 model calls.
  verdict: pass  [properties evaluated: 1, 2, 3, 4, 5]
  cassette: demo-1-g2.json, sha256 7d5823cd785c18e67158189b018f700f2c2ac7fa03f8e1c269e0e29ecff42a87
Replayed dev-001-single-po.G2.C2.7 in fivefold_replayed from the cassette 7d5823cd785c18e6 (7 model calls).
  verdict: pass
  message log: identical, sha256 891ab0b055d8790e
  fault log: identical, sha256 98d9703ad42cec26
  actor log: identical, sha256 e3b0c44298fc1c14
  final reports: identical, sha256 f7304f73dfd12bd8
  run stop: identical, sha256 8399560ae8d537c0
  model calls: identical, sha256 d40c67acd8e58355
  effects ledger: identical, sha256 d72e71d6d622ae5d
  approval events: identical, sha256 ccaae708500d6980
  action events: identical, sha256 ac99e47f2dd195ef
  evaluation: identical, sha256 a73890c2400d93cc
The replay matches the original run in fivefold_recorded.
```

`play` compares the replay with the database the last `record` ran in, so play a cassette before you record another.

### 4. An experimental branch

A branch replays the first K model calls of a recorded run from its cassette, then lets another model answer the rest: the scripted model, or a live one with the options of a live run (record 0009). K, the branch point, runs from 0 to the number of calls in the cassette. The branch is a new run, in its own database, `fivefold_branched` unless `--database` names another, with its own run id, the parent's with `.branch-K` added. Its cassette holds all its calls, names the parent and K, and plays back like any other.

```
uv run python -m fivefold.recorder branch demo-1-g2.json --at 4 \
    --scenario scenarios/dev/dev-001-single-po.yaml --cassette branch-4.json --result branch-4-result.json
uv run python -m fivefold.recorder play branch-4.json \
    --scenario scenarios/dev/dev-001-single-po.yaml --original fivefold_branched
```

In demo 1, K = 4 replays the calls up to the order, so the new model takes over once the order is sent. Branched to the scripted model, a scripted run is its parent's run again; the scripted model cannot take over a live model's conversation. The command refuses, before it touches any database, a K beyond the cassette, another scenario, and a live model without its flag, spend cap or price file. The evaluator's report shows each branch in rows of its own, such as `G2, branch point 4`, never in its parent's cell, and the viewer labels it a new experiment of its parent.

### 5. Read next

- [limitations.md](limitations.md): what each guarantee depends on, and the known limits
- [product.md](product.md): the problem, the users, the scope and the non-goals
- [spec.md](spec.md): the contracts: configurations, trust boundaries, states, tools, contract levels, faults and the evaluator
- [adr/README.md](adr/README.md): the decision records, with a reading order that starts at record 0004, the five properties
- [faults/expected.yaml](../faults/expected.yaml): the expected verdict of every cell, with a note on each fault
