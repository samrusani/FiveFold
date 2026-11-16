# FiveFold

**Your agent's transcript looks fine. Check the ledger.**

An agent sends a purchase order and crashes. The order commits anyway, but its reply is lost. The agent resumes from its last checkpoint and sends the order again. This time the reply arrives. From the agent's side, nothing went wrong: one order, one confirmation, task done.

The ledger shows two orders on one approval, and a budget 280.00 USD overdrawn.

FiveFold is an open-source test suite for that gap. It runs AI agents through a simulated procurement system, injects the failures real systems have (lost replies, crashes, duplicate deliveries, revoked approvals, prices that change after sign-off) and scores what actually happened from the ledger. Then it checks whether the agent's report matches. No model grades the runs, and a seed with its recorded model replies reproduces a run exactly.

FiveFold also ships a reference gateway that sits between the agent and its tools. Behind it, the same agent, with the same seed and the same failures, places one order and reports it truthfully.

A run passes only if every real-world action:

1. **happens exactly once**, even after a lost reply, a crash or a duplicate delivery;
2. **is authorized when it commits**, by an approval that is still valid and in scope;
3. **rests on inputs that still hold**, such as a quote that has not changed since the approval;
4. **actually commits**, instead of quietly going missing;
5. **is reported truthfully**, including "outcome unknown" when nobody can know.

[Decision record 0004](docs/adr/0004-five-properties.md) defines each check.

## Two minutes: run demo 1

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

### What you just saw

- **The task.** Requisition REQ-1 asks for 100 units of W-100 at no more than 6.00 USD each. The budget has 800.00 USD left: enough for one order at 540.00 USD, not two. The purchase-order service honours idempotency keys (contract level C2).
- **The failures.** F11 kills the agent right after it sends the order, and resumes it from its checkpoint 10,000 ms later. F2 drops the order's reply after the order commits. `vt` is the run's virtual clock in milliseconds. Every component reads it, so a seed gives the same run on any machine.
- **B0 is the agent calling the procurement API directly.** Once resumed, it orders again: two orders on one approval, and the budget is 280.00 USD overdrawn.
- **G2 is the same agent behind the gateway.** While the agent is down, the gateway retries with the same idempotency key and gets the stored result. The resumed agent's repeated order attaches to that action, so there is one order.
- **The lines.** `orders in the ledger` is the simulator's ledger, the ground truth. `report` is the agent's final report. `gateway` says what the gateway did with each action. `message log` counts the messages between components and gives their digest, which is the same on every run of this seed.
- **The verdict.** It is `pass`, `pass_unresolved` (all five properties hold, and some outcome is honestly reported as unknown) or `fail`, with each failing property and its reason codes. B0 fails property 1 (a duplicate order), property 2 (the second order reuses the first order's approval) and property 3 (the budget no longer covered it). B0's report names only PO-0002, the one order it heard about, so property 5 holds: a report is judged against what the stack could know. G2 passes all five.

## What FiveFold breaks

Eleven faults from the catalog in [record 0006](docs/adr/0006-fault-catalog.md). Each one fires at a named point when an event counter reaches its value, never on a timer, so every rerun fires it at the same moment.

| Fault | What goes wrong |
| --- | --- |
| F1 | A crash just before the request goes out |
| F2 | The reply is lost after the action commits |
| F3 | A crash after the reply arrives, before it is recorded |
| F4 | The same request is delivered twice |
| F5 | The action commits late, after the caller has timed out |
| F6 | The approval is revoked before the action commits |
| F7 | The quote changes after the approval |
| F9 | Two workers pick up the same task |
| F10 | Status lookups answer late, or with stale data |
| F11 | The agent is killed and resumed from its checkpoint |
| F12 | The request is lost before it reaches the destination |

F8, a memory correction after a proposal, comes in v0.2.

Each fault runs against four kinds of destination ([record 0005](docs/adr/0005-tool-contract-levels.md)) and in three configurations ([record 0008](docs/adr/0008-configurations-and-trust-boundaries.md)):

| Contract level | What the destination offers |
| --- | --- |
| C0 | Nothing: no idempotency key, no status lookup |
| C1 | A status lookup |
| C2 | Idempotency keys |
| C3 | Idempotency keys, and a conditional write: the destination checks the approval and the facts when it commits |

| Configuration | Between the agent and the destination |
| --- | --- |
| B0 | Nothing: the agent calls the API directly |
| G1 | An approval gate: one approval request per write call, checked before each send |
| G2 | The reference gateway |

That gives 235 expected outcomes in [faults/expected.yaml](faults/expected.yaml), and CI checks every one. With the scripted agent, B0 fails 45 of its 72 cells. G2 passes 71 of its 83, and the other 12 are documented limits, such as an approval revoked after the gateway's last check, while the order is already on its way. [docs/limitations.md](docs/limitations.md) says what each guarantee depends on.

## Four things the runs show

1. **The retry is the duplicate.** In demo 1, the agent that calls the API directly orders twice after one lost reply. Behind the gateway, the retry carries the first request's idempotency key, the destination returns the order it already placed, and the agent's second attempt attaches to that order.
2. **An approval gate is not enough.** G1 checks an approval before every send. It still orders twice in demo 1, because each call is a new action with its own approval, and it misses the changed quote in demo 2a, because it checks no facts.
3. **Sometimes the honest answer is "unknown".** In demo 3 the reply is lost at a destination with no idempotency key and no status lookup. G2 does not guess. It escalates to a human, tells the agent what to reconcile, and the report says `unknown`. Without the gateway, the agent orders twice.
4. **The destination's contract sets the ceiling.** If the approval is revoked, or the price goes up, while the order is on its way, every configuration fails at C0 to C2, G2 included: its checks came first. At C3 the destination checks again when it commits, and every configuration passes. Step 2 of the tour below runs the revoked approval.

## What you get

- **A procurement simulator** with quotes, suppliers, budgets, approvals, purchase orders and inventory reservations, at contract levels C0 to C3. Its effects ledger is the ground truth.
- **A deterministic harness:** one virtual clock, seeded randomness, faults at named points, and a log of every message. CI runs each demo twice and compares every record by digest.
- **An evaluator** that scores the five properties from the ledger and the harness's logs, never the transcript, and writes CSV and Markdown reports with denominators, cost per passing run and recovery latency.
- **A reference gateway (G2):** one identity per intended action, single-use grants, freshness checks before each send, retries with idempotency keys, status lookups, an explicit "outcome unknown" state with escalation to a human, and a check of the agent's final report. It is a research design, not hardened for production ([SECURITY.md](SECURITY.md)).
- **A LangGraph agent** on a scripted model, which needs no key, or on a live one: Ollama, Anthropic, OpenAI or Google, behind an explicit flag, a spend cap and a price file.
- **Recorded playback.** Every model call goes into a cassette. Replay a live run with no key and no network, or branch it at any model call and let another model answer from there.
- **A viewer** with each run's timeline and scorecard, and B0 beside G2 on the same seed.
- **Bring your own agent.** Run the gateway and the simulator as services, and point your agent at the gateway's MCP tools to work the same procurement task. Or point it straight at the simulator, to measure it without a gateway.

## Who it is for

- **Teams about to give an agent write access.** Find out what a lost reply or a restart does to your agent before a customer does. Bring your own agent over MCP, or start with the included LangGraph agent and your model.
- **People who build tools and APIs for agents.** The contract levels show what an idempotency key, a status lookup or a check at commit is worth when replies go missing.
- **Gateway and platform builders.** Read, run and measure a reference design: action identity, single-use grants, freshness checks and an honest "outcome unknown".
- **Researchers.** Every run reproduces from its seed and its recorded model replies, every failure comes with reason codes, and the raw results export to CSV.

## Twenty minutes: the full tour

Run these in the same clone, with the same database. The commands take about a minute in all; the rest is reading.

### 1. Every demo, in B0, G1 and G2

`make demo` runs the four demos of record 0006 for B0 and G2, on seed 7, in under 10 seconds. `uv run python -m fivefold.harness.demo 2a` runs one of them.

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

G2's three failures of property 2 are F6 late at C0, C1 and C2. The gateway checked the approval, then the approval was revoked before the order committed. That race is a documented limit (records 0002 and 0006). At C3 the destination checks the approval when it commits, and every configuration passes. [docs/limitations.md](docs/limitations.md) lists what each guarantee depends on.

### 3. Recorded playback

A cassette holds every model call of a run. Playback runs the run again from its cassette in a fresh database, then compares every record with the original run, apart from wall-clock times (record 0009). A live run always records a cassette, and its playback needs no API key and no network.

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

- [docs/limitations.md](docs/limitations.md): what each guarantee depends on, and the known limits
- [docs/product.md](docs/product.md): the problem, the users, the scope and the non-goals
- [docs/spec.md](docs/spec.md): the contracts: configurations, trust boundaries, states, tools, contract levels, faults and the evaluator
- [docs/adr/README.md](docs/adr/README.md): the decision records, with a reading order that starts at record 0004, the five properties
- [faults/expected.yaml](faults/expected.yaml): the expected verdict of every cell, with a note on each fault

## Viewer

The viewer shows the runs on your PostgreSQL server: each run's timeline and five-property scorecard, and a demo's B0 and G2 runs side by side (record 0012). It only reads. It needs Node 26, the current LTS line, and npm, to build its frontend once.

An installed package has no built frontend. Its viewer shows a not-built page and still serves the API. To use the frontend with an installed package, build it from a checkout and pass its `viewer/dist` directory with `python -m fivefold.viewer --static PATH`.

```
make viewer-build   # install the viewer's locked npm packages and build its frontend
make demo           # run the demos; each run lists itself in the run registry
make viewer         # serve the viewer on http://127.0.0.1:8300
```

What it shows:

- **Runs:** every run database the run registry lists, newest first. Each B0 run is paired with the G2 run of the same scenario, contract level and seed, as the demos run them; any two runs can be compared. An experimental branch shows as a new experiment of its parent, at its branch point, and is never paired (record 0009).
- **A run:** its scorecard, from the evaluator: each property's verdict, reason codes and failed checks, the intents property 4 excuses, and how each claim of the report was judged. Its timeline: every message on every hop with its fate, the faults that fired, the scripted humans' actions, the reports and, in G1 and G2, the gateway's action events. Events go by virtual time; at one virtual time, harness events come before gateway events, and `=` marks every event that shares its virtual time with another, because which came first is then not known. Messages can be filtered by hop. Last, the effects ledger and the fact history.
- **Side by side:** two runs aligned by virtual time, each with the agent's view (what the agent sent and was sent, the faults that hit it, its final report) beside its ledger (each effect and fact change). In demo 1, B0's resumed agent orders again and the budget goes over its limit; G2's resumed agent gets the order the gateway already confirmed.

The run registry is a small database, `fivefold_registry` (set `FIVEFOLD_REGISTRY_DB` for another), with one row per run database. Every run writes its row as it starts: demos, studies, recorded playback, split runs and tests. A study's dropped episode database leaves the registry. `python -m fivefold.harness.migrations bootstrap` creates the registry; the demo, study and recorder commands and the split topology's `start` do so too.

The viewer connects to the server `FIVEFOLD_ADMIN_DSN` names, always as `ff_viewer`, which can only select, in read-only transactions. It opens only databases the registry lists, and reads only their `sim`, `gw`, `harness` and `eval` schemas. Its API answers JSON to `GET` only: `/api/runs`, `/api/runs/DATABASE` (the run and its scorecard), `/api/runs/DATABASE/timeline` and `/api/runs/DATABASE/ledger`. A run is named by its database, since a recording and its replay share a run id.

With Docker Compose, `docker compose up viewer` builds the frontend and the API into one image and serves the viewer on 127.0.0.1:8300 against the Compose database.

To work on the frontend, `cd viewer`, then `npm run dev` serves it with hot reload on port 5173 and passes `/api` to a viewer on port 8300; `npm test` runs the Vitest tests, `npm run lint` runs ESLint, and `npm run build` type checks and builds. `make viewer-e2e` checks a running viewer in Chromium against a demo's runs and saves a screenshot of the side-by-side page; it needs Playwright's Chromium (`npx playwright install chromium`, once).

## Studies and reports

`python -m fivefold.harness.study` runs the episodes of milestone M4. An episode is a cell of `faults/expected.yaml`, or a cell with no fault, run with one seed. By default the study runs every expected cell of dev-001, in B0, G1 and G2 at C0 to C3, once each, with the scripted model. Each episode runs in a fresh database, dropped once it ends.

`--cells` takes `all`, `none` (no fault), a fault (`F2`), a fault and variant (`F11-after_tool_call`) or a cell (`F2-C2-G2`). `--configs` and `--contracts` narrow the matrix. `--episodes N` runs each cell with the seeds S to S+N-1, where `--seed S` defaults to 7. Episodes run in a fixed order, by seed and then by cell, and each result is saved as `CELL-seed-SEED.json`, such as `F2-C0-G2-seed-7.json`. The same inputs with the scripted model give the same bytes, apart from the wall-clock latencies of recovery (see below). The output directory must be new or empty. The cells' fault plans are written for dev-001, so with another scenario only `--cells none` runs. A study refuses to start if any of its episode databases already exists, such as one another study kept, rather than replace it. It never drops a database it did not create: if another study creates an episode's database after that check, the study stops at that episode with one line naming the database, and keeps the results it has.

The evaluator writes each run's result as JSON, with the input and output tokens its model calls used (`tokens_in` and `tokens_out`; a scripted run uses none). `uv run python -m fivefold.harness.demo --out results` saves one file per demo run. `uv run python -m fivefold.evaluator report DIR --csv runs.csv --markdown report.md` turns the result files in `DIR` into a CSV with one row per run and the Markdown report. The report needs no database, and the same results always give the same bytes.

Each result also holds recovery latency (record 0014) for every task intent a fault touched: from the first fault that touched it until the stack's evidence settles the outcome found at the end, in virtual and in wall-clock milliseconds. If an effect committed, recovery ends at the first write answer, status answer or attestation that names one. If none did, it ends when the last of the intent's requests is first shown never to commit, or at the fault when the stack sent none. Evidence the stack held before the fault gives 0 ms. An intent whose outcome the evidence never settles, such as F2 at C0 with no resolver, is unresolved: it has no latency and counts apart. On seed 7 with F2 at C2, recovery takes 2,013 ms in B0, which places a second order and fails properties 1, 2 and 3, and 2,257 ms in G2, which retries with the same key and passes: latency alone does not rank configurations.

The metrics count `affected_intents` and `unresolved_intents`. The CSV adds `recovery_ms_max` and `recovery_wall_ms_max`, the largest latencies among a run's resolved intents. The report adds a table by fault plan, configuration and contract level: affected, resolved and unresolved intents, then the median and largest virtual latency and the median wall-clock latency over the resolved ones, with the lower middle value for an even count. Latencies are never summed. Wall-clock latency depends on the machine, so playback, the determinism check and the report's check for a result given twice all leave it out.

Add `--prices prices.yaml`, a price file in the format of `prices/example.yaml`, to price the tokens. The CSV then gains `cost_usd`, each run's cost in USD as an exact decimal. The report gains a cost table by model, configuration and contract level: runs, passing runs, tokens in and out, total cost and cost per passing run, at the price file's date. A passing run evaluated all five properties and all five hold, so its verdict is pass or pass_unresolved. Cost per passing run is the cost of all the row's runs, failed ones too, over its passing runs. The scripted model costs nothing, and a run of a model the price file does not price is refused. Without a price file, the report says that cost is not computed.

## Live runs

Live runs are manual and never run in CI. A live run is refused unless the environment sets `FIVEFOLD_LIVE=1` and the command gets a spend cap in USD and a price file that prices the model (records 0004 and 0009). `--live PROVIDER:MODEL` names the model. The providers:

- `ollama:MODEL`, a model on an Ollama server: no key. The server is your own at `http://127.0.0.1:11434` unless `OLLAMA_HOST` names another.
- `anthropic:MODEL`, Anthropic's API: your key in `ANTHROPIC_API_KEY`.
- `openai:MODEL`, OpenAI's API: your key in `OPENAI_API_KEY`.
- `google_genai:MODEL`, Google's Gemini API: your key in `GOOGLE_API_KEY` or `GEMINI_API_KEY`. A run is refused while `GOOGLE_GENAI_USE_VERTEXAI` or `GOOGLE_GENAI_USE_ENTERPRISE` is set, since either sends the requests to Vertex AI with Google's default credentials.

A live run with an API provider is refused unless its key variable is set. The key stays in that variable: `--param` takes generation parameters only, so a key, a base URL or a header can never reach a cassette or the run's records.

The price file must price the model under its provider, in USD per million input tokens and per million output tokens. Use prices you have checked on the provider's price list, and set `price_date` to the day you read them: the spend cap and the report's cost are only as right as those prices. `prices/example.yaml` prices local Ollama models at 0 USD and no API model.

To run demo 1 in G2 with a local model:

1. Install [Ollama](https://ollama.com) and pull a model that can call tools: `ollama pull MODEL`.
2. Copy `prices/example.yaml` to `prices.yaml`, and add `MODEL` under `ollama:` at 0 USD, quoted, as the examples are.
3. Record the episode. `CAP` is the spend cap in USD; a local model costs nothing, so any cap above 0 lets it run. If the Ollama server is not at `http://127.0.0.1:11434`, set `OLLAMA_HOST`.

   ```
   FIVEFOLD_LIVE=1 uv run python -m fivefold.recorder record --demo 1 --config G2 \
       --cassette live-1-g2.json --live ollama:MODEL --spend-cap-usd CAP --prices prices.yaml
   ```

4. Play it back, with no network and no flag: `uv run python -m fivefold.recorder play live-1-g2.json --scenario scenarios/dev/dev-001-single-po.yaml`.

The record command prints the verdict, the cassette's digest and what the run spent. Each model call takes the scenario's `think_ms` of virtual time, however long the model really takes, and `harness.model_call` logs its tokens. `--param NAME=VALUE`, such as `--param temperature=0`, sets a model parameter; the cassette records it. Before each call, the spend so far is checked against the cap, and a refused call ends that agent worker as `budget`. A call the provider has not answered after `--call-limit-s` wall-clock seconds (default 600) fails, and its worker ends with an error, so a stalled server cannot hold the run.

A live study takes the same options and is refused without them, before it touches any database. It also saves each episode's cassette in `cassettes/`; `python -m fivefold.recorder play` compares a replay with the original run's database, so give `--keep-databases` to play one back. One spend cap holds across the study, and the study stops once the spend reaches it, since every later model call would be refused. An episode the cap cut short keeps its cassette, and its result goes to `spend-cap/`, which a report over the directory leaves out.

The pilot of 10 to 20 live episodes is the maintainer's to size (`docs/plan.md`, M4). For example, no fault and F2, at C2, in B0, G1 and G2, with three seeds each, is 18 episodes. `MODEL` and `CAP` are placeholders: the maintainer has chosen neither yet.

```
FIVEFOLD_LIVE=1 uv run python -m fivefold.harness.study --out pilot \
    --cells none F2 --contracts C2 --episodes 3 \
    --live ollama:MODEL --spend-cap-usd CAP --prices prices.yaml
uv run python -m fivefold.evaluator report pilot --csv pilot.csv --markdown pilot.md
```

The study prints each episode's verdict next to the one `faults/expected.yaml` expects for its cell, how each agent worker ended, and in a live study the episode's model calls and the spend so far.

## Admin API

The gateway's admin API lists its actions, grants, action events and open escalations, and resolves an escalation when a human attests what happened (records 0003 and 0010). Approvals stay in the approvals service. In-process runs, the demos and the scripted resolver call the gateway's admin path directly. The HTTP API, built with FastAPI, is for a gateway served over HTTP: `fivefold.gateway.admin_api.admin_app(admin, records, token=TOKEN)` returns it as an ASGI app, to mount under a path such as `/admin`. The split topology's gateway serves it at `/admin` (see [Bring your own agent](#bring-your-own-agent-split-topology)). It reads time only from the gateway's virtual clock and never contacts the simulator.

Every request needs `Authorization: Bearer TOKEN`. A deployment reads the token from `FIVEFOLD_ADMIN_API_TOKEN`, and the app refuses an empty one, since the API attests outcomes.

| Request | Answer |
| --- | --- |
| `GET /actions` | Every action: its state, intent, operation, target, arguments and the approval its grant names |
| `GET /actions/A-0001` | One action, its escalation and its action events in seq order |
| `GET /grants` | Every grant |
| `GET /events?from_seq=N` | The run's action events from seq N on, in seq order |
| `GET /escalations` | The open escalations, each with `not_committed_from_vt` |
| `POST /escalations/ESC-0001/resolve` | Attests the outcome, and answers with the action's result |

To attest that order PO-0001 committed:

```
curl -X POST http://HOST/admin/escalations/ESC-0001/resolve \
    -H "Authorization: Bearer $FIVEFOLD_ADMIN_API_TOKEN" -H "Content-Type: application/json" \
    -d '{"resolution": "committed", "effect_id": "PO-0001", "actor": "ops-1"}'
```

`{"resolution": "not_committed", "actor": "ops-1"}` attests that nothing committed. The gateway accepts it only from S after the action's latest send, when every request has committed or never will; S is 10,050 ms of virtual time in dev-001. Errors answer `{"reason": ..., "message": ...}`: 401 without the token, 404 for an unknown action or escalation, 409 for an escalation resolved already or a "not committed" that comes too early (with `not_committed_from_vt`), and 422 for a body that does not parse.

## Bring your own agent (split topology)

The split topology runs the gateway and the simulator as separate services, so you can point your own agent at them (record 0009). It is not deterministic: virtual time follows the host's monotonic clock, so a run takes wall time, and its timing changes from run to run. Faults still fire on counters.

What your agent reaches depends on the configuration:

- **G1 and G2:** the gateway's tools over MCP's streamable HTTP transport, at `http://gateway:8000/mcp`. Run your agent in a container on the Docker network `fivefold-agent`, for example with `docker run --network fivefold-agent`. That network joins the gateway and your agent only, and the agent holds no simulator credential, so it reaches the simulator only through the gateway (record 0008). The boundary holds only for an agent in that container. An agent on the host can reach the simulator at `127.0.0.1:8100`, the Compose PostgreSQL at `127.0.0.1:5432`, which trusts every connection from 127.0.0.1, and the run's directory, which holds the gateway's and the harness's tokens. The gateway's `http://127.0.0.1:8000/mcp` answers an agent on the host too, but outside record 0008's boundary.
- **B0:** the simulator's API over HTTP, at `http://127.0.0.1:8100/api/CALL`. Each call of `src/fivefold/domain/api.py`, such as `get_quote`, `request_approval` or `submit_purchase_order`, is a POST with the request as a JSON body and the header `Authorization: Bearer TOKEN`. `TOKEN` is the agent's credential for this run: `start` prints it and writes it to `agent-1.token` in the run's directory. The final report goes to `/api/submit_final_report`. Every answer the API defines comes with status 200, apart from a refused credential: 401 for a token that is not this run's, 403 for one the configuration does not allow. In G1 and G2 the simulator refuses the agent's token. A request the API never takes gets 404 (no such call) or 422 (a body that is not the call's request, or one holding a NUL character, `\u0000`, which the harness's log cannot store), and once `end` begins, 503. The gateway refuses an MCP message holding a NUL the same way, with invalid params. The simulator publishes its port on 127.0.0.1 only, so run a B0 agent on the host, or in a container on the host's network (`docker run --network host`).

With Docker Compose, from the repository's root:

```
docker compose --profile split run --rm split start \
    --scenario scenarios/dev/dev-001-single-po.yaml --config G2 --contract C2 --plan none
docker compose --profile split up --detach --wait simulator gateway
# Run your agent here: in G1 and G2, in a container on the network fivefold-agent,
# pointed at http://gateway:8000/mcp. To check the setup first, run the scripted agent:
docker compose --profile split run --rm agent \
    --scenario scenarios/dev/dev-001-single-po.yaml --config G2 --contract C2 \
    --gateway-url http://gateway:8000/mcp
docker compose --profile split run --rm split end
docker compose --profile split run --rm split score
docker compose --profile split down
```

Start a run before the services, and start the services again for each run. Without Docker, run the same commands with `uv run python -m fivefold.harness.split` against your PostgreSQL server, and serve each service in its own terminal: `serve simulator`, then `serve gateway`. Without Docker there is no agent network, so a G1 or G2 agent there is outside record 0008's boundary. The run's directory is `split-run` unless `--run-dir` or `FIVEFOLD_RUN_DIR` says otherwise. It holds the run's secrets, so keep it to yourself; git ignores it.

`start` takes the scenario, the configuration, the contract level, the seed (default 7) and the fault plan: the scenario's own by default, `--plan none` for no fault, or `--plan FILE`, a YAML list in the format of a scenario's `faults`. A random plan runs only in the inproc topology, so `start` refuses a scenario that has one. `--model NAME` names the model your agent uses; the run records it with the model adapter `external`, since the harness runs no model here. Each run gets its own database, made from a migrated template database, and a row in the run registry, as every run does (see [Viewer](#viewer)). `start` refuses a database that already has the run's name; `--replace` drops it first, but only if it holds the same run, or none yet, since two run ids can give one default name. For example, F2 on the purchase order's answer in G1 or G2:

```yaml
- fault: F2
  at: {point: net.gw_sim.answer, match: {api: submit_purchase_order, answer: committed}, occurrence: 1}
```

`end` ends the agents' phase: G2 closes the run, and both services refuse your agent from then on. After the drain window, `2 × (D + max_hop_latency) + L` of wall time (20,100 ms with dev-001's values), the run stops. `score` then scores it with the evaluator, and `--out FILE` saves its result JSON for `python -m fivefold.evaluator report`.

The split topology fires faults only on the network and in the simulator, at these points:

| Point | Faults | Configurations |
| --- | --- | --- |
| `net.agent_gw.request` | F4, F12 | G1, G2 |
| `net.gw_sim.request` | F4, F6 late, F7 late, F12 | G1, G2 |
| `net.gw_sim.answer` | F2 | G1, G2 |
| `net.agent_sim.request` | F4, F6 late, F7 late, F12 | B0 |
| `net.agent_sim.answer` | F2, F6 early, F7 early | B0 |
| `sim.write.received` | F5 | all |
| `sim.status.answer` | F10 | all, at C1 |

`start` refuses, in one line, a plan with a fault inside the gateway or your agent: F1, F3, F9 and F11, and F6 and F7 early in G1 and G2. A gateway crash would mean the gateway's process exits and restarts, and the harness has no hold on your agent. It also refuses a scenario with a scripted resolver: here a human resolves escalations.

When the gateway escalates an action whose outcome is unknown, you resolve it through the gateway's [admin API](#admin-api) at `http://127.0.0.1:8000/admin`. The gateway serves it when `FIVEFOLD_ADMIN_API_TOKEN` holds the API's bearer token, in the environment of `serve gateway`, or where you run Compose. The admin API shares its listener with the MCP endpoint, so your agent's network reaches it too, and the bearer token is its only guard. Make the token long and random, for example with `python -c "import secrets; print(secrets.token_urlsafe(32))"`, and never give it to your agent. Each attestation is also logged in the harness's actor log, as the scripted resolver's are in the inproc topology, so the evaluator counts it (record 0004). Once `end` stops the gateway, the admin API takes no attestation: it answers 503 and records nothing.

Limits: a split run has one agent worker, `agent-1`. Start each service once per run: a service keeps its fault counters, whether the agents' phase is over, and the scripted approver's timers in its memory, so one started again mid-run would fire a fault again from its first occurrence, serve your agent after the agents' phase, and never decide an approval it had scheduled. `serve` refuses a run that has ended. Every service must run on one host, since they share its monotonic clock, and each hop is assumed to take at most `max_hop_latency_ms` (50 ms by default). The harness logs your agent's messages when they reach a service, and an answer to your agent as delivered when the service hands it to its HTTP server: it cannot see whether your agent read it. The simulator logs an answer as dropped if your agent's connection has closed; the gateway's MCP library writes its answers itself, so the gateway logs each one as delivered even after your agent has gone. It counts no tokens and no cost, since it runs no model.

## Status

FiveFold is working toward its first release, v0.1. Everything above runs today.

- **Built:** the simulator at contract levels C0 to C3; the deterministic harness with eleven faults, fixed or random fault plans, recorded playback and branching; the reference gateway (G2) and the approval gate (G1); the LangGraph agent with scripted and live models; the evaluator with its reports, cost and recovery latency; the viewer; the admin API; and the split topology for bringing your own agent.
- **Left for v0.1:** the live-model study and its report with raw data, and a short video. The study waits for the maintainer's choice of models, API keys and a spend cap. The held-out scenarios are kept outside this repository.
- **Planned for v0.2:** baselines on Temporal and DBOS, an MCP conformance test for lost replies, a TLA+ model of the state machine, and F8.

Every expected cell is a test, run from its committed seed. Record 0006 fixes no G1 outcome, so G1's cells come from its first runs, and record 0013 settles two of F10's. Hypothesis checks invariants I1 to I7 on the gateway's state machine, and on full runs of dev-001 with random fault plans (`{random: {from: [...], max: N}}`, record 0006) in every configuration and contract level: 20 runs in CI and 100 nightly.

## Documents

Questions, bug reports and failure modes FiveFold does not cover yet are welcome as issues. [CONTRIBUTING.md](CONTRIBUTING.md) says how to propose a change.

- [docs/product.md](docs/product.md): what and why, scope and non-goals
- [docs/spec.md](docs/spec.md): the contracts to build against
- [docs/plan.md](docs/plan.md): milestones, workstreams and first tickets
- [docs/limitations.md](docs/limitations.md): what each guarantee depends on, and the known limits
- [docs/adr/](docs/adr/README.md): decision records, with a suggested reading order
- [CONTRIBUTING.md](CONTRIBUTING.md): how to propose changes
- [SECURITY.md](SECURITY.md): how to report a vulnerability privately
- [CLAUDE.md](CLAUDE.md): rules for coding agents working in this repository

## Development

You need Python 3.12, [uv](https://docs.astral.sh/uv/) 0.13.8 or later, and PostgreSQL 16. Docker Compose can run the database for you. The viewer's frontend also needs Node 26 and npm; the Python tests never do.

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

## Maintainer

[samrusani](https://github.com/samrusani)

## License

Apache License 2.0. See [LICENSE](LICENSE).
