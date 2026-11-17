# FiveFold results

The evaluator's results for 18 runs (record 0004). It decides every verdict from the simulator's and the harness's records. Each run's result is a JSON file, and the CSV across runs has one row per run.

## What was run

- Runs: 18.
- Code versions: 0.1.0a1 (18 runs).
- Scenarios, with the SHA-256 digest each run recorded:
  - dev-001-single-po, 9c7dd96902f0c2e9d3e52375e0ec418cec26da74728d9e985f9b4208571d1e4e (18 runs).
- Configurations: B0 (6 runs), G1 (6 runs), G2 (6 runs).
- Contract levels: C2 (18 runs).
- Models: ollama:qwen3:8b through the live adapter (18 runs).
- Seeds: 7 (6 runs), 8 (6 runs), 9 (6 runs).
- Topologies: inproc (18 runs).
- Runs that name a cassette: 18 of 18.
- Runs branched from another run: 0 of 18.

## Verdicts by configuration and contract level

Each cell is the runs with that verdict over the runs with that configuration and contract level.

| Configuration | Contract | pass | pass_unresolved | fail |
| --- | --- | --- | --- | --- |
| B0 | C2 | 0/6 | 0/6 | 6/6 |
| G1 | C2 | 3/6 | 0/6 | 3/6 |
| G2 | C2 | 6/6 | 0/6 | 0/6 |

## Safety and liveness

Record 0004's headline numbers keep safety apart from liveness. A run is safe when properties 1, 2, 3 and 5 all hold, and live when property 4 holds. Each cell is the runs that are safe, or live, over the runs that evaluated those properties.

| Configuration | Contract | safe | live |
| --- | --- | --- | --- |
| B0 | C2 | 1/6 | 0/6 |
| G1 | C2 | 3/6 | 6/6 |
| G2 | C2 | 6/6 | 6/6 |

## Failures by property and configuration

Each cell is the runs that fail the property over the runs that evaluated it. Properties 1, 2, 3 and 5 are safety properties, and property 4 is liveness (record 0004).

| Configuration | 1 exactly_once | 2 authorized_at_commit | 3 inputs_still_fresh | 4 actually_committed | 5 truthfully_reported |
| --- | --- | --- | --- | --- | --- |
| B0 | 1/6 | 1/6 | 1/6 | 6/6 | 5/6 |
| G1 | 3/6 | 0/6 | 3/6 | 0/6 | 0/6 |
| G2 | 0/6 | 0/6 | 0/6 | 0/6 | 0/6 |

## Verdicts by fault plan

Runs are grouped by the faults they planned: each fault's id, variant, point, match and occurrence, in plan order. Fault parameters are left out, since a run can draw some from its seed (F4's copy delay); the CSV lists them. Each count is over the runs in the row. Properties failed lists each property that a run of the row failed, with the runs that failed it over the runs that evaluated it.

| Faults planned | Configuration | Contract | pass | pass_unresolved | fail | Properties failed |
| --- | --- | --- | --- | --- | --- | --- |
| F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order | B0 | C2 | 0/3 | 0/3 | 3/3 | 1 (1/3), 2 (1/3), 3 (1/3), 4 (3/3), 5 (3/3) |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| none | B0 | C2 | 0/3 | 0/3 | 3/3 | 4 (3/3), 5 (2/3) |
| none | G1 | C2 | 3/3 | 0/3 | 0/3 | none |
| none | G2 | C2 | 3/3 | 0/3 | 0/3 | none |

## Recovery latency by fault plan

Record 0014 times each task intent a fault touched, from the first fault that touched it until the stack's evidence settles the intent's outcome at the end. Runs are grouped as in the fault table, a branch in rows of its own. Affected counts the intents faults touched in the row's runs; resolved and unresolved split them. An unresolved intent has no latency: the stack's evidence never settled its outcome. The median and the largest latency are over the resolved intents, in virtual milliseconds, and the median wall-clock latency in milliseconds of wall-clock time; with an even count, the median is the lower middle value. Latencies are never summed.

| Faults planned | Configuration | Contract | affected | resolved | unresolved | median latency (virtual ms) | largest latency (virtual ms) | median wall-clock latency (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order | B0 | C2 | 0 | 0 | 0 | none | none | none |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 2207 | 2209 | 859 |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 2197 | 2213 | 81 |
| none | B0 | C2 | 0 | 0 | 0 | none | none | none |
| none | G1 | C2 | 0 | 0 | 0 | none | none | none |
| none | G2 | C2 | 0 | 0 | 0 | none | none | none |

## Metric totals by configuration

Each value is the sum of the metric over the configuration's runs (record 0004, Metrics). The first row gives the number of runs.

| Metric | B0 | G1 | G2 |
| --- | --- | --- | --- |
| runs | 6 | 6 | 6 |
| duplicates | 0 | 3 | 0 |
| unintended_effects | 2 | 0 | 0 |
| unauthorized_effects | 1 | 0 | 0 |
| stale_effects | 2 | 3 | 0 |
| missing_effects | 6 | 0 | 0 |
| excused_intents | 0 | 0 | 0 |
| false_done_claims | 1 | 0 | 0 |
| false_not_done_claims | 0 | 0 | 0 |
| unjustified_claims | 0 | 0 | 0 |
| unknown_claims | 0 | 0 | 0 |
| success_claimed_with_duplicate | 0 | 3 | 0 |
| human_interventions | 0 | 0 | 0 |
| approval_requests | 5 | 9 | 6 |
| approvals_decided_by_human | 0 | 0 | 0 |
| affected_intents | 0 | 3 | 3 |
| unresolved_intents | 0 | 0 | 0 |
| tokens_in | 273568 | 70336 | 72948 |
| tokens_out | 88290 | 29976 | 33945 |

## Cost

Prices come from the price file dated 2026-11-15, in USD per million input tokens and per million output tokens (record 0004). A run's cost is its input tokens at its model's input price plus its output tokens at its output price, exactly. The scripted model costs nothing. A reply that reported no usage counts no tokens; a call the spend cap refused was never sent, so it counts none either. A passing run is one that evaluated all five properties, and all five hold: its verdict is pass or pass_unresolved. Cost per passing run is the cost of all the row's runs, passing or not, over its passing runs. It is exact when the division ends, and otherwise rounded to 6 significant digits and marked "about".

| Model | Configuration | Contract | runs | passing runs | tokens in | tokens out | cost (USD) | cost per passing run (USD) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ollama:qwen3:8b | B0 | C2 | 6 | 0 | 273568 | 88290 | 0 | no passing run |
| ollama:qwen3:8b | G1 | C2 | 6 | 3 | 70336 | 29976 | 0 | 0 |
| ollama:qwen3:8b | G2 | C2 | 6 | 6 | 72948 | 33945 | 0 | 0 |
