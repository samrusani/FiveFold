# FiveFold results

The evaluator's results for 741 runs (record 0004). It decides every verdict from the simulator's and the harness's records. Each run's result is a JSON file, and the CSV across runs has one row per run.

## What was run

- Runs: 741.
- Code versions: 0.1.0a1 (741 runs).
- Scenarios, with the SHA-256 digest each run recorded:
  - dev-001-single-po, 9c7dd96902f0c2e9d3e52375e0ec418cec26da74728d9e985f9b4208571d1e4e (738 runs).
  - dev-001-single-po, ae1bca6563d1efc7e97e15f55654b7afc0681a60b638f9f72f827821b185e3be (3 runs).
- Configurations: B0 (228 runs), G1 (252 runs), G2 (261 runs).
- Contract levels: C0 (183 runs), C1 (192 runs), C2 (183 runs), C3 (183 runs).
- Models: anthropic:claude-sonnet-5-5 through the live adapter (741 runs).
- Seeds: 7 (247 runs), 8 (247 runs), 9 (247 runs).
- Topologies: inproc (741 runs).
- Runs that name a cassette: 741 of 741.
- Runs branched from another run: 0 of 741.

## Verdicts by configuration and contract level

Each cell is the runs with that verdict over the runs with that configuration and contract level.

| Configuration | Contract | pass | pass_unresolved | fail |
| --- | --- | --- | --- | --- |
| B0 | C0 | 18/57 | 0/57 | 39/57 |
| B0 | C1 | 18/57 | 0/57 | 39/57 |
| B0 | C2 | 18/57 | 0/57 | 39/57 |
| B0 | C3 | 39/57 | 8/57 | 10/57 |
| G1 | C0 | 21/63 | 0/63 | 42/63 |
| G1 | C1 | 21/63 | 0/63 | 42/63 |
| G1 | C2 | 21/63 | 0/63 | 42/63 |
| G1 | C3 | 42/63 | 1/63 | 20/63 |
| G2 | C0 | 36/63 | 12/63 | 15/63 |
| G2 | C1 | 57/72 | 0/72 | 15/72 |
| G2 | C2 | 57/63 | 0/63 | 6/63 |
| G2 | C3 | 63/63 | 0/63 | 0/63 |

## Safety and liveness

Record 0004's headline numbers keep safety apart from liveness. A run is safe when properties 1, 2, 3 and 5 all hold, and live when property 4 holds. Each cell is the runs that are safe, or live, over the runs that evaluated those properties.

| Configuration | Contract | safe | live |
| --- | --- | --- | --- |
| B0 | C0 | 18/57 | 57/57 |
| B0 | C1 | 18/57 | 57/57 |
| B0 | C2 | 18/57 | 57/57 |
| B0 | C3 | 47/57 | 57/57 |
| G1 | C0 | 21/63 | 63/63 |
| G1 | C1 | 21/63 | 63/63 |
| G1 | C2 | 21/63 | 63/63 |
| G1 | C3 | 43/63 | 63/63 |
| G2 | C0 | 54/63 | 57/63 |
| G2 | C1 | 57/72 | 72/72 |
| G2 | C2 | 57/63 | 63/63 |
| G2 | C3 | 63/63 | 63/63 |

## Failures by property and configuration

Each cell is the runs that fail the property over the runs that evaluated it. Properties 1, 2, 3 and 5 are safety properties, and property 4 is liveness (record 0004).

| Configuration | 1 exactly_once | 2 authorized_at_commit | 3 inputs_still_fresh | 4 actually_committed | 5 truthfully_reported |
| --- | --- | --- | --- | --- | --- |
| B0 | 81/228 | 81/228 | 99/228 | 0/228 | 28/228 |
| G1 | 99/252 | 18/252 | 117/252 | 0/252 | 74/252 |
| G2 | 9/261 | 15/261 | 18/261 | 6/261 | 3/261 |

## Verdicts by fault plan

Runs are grouped by the faults they planned: each fault's id, variant, point, match and occurrence, in plan order. Fault parameters are left out, since a run can draw some from its seed (F4's copy delay); the CSV lists them. Each count is over the runs in the row. Properties failed lists each property that a run of the row failed, with the runs that failed it over the runs that evaluated it.

| Faults planned | Configuration | Contract | pass | pass_unresolved | fail | Properties failed |
| --- | --- | --- | --- | --- | --- | --- |
| F1 a at agent.before_tool_call when tool=submit_purchase_order | B0 | C0 | 3/3 | 0/3 | 0/3 | none |
| F1 a at agent.before_tool_call when tool=submit_purchase_order | B0 | C1 | 3/3 | 0/3 | 0/3 | none |
| F1 a at agent.before_tool_call when tool=submit_purchase_order | B0 | C2 | 3/3 | 0/3 | 0/3 | none |
| F1 a at agent.before_tool_call when tool=submit_purchase_order | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G1 | C3 | 0/3 | 0/3 | 3/3 | 5 (3/3) |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F1 b at agent.before_tool_call when tool=submit_purchase_order | B0 | C0 | 3/3 | 0/3 | 0/3 | none |
| F1 b at agent.before_tool_call when tool=submit_purchase_order | B0 | C1 | 3/3 | 0/3 | 0/3 | none |
| F1 b at agent.before_tool_call when tool=submit_purchase_order | B0 | C2 | 3/3 | 0/3 | 0/3 | none |
| F1 b at agent.before_tool_call when tool=submit_purchase_order | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G1 | C0 | 3/3 | 0/3 | 0/3 | none |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G1 | C1 | 3/3 | 0/3 | 0/3 | none |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G1 | C2 | 3/3 | 0/3 | 0/3 | none |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G2 | C0 | 0/3 | 0/3 | 3/3 | 4 (3/3) |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order | B0 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order | B0 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order | B0 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order | B0 | C3 | 0/3 | 1/3 | 2/3 | 5 (2/3) |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G1 | C3 | 0/3 | 0/3 | 3/3 | 5 (3/3) |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G2 | C0 | 0/3 | 3/3 | 0/3 | none |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order + F10 delayed at sim.status.answer when api=get_order_status | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order + F10 stale_beyond_lag at sim.write.received when api=submit_purchase_order | G2 | C1 | 0/3 | 0/3 | 3/3 | 5 (3/3) |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order + F10 stale_within_lag at sim.status.answer when api=get_order_status | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F3 at agent.after_tool_result when tool=submit_purchase_order | B0 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F3 at agent.after_tool_result when tool=submit_purchase_order | B0 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F3 at agent.after_tool_result when tool=submit_purchase_order | B0 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F3 at agent.after_tool_result when tool=submit_purchase_order | B0 | C3 | 0/3 | 2/3 | 1/3 | 5 (1/3) |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G1 | C3 | 0/3 | 1/3 | 2/3 | 5 (2/3) |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G2 | C0 | 0/3 | 3/3 | 0/3 | none |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F4 at net.agent_gw.request when api=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F4 at net.agent_gw.request when api=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F4 at net.agent_gw.request when api=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F4 at net.agent_gw.request when api=submit_purchase_order | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F4 at net.agent_gw.request when api=submit_purchase_order | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F4 at net.agent_gw.request when api=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F4 at net.agent_gw.request when api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F4 at net.agent_gw.request when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F4 at net.agent_sim.request when api=submit_purchase_order | B0 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F4 at net.agent_sim.request when api=submit_purchase_order | B0 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F4 at net.agent_sim.request when api=submit_purchase_order | B0 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F4 at net.agent_sim.request when api=submit_purchase_order | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F4 at net.gw_sim.request when api=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F4 at net.gw_sim.request when api=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F4 at net.gw_sim.request when api=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F4 at net.gw_sim.request when api=submit_purchase_order | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F4 at net.gw_sim.request when api=submit_purchase_order | G2 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F4 at net.gw_sim.request when api=submit_purchase_order | G2 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F4 at net.gw_sim.request when api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F4 at net.gw_sim.request when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | B0 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | B0 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | B0 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G2 | C0 | 0/3 | 3/3 | 0/3 | none |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G2 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F5 in_contract at sim.write.received when api=submit_purchase_order | B0 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F5 in_contract at sim.write.received when api=submit_purchase_order | B0 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F5 in_contract at sim.write.received when api=submit_purchase_order | B0 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F5 in_contract at sim.write.received when api=submit_purchase_order | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3) |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G2 | C0 | 0/3 | 3/3 | 0/3 | none |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F6 early at gw.dispatch.before_check | G1 | C0 | 3/3 | 0/3 | 0/3 | none |
| F6 early at gw.dispatch.before_check | G1 | C1 | 3/3 | 0/3 | 0/3 | none |
| F6 early at gw.dispatch.before_check | G1 | C2 | 3/3 | 0/3 | 0/3 | none |
| F6 early at gw.dispatch.before_check | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F6 early at gw.dispatch.before_check | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F6 early at gw.dispatch.before_check | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F6 early at gw.dispatch.before_check | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F6 early at gw.dispatch.before_check | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F6 early at net.agent_sim.answer when api=request_approval | B0 | C0 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 early at net.agent_sim.answer when api=request_approval | B0 | C1 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 early at net.agent_sim.answer when api=request_approval | B0 | C2 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 early at net.agent_sim.answer when api=request_approval | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F6 late at net.agent_sim.request when api=submit_purchase_order | B0 | C0 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 late at net.agent_sim.request when api=submit_purchase_order | B0 | C1 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 late at net.agent_sim.request when api=submit_purchase_order | B0 | C2 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 late at net.agent_sim.request when api=submit_purchase_order | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G2 | C0 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G2 | C1 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G2 | C2 | 0/3 | 0/3 | 3/3 | 2 (3/3) |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at gw.dispatch.before_check | G1 | C0 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at gw.dispatch.before_check | G1 | C1 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at gw.dispatch.before_check | G1 | C2 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at gw.dispatch.before_check | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at gw.dispatch.before_check | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at gw.dispatch.before_check | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at gw.dispatch.before_check | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at gw.dispatch.before_check | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.agent_sim.answer when api=request_approval | B0 | C0 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.agent_sim.answer when api=request_approval | B0 | C1 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.agent_sim.answer when api=request_approval | B0 | C2 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.agent_sim.answer when api=request_approval | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.agent_sim.request when api=submit_purchase_order | B0 | C0 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.agent_sim.request when api=submit_purchase_order | B0 | C1 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.agent_sim.request when api=submit_purchase_order | B0 | C2 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.agent_sim.request when api=submit_purchase_order | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G1 | C0 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G1 | C1 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G1 | C2 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 price_up at gw.dispatch.before_check | G1 | C0 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at gw.dispatch.before_check | G1 | C1 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at gw.dispatch.before_check | G1 | C2 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at gw.dispatch.before_check | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 price_up at gw.dispatch.before_check | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F7 price_up at gw.dispatch.before_check | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F7 price_up at gw.dispatch.before_check | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F7 price_up at gw.dispatch.before_check | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 price_up at net.agent_sim.answer when api=request_approval | B0 | C0 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.agent_sim.answer when api=request_approval | B0 | C1 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.agent_sim.answer when api=request_approval | B0 | C2 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.agent_sim.answer when api=request_approval | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 price_up at net.agent_sim.request when api=submit_purchase_order | B0 | C0 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.agent_sim.request when api=submit_purchase_order | B0 | C1 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.agent_sim.request when api=submit_purchase_order | B0 | C2 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.agent_sim.request when api=submit_purchase_order | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G2 | C0 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G2 | C1 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G2 | C2 | 0/3 | 0/3 | 3/3 | 3 (3/3) |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F9 different_args at agent.step when worker=agent-1 | B0 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 different_args at agent.step when worker=agent-1 | B0 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 different_args at agent.step when worker=agent-1 | B0 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 different_args at agent.step when worker=agent-1 | B0 | C3 | 0/3 | 0/3 | 3/3 | 5 (3/3) |
| F9 different_args at agent.step when worker=agent-1 | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 different_args at agent.step when worker=agent-1 | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 different_args at agent.step when worker=agent-1 | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 different_args at agent.step when worker=agent-1 | G1 | C3 | 0/3 | 0/3 | 3/3 | 5 (3/3) |
| F9 different_args at agent.step when worker=agent-1 | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F9 different_args at agent.step when worker=agent-1 | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F9 different_args at agent.step when worker=agent-1 | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F9 different_args at agent.step when worker=agent-1 | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F9 same_args at agent.step when worker=agent-1 | B0 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 same_args at agent.step when worker=agent-1 | B0 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 same_args at agent.step when worker=agent-1 | B0 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 same_args at agent.step when worker=agent-1 | B0 | C3 | 0/3 | 0/3 | 3/3 | 5 (3/3) |
| F9 same_args at agent.step when worker=agent-1 | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 same_args at agent.step when worker=agent-1 | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 same_args at agent.step when worker=agent-1 | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F9 same_args at agent.step when worker=agent-1 | G1 | C3 | 0/3 | 0/3 | 3/3 | 5 (3/3) |
| F9 same_args at agent.step when worker=agent-1 | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F9 same_args at agent.step when worker=agent-1 | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F9 same_args at agent.step when worker=agent-1 | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F9 same_args at agent.step when worker=agent-1 | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | B0 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | B0 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | B0 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | B0 | C3 | 0/3 | 2/3 | 1/3 | 5 (1/3) |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G1 | C3 | 0/3 | 0/3 | 3/3 | 5 (3/3) |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | B0 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | B0 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | B0 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 2 (3/3), 3 (3/3) |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | B0 | C3 | 0/3 | 3/3 | 0/3 | none |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G1 | C0 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G1 | C1 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G1 | C2 | 0/3 | 0/3 | 3/3 | 1 (3/3), 3 (3/3), 5 (3/3) |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G1 | C3 | 0/3 | 0/3 | 3/3 | 5 (3/3) |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_gw.request when api=submit_purchase_order | G1 | C0 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_gw.request when api=submit_purchase_order | G1 | C1 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_gw.request when api=submit_purchase_order | G1 | C2 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_gw.request when api=submit_purchase_order | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_gw.request when api=submit_purchase_order | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_gw.request when api=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_gw.request when api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_gw.request when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_sim.request when api=submit_purchase_order | B0 | C0 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_sim.request when api=submit_purchase_order | B0 | C1 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_sim.request when api=submit_purchase_order | B0 | C2 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.agent_sim.request when api=submit_purchase_order | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.gw_sim.request when api=submit_purchase_order | G1 | C0 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.gw_sim.request when api=submit_purchase_order | G1 | C1 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.gw_sim.request when api=submit_purchase_order | G1 | C2 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.gw_sim.request when api=submit_purchase_order | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.gw_sim.request when api=submit_purchase_order | G2 | C0 | 0/3 | 0/3 | 3/3 | 4 (3/3) |
| F12 at net.gw_sim.request when api=submit_purchase_order | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.gw_sim.request when api=submit_purchase_order | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| F12 at net.gw_sim.request when api=submit_purchase_order | G2 | C3 | 3/3 | 0/3 | 0/3 | none |
| none | B0 | C0 | 3/3 | 0/3 | 0/3 | none |
| none | B0 | C1 | 3/3 | 0/3 | 0/3 | none |
| none | B0 | C2 | 3/3 | 0/3 | 0/3 | none |
| none | B0 | C3 | 3/3 | 0/3 | 0/3 | none |
| none | G1 | C0 | 3/3 | 0/3 | 0/3 | none |
| none | G1 | C1 | 3/3 | 0/3 | 0/3 | none |
| none | G1 | C2 | 3/3 | 0/3 | 0/3 | none |
| none | G1 | C3 | 3/3 | 0/3 | 0/3 | none |
| none | G2 | C0 | 3/3 | 0/3 | 0/3 | none |
| none | G2 | C1 | 3/3 | 0/3 | 0/3 | none |
| none | G2 | C2 | 3/3 | 0/3 | 0/3 | none |
| none | G2 | C3 | 3/3 | 0/3 | 0/3 | none |

## Recovery latency by fault plan

Record 0014 times each task intent a fault touched, from the first fault that touched it until the stack's evidence settles the intent's outcome at the end. Runs are grouped as in the fault table, a branch in rows of its own. Affected counts the intents faults touched in the row's runs; resolved and unresolved split them. An unresolved intent has no latency: the stack's evidence never settled its outcome. The median and the largest latency are over the resolved intents, in virtual milliseconds, and the median wall-clock latency in milliseconds of wall-clock time; with an even count, the median is the lower middle value. Latencies are never summed.

| Faults planned | Configuration | Contract | affected | resolved | unresolved | median latency (virtual ms) | largest latency (virtual ms) | median wall-clock latency (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F1 a at agent.before_tool_call when tool=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 1047 | 1048 | 53 |
| F1 a at agent.before_tool_call when tool=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 1047 | 1048 | 54 |
| F1 a at agent.before_tool_call when tool=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 1047 | 1048 | 57 |
| F1 a at agent.before_tool_call when tool=submit_purchase_order | B0 | C3 | 3 | 3 | 0 | 1047 | 1048 | 58 |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 1091 | 1099 | 1116 |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 1091 | 1099 | 1188 |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 1091 | 1099 | 738 |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 1091 | 1099 | 1138 |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G2 | C0 | 3 | 3 | 0 | 1240 | 1285 | 1315 |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 1240 | 1285 | 1330 |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 1240 | 1285 | 1256 |
| F1 a at gw.dispatch.after_check when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 1240 | 1285 | 1268 |
| F1 b at agent.before_tool_call when tool=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 1047 | 1048 | 63 |
| F1 b at agent.before_tool_call when tool=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 1047 | 1048 | 58 |
| F1 b at agent.before_tool_call when tool=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 1047 | 1048 | 66 |
| F1 b at agent.before_tool_call when tool=submit_purchase_order | B0 | C3 | 3 | 3 | 0 | 1047 | 1048 | 62 |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 2304 | 2326 | 2951 |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 2304 | 2326 | 2746 |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 2304 | 2326 | 2807 |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 2304 | 2326 | 2880 |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G2 | C0 | 3 | 0 | 3 | none | none | none |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 11976 | 11978 | 7111 |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 2242 | 2249 | 2778 |
| F1 b at gw.dispatch.after_record when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 2062 | 2079 | 2827 |
| F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 2012 | 2036 | 1175 |
| F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 2012 | 2036 | 836 |
| F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 2012 | 2036 | 1056 |
| F2 at net.agent_sim.answer when answer=committed, api=submit_purchase_order | B0 | C3 | 3 | 0 | 3 | none | none | none |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 2174 | 2196 | 1080 |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 2174 | 2196 | 1266 |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 2174 | 2196 | 1174 |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G1 | C3 | 3 | 0 | 3 | none | none | none |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G2 | C0 | 3 | 0 | 3 | none | none | none |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 3041 | 3056 | 1280 |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 2224 | 2247 | 98 |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 2045 | 2067 | 36 |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order + F10 delayed at sim.status.answer when api=get_order_status | G2 | C1 | 3 | 3 | 0 | 18033 | 18066 | 1468 |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order + F10 stale_beyond_lag at sim.write.received when api=submit_purchase_order | G2 | C1 | 3 | 0 | 3 | none | none | none |
| F2 at net.gw_sim.answer when answer=committed, api=submit_purchase_order + F10 stale_within_lag at sim.status.answer when api=get_order_status | G2 | C1 | 3 | 3 | 0 | 3056 | 4032 | 1330 |
| F3 at agent.after_tool_result when tool=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 1019 | 1029 | 40 |
| F3 at agent.after_tool_result when tool=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 1019 | 1029 | 31 |
| F3 at agent.after_tool_result when tool=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 1019 | 1029 | 30 |
| F3 at agent.after_tool_result when tool=submit_purchase_order | B0 | C3 | 3 | 0 | 3 | none | none | none |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 2318 | 2335 | 2836 |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 2318 | 2335 | 2177 |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 2318 | 2335 | 2788 |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G1 | C3 | 3 | 0 | 3 | none | none | none |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G2 | C0 | 3 | 0 | 3 | none | none | none |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 2055 | 2089 | 2730 |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 2155 | 2212 | 2950 |
| F3 at gw.dispatch.after_answer when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 1998 | 2036 | 1067 |
| F4 at net.agent_gw.request when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 169 | 235 | 194 |
| F4 at net.agent_gw.request when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 169 | 235 | 193 |
| F4 at net.agent_gw.request when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 169 | 235 | 193 |
| F4 at net.agent_gw.request when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 169 | 235 | 170 |
| F4 at net.agent_gw.request when api=submit_purchase_order | G2 | C0 | 3 | 3 | 0 | 390 | 491 | 197 |
| F4 at net.agent_gw.request when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 390 | 491 | 138 |
| F4 at net.agent_gw.request when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 390 | 491 | 185 |
| F4 at net.agent_gw.request when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 390 | 491 | 166 |
| F4 at net.agent_sim.request when api=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 47 | 48 | 44 |
| F4 at net.agent_sim.request when api=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 47 | 48 | 39 |
| F4 at net.agent_sim.request when api=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 47 | 48 | 29 |
| F4 at net.agent_sim.request when api=submit_purchase_order | B0 | C3 | 3 | 3 | 0 | 47 | 48 | 49 |
| F4 at net.gw_sim.request when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 57 | 72 | 21 |
| F4 at net.gw_sim.request when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 57 | 72 | 33 |
| F4 at net.gw_sim.request when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 57 | 72 | 23 |
| F4 at net.gw_sim.request when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 57 | 72 | 34 |
| F4 at net.gw_sim.request when api=submit_purchase_order | G2 | C0 | 3 | 3 | 0 | 57 | 58 | 28 |
| F4 at net.gw_sim.request when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 57 | 58 | 21 |
| F4 at net.gw_sim.request when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 57 | 58 | 26 |
| F4 at net.gw_sim.request when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 57 | 58 | 28 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 2012 | 2036 | 1083 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 2012 | 2036 | 806 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 2012 | 2036 | 867 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | B0 | C3 | 3 | 3 | 0 | 2012 | 2036 | 666 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 2174 | 2196 | 1371 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 2174 | 2196 | 1301 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 2174 | 2196 | 1124 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 2174 | 2196 | 1444 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G2 | C0 | 3 | 0 | 3 | none | none | none |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 11928 | 11931 | 4574 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 30788 | 30796 | 1675 |
| F5 beyond_deadline at sim.write.received when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 30036 | 30060 | 1521 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 2012 | 2036 | 912 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 2012 | 2036 | 1037 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 2012 | 2036 | 1043 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | B0 | C3 | 3 | 3 | 0 | 2012 | 2036 | 858 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 2174 | 2196 | 922 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 2174 | 2196 | 917 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 2174 | 2196 | 732 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 2174 | 2196 | 1278 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G2 | C0 | 3 | 0 | 3 | none | none | none |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 5140 | 5161 | 1586 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 6350 | 6419 | 1543 |
| F5 in_contract at sim.write.received when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 6034 | 6037 | 1560 |
| F6 early at gw.dispatch.before_check | G1 | C0 | 3 | 3 | 0 | 0 | 0 | 0 |
| F6 early at gw.dispatch.before_check | G1 | C1 | 3 | 3 | 0 | 0 | 0 | 0 |
| F6 early at gw.dispatch.before_check | G1 | C2 | 3 | 3 | 0 | 0 | 0 | 0 |
| F6 early at gw.dispatch.before_check | G1 | C3 | 3 | 3 | 0 | 0 | 0 | 0 |
| F6 early at gw.dispatch.before_check | G2 | C0 | 3 | 3 | 0 | 0 | 0 | 0 |
| F6 early at gw.dispatch.before_check | G2 | C1 | 3 | 3 | 0 | 0 | 0 | 0 |
| F6 early at gw.dispatch.before_check | G2 | C2 | 3 | 3 | 0 | 0 | 0 | 0 |
| F6 early at gw.dispatch.before_check | G2 | C3 | 3 | 3 | 0 | 0 | 0 | 0 |
| F6 early at net.agent_sim.answer when api=request_approval | B0 | C0 | 3 | 3 | 0 | 1053 | 1081 | 2512 |
| F6 early at net.agent_sim.answer when api=request_approval | B0 | C1 | 3 | 3 | 0 | 1053 | 1081 | 2582 |
| F6 early at net.agent_sim.answer when api=request_approval | B0 | C2 | 3 | 3 | 0 | 1053 | 1081 | 2428 |
| F6 early at net.agent_sim.answer when api=request_approval | B0 | C3 | 3 | 3 | 0 | 1053 | 1081 | 2555 |
| F6 late at net.agent_sim.request when api=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 47 | 48 | 50 |
| F6 late at net.agent_sim.request when api=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 47 | 48 | 49 |
| F6 late at net.agent_sim.request when api=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 47 | 48 | 38 |
| F6 late at net.agent_sim.request when api=submit_purchase_order | B0 | C3 | 3 | 3 | 0 | 47 | 48 | 41 |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 57 | 72 | 22 |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 57 | 72 | 19 |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 57 | 72 | 24 |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 3472 | 3473 | 5942 |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G2 | C0 | 3 | 3 | 0 | 57 | 58 | 28 |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 57 | 58 | 16 |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 57 | 58 | 15 |
| F6 late at net.gw_sim.request when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 3718 | 3718 | 6488 |
| F7 harmless at gw.dispatch.before_check | G1 | C0 | 3 | 3 | 0 | 87 | 167 | 59 |
| F7 harmless at gw.dispatch.before_check | G1 | C1 | 3 | 3 | 0 | 87 | 167 | 52 |
| F7 harmless at gw.dispatch.before_check | G1 | C2 | 3 | 3 | 0 | 87 | 167 | 82 |
| F7 harmless at gw.dispatch.before_check | G1 | C3 | 3 | 3 | 0 | 87 | 167 | 76 |
| F7 harmless at gw.dispatch.before_check | G2 | C0 | 3 | 3 | 0 | 216 | 231 | 76 |
| F7 harmless at gw.dispatch.before_check | G2 | C1 | 3 | 3 | 0 | 216 | 231 | 91 |
| F7 harmless at gw.dispatch.before_check | G2 | C2 | 3 | 3 | 0 | 216 | 231 | 91 |
| F7 harmless at gw.dispatch.before_check | G2 | C3 | 3 | 3 | 0 | 216 | 231 | 86 |
| F7 harmless at net.agent_sim.answer when api=request_approval | B0 | C0 | 3 | 3 | 0 | 1053 | 1081 | 2396 |
| F7 harmless at net.agent_sim.answer when api=request_approval | B0 | C1 | 3 | 3 | 0 | 1053 | 1081 | 2829 |
| F7 harmless at net.agent_sim.answer when api=request_approval | B0 | C2 | 3 | 3 | 0 | 1053 | 1081 | 2422 |
| F7 harmless at net.agent_sim.answer when api=request_approval | B0 | C3 | 3 | 3 | 0 | 1053 | 1081 | 2502 |
| F7 harmless at net.agent_sim.request when api=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 47 | 48 | 40 |
| F7 harmless at net.agent_sim.request when api=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 47 | 48 | 58 |
| F7 harmless at net.agent_sim.request when api=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 47 | 48 | 46 |
| F7 harmless at net.agent_sim.request when api=submit_purchase_order | B0 | C3 | 3 | 3 | 0 | 47 | 48 | 38 |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 57 | 72 | 31 |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 57 | 72 | 18 |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 57 | 72 | 21 |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 57 | 72 | 18 |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G2 | C0 | 3 | 3 | 0 | 57 | 58 | 16 |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 57 | 58 | 30 |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 57 | 58 | 15 |
| F7 harmless at net.gw_sim.request when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 57 | 58 | 17 |
| F7 price_up at gw.dispatch.before_check | G1 | C0 | 3 | 3 | 0 | 87 | 167 | 54 |
| F7 price_up at gw.dispatch.before_check | G1 | C1 | 3 | 3 | 0 | 87 | 167 | 40 |
| F7 price_up at gw.dispatch.before_check | G1 | C2 | 3 | 3 | 0 | 87 | 167 | 66 |
| F7 price_up at gw.dispatch.before_check | G1 | C3 | 3 | 3 | 0 | 4621 | 4648 | 8001 |
| F7 price_up at gw.dispatch.before_check | G2 | C0 | 3 | 3 | 0 | 2734 | 2743 | 4426 |
| F7 price_up at gw.dispatch.before_check | G2 | C1 | 3 | 3 | 0 | 2734 | 2743 | 4318 |
| F7 price_up at gw.dispatch.before_check | G2 | C2 | 3 | 3 | 0 | 2734 | 2743 | 4718 |
| F7 price_up at gw.dispatch.before_check | G2 | C3 | 3 | 3 | 0 | 2734 | 2743 | 4457 |
| F7 price_up at net.agent_sim.answer when api=request_approval | B0 | C0 | 3 | 3 | 0 | 1053 | 1081 | 2440 |
| F7 price_up at net.agent_sim.answer when api=request_approval | B0 | C1 | 3 | 3 | 0 | 1053 | 1081 | 3300 |
| F7 price_up at net.agent_sim.answer when api=request_approval | B0 | C2 | 3 | 3 | 0 | 1053 | 1081 | 2444 |
| F7 price_up at net.agent_sim.answer when api=request_approval | B0 | C3 | 3 | 3 | 0 | 4252 | 4271 | 9185 |
| F7 price_up at net.agent_sim.request when api=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 47 | 48 | 41 |
| F7 price_up at net.agent_sim.request when api=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 47 | 48 | 33 |
| F7 price_up at net.agent_sim.request when api=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 47 | 48 | 41 |
| F7 price_up at net.agent_sim.request when api=submit_purchase_order | B0 | C3 | 3 | 3 | 0 | 3184 | 3247 | 6839 |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 57 | 72 | 17 |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 57 | 72 | 17 |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 57 | 72 | 21 |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 4553 | 4595 | 7710 |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G2 | C0 | 3 | 3 | 0 | 57 | 58 | 16 |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 57 | 58 | 18 |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 57 | 58 | 16 |
| F7 price_up at net.gw_sim.request when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 4788 | 4884 | 7822 |
| F9 different_args at agent.step when worker=agent-1 | B0 | C0 | 3 | 3 | 0 | 3238 | 3279 | 11509 |
| F9 different_args at agent.step when worker=agent-1 | B0 | C1 | 3 | 3 | 0 | 3238 | 3279 | 13523 |
| F9 different_args at agent.step when worker=agent-1 | B0 | C2 | 3 | 3 | 0 | 3238 | 3279 | 11954 |
| F9 different_args at agent.step when worker=agent-1 | B0 | C3 | 3 | 3 | 0 | 3238 | 3279 | 11721 |
| F9 different_args at agent.step when worker=agent-1 | G1 | C0 | 3 | 3 | 0 | 2442 | 2460 | 7560 |
| F9 different_args at agent.step when worker=agent-1 | G1 | C1 | 3 | 3 | 0 | 2442 | 2460 | 7553 |
| F9 different_args at agent.step when worker=agent-1 | G1 | C2 | 3 | 3 | 0 | 2442 | 2460 | 7571 |
| F9 different_args at agent.step when worker=agent-1 | G1 | C3 | 3 | 3 | 0 | 2442 | 2460 | 7658 |
| F9 different_args at agent.step when worker=agent-1 | G2 | C0 | 3 | 3 | 0 | 2649 | 2667 | 7585 |
| F9 different_args at agent.step when worker=agent-1 | G2 | C1 | 3 | 3 | 0 | 2649 | 2667 | 7511 |
| F9 different_args at agent.step when worker=agent-1 | G2 | C2 | 3 | 3 | 0 | 2649 | 2667 | 7477 |
| F9 different_args at agent.step when worker=agent-1 | G2 | C3 | 3 | 3 | 0 | 2649 | 2667 | 7581 |
| F9 same_args at agent.step when worker=agent-1 | B0 | C0 | 3 | 3 | 0 | 3238 | 3279 | 11641 |
| F9 same_args at agent.step when worker=agent-1 | B0 | C1 | 3 | 3 | 0 | 3238 | 3279 | 13719 |
| F9 same_args at agent.step when worker=agent-1 | B0 | C2 | 3 | 3 | 0 | 3238 | 3279 | 11642 |
| F9 same_args at agent.step when worker=agent-1 | B0 | C3 | 3 | 3 | 0 | 3238 | 3279 | 11931 |
| F9 same_args at agent.step when worker=agent-1 | G1 | C0 | 3 | 3 | 0 | 2442 | 2460 | 7430 |
| F9 same_args at agent.step when worker=agent-1 | G1 | C1 | 3 | 3 | 0 | 2442 | 2460 | 8766 |
| F9 same_args at agent.step when worker=agent-1 | G1 | C2 | 3 | 3 | 0 | 2442 | 2460 | 7700 |
| F9 same_args at agent.step when worker=agent-1 | G1 | C3 | 3 | 3 | 0 | 2442 | 2460 | 7819 |
| F9 same_args at agent.step when worker=agent-1 | G2 | C0 | 3 | 3 | 0 | 2649 | 2667 | 7630 |
| F9 same_args at agent.step when worker=agent-1 | G2 | C1 | 3 | 3 | 0 | 2649 | 2667 | 7782 |
| F9 same_args at agent.step when worker=agent-1 | G2 | C2 | 3 | 3 | 0 | 2649 | 2667 | 7765 |
| F9 same_args at agent.step when worker=agent-1 | G2 | C3 | 3 | 3 | 0 | 2649 | 2667 | 7635 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 1019 | 1029 | 85 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 1019 | 1029 | 105 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 1019 | 1029 | 55 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | B0 | C3 | 3 | 0 | 3 | none | none | none |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 163 | 278 | 136 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 163 | 278 | 114 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 163 | 278 | 230 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 163 | 278 | 156 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G2 | C0 | 3 | 3 | 0 | 394 | 506 | 198 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 394 | 506 | 153 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 394 | 506 | 165 |
| F11 after_tool_call at agent.after_tool_call when tool=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 394 | 506 | 253 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 1019 | 1029 | 53 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 1019 | 1029 | 58 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 1019 | 1029 | 41 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | B0 | C3 | 3 | 0 | 3 | none | none | none |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 0 | 0 | 0 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 0 | 0 | 0 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 0 | 0 | 0 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 0 | 0 | 0 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G2 | C0 | 3 | 3 | 0 | 0 | 0 | 0 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 0 | 0 | 0 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 0 | 0 | 0 |
| F11 after_tool_result at agent.after_tool_result when tool=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 0 | 0 | 0 |
| F12 at net.agent_gw.request when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 4655 | 4756 | 1288 |
| F12 at net.agent_gw.request when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 4655 | 4756 | 993 |
| F12 at net.agent_gw.request when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 4655 | 4756 | 879 |
| F12 at net.agent_gw.request when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 4655 | 4756 | 1104 |
| F12 at net.agent_gw.request when api=submit_purchase_order | G2 | C0 | 3 | 3 | 0 | 4880 | 4984 | 1337 |
| F12 at net.agent_gw.request when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 4880 | 4984 | 1459 |
| F12 at net.agent_gw.request when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 4880 | 4984 | 827 |
| F12 at net.agent_gw.request when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 4880 | 4984 | 1458 |
| F12 at net.agent_sim.request when api=submit_purchase_order | B0 | C0 | 3 | 3 | 0 | 2047 | 2048 | 1309 |
| F12 at net.agent_sim.request when api=submit_purchase_order | B0 | C1 | 3 | 3 | 0 | 2047 | 2048 | 955 |
| F12 at net.agent_sim.request when api=submit_purchase_order | B0 | C2 | 3 | 3 | 0 | 2047 | 2048 | 656 |
| F12 at net.agent_sim.request when api=submit_purchase_order | B0 | C3 | 3 | 3 | 0 | 2047 | 2048 | 1250 |
| F12 at net.gw_sim.request when api=submit_purchase_order | G1 | C0 | 3 | 3 | 0 | 2230 | 2235 | 1208 |
| F12 at net.gw_sim.request when api=submit_purchase_order | G1 | C1 | 3 | 3 | 0 | 2230 | 2235 | 1371 |
| F12 at net.gw_sim.request when api=submit_purchase_order | G1 | C2 | 3 | 3 | 0 | 2230 | 2235 | 1142 |
| F12 at net.gw_sim.request when api=submit_purchase_order | G1 | C3 | 3 | 3 | 0 | 2230 | 2235 | 949 |
| F12 at net.gw_sim.request when api=submit_purchase_order | G2 | C0 | 3 | 0 | 3 | none | none | none |
| F12 at net.gw_sim.request when api=submit_purchase_order | G2 | C1 | 3 | 3 | 0 | 11938 | 11972 | 4416 |
| F12 at net.gw_sim.request when api=submit_purchase_order | G2 | C2 | 3 | 3 | 0 | 2206 | 2266 | 68 |
| F12 at net.gw_sim.request when api=submit_purchase_order | G2 | C3 | 3 | 3 | 0 | 2057 | 2058 | 36 |
| none | B0 | C0 | 0 | 0 | 0 | none | none | none |
| none | B0 | C1 | 0 | 0 | 0 | none | none | none |
| none | B0 | C2 | 0 | 0 | 0 | none | none | none |
| none | B0 | C3 | 0 | 0 | 0 | none | none | none |
| none | G1 | C0 | 0 | 0 | 0 | none | none | none |
| none | G1 | C1 | 0 | 0 | 0 | none | none | none |
| none | G1 | C2 | 0 | 0 | 0 | none | none | none |
| none | G1 | C3 | 0 | 0 | 0 | none | none | none |
| none | G2 | C0 | 0 | 0 | 0 | none | none | none |
| none | G2 | C1 | 0 | 0 | 0 | none | none | none |
| none | G2 | C2 | 0 | 0 | 0 | none | none | none |
| none | G2 | C3 | 0 | 0 | 0 | none | none | none |

## Metric totals by configuration

Each value is the sum of the metric over the configuration's runs (record 0004, Metrics). The first row gives the number of runs.

| Metric | B0 | G1 | G2 |
| --- | --- | --- | --- |
| runs | 228 | 252 | 261 |
| duplicates | 81 | 99 | 9 |
| unintended_effects | 0 | 0 | 0 |
| unauthorized_effects | 81 | 18 | 15 |
| stale_effects | 99 | 117 | 18 |
| missing_effects | 0 | 0 | 6 |
| excused_intents | 6 | 12 | 12 |
| false_done_claims | 0 | 0 | 0 |
| false_not_done_claims | 6 | 15 | 3 |
| unjustified_claims | 22 | 54 | 0 |
| unknown_claims | 8 | 6 | 18 |
| success_claimed_with_duplicate | 81 | 99 | 9 |
| human_interventions | 0 | 0 | 0 |
| approval_requests | 258 | 405 | 288 |
| approvals_decided_by_human | 0 | 0 | 0 |
| affected_intents | 216 | 240 | 249 |
| unresolved_intents | 12 | 6 | 21 |
| tokens_in | 7531212 | 5019733 | 5224096 |
| tokens_out | 307713 | 274333 | 291134 |

## Cost

Prices come from the price file dated 2026-11-17, in USD per million input tokens and per million output tokens (record 0004). A run's cost is its input tokens at its model's input price plus its output tokens at its output price, exactly. The scripted model costs nothing. A reply that reported no usage counts no tokens; a call the spend cap refused was never sent, so it counts none either. A passing run is one that evaluated all five properties, and all five hold: its verdict is pass or pass_unresolved. Cost per passing run is the cost of all the row's runs, passing or not, over its passing runs. It is exact when the division ends, and otherwise rounded to 6 significant digits and marked "about".

| Model | Configuration | Contract | runs | passing runs | tokens in | tokens out | cost (USD) | cost per passing run (USD) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| anthropic:claude-sonnet-5-5 | B0 | C0 | 57 | 18 | 1778928 | 71791 | 4.275766 | about 0.237543 |
| anthropic:claude-sonnet-5-5 | B0 | C1 | 57 | 18 | 1871707 | 74239 | 4.485804 | about 0.249211 |
| anthropic:claude-sonnet-5-5 | B0 | C2 | 57 | 18 | 1778601 | 71714 | 4.274342 | about 0.237463 |
| anthropic:claude-sonnet-5-5 | B0 | C3 | 57 | 47 | 2101976 | 89969 | 5.103642 | about 0.108588 |
| anthropic:claude-sonnet-5-5 | G1 | C0 | 63 | 21 | 1188695 | 64555 | 3.02294 | about 0.14395 |
| anthropic:claude-sonnet-5-5 | G1 | C1 | 63 | 21 | 1166366 | 64112 | 2.973852 | 0.141612 |
| anthropic:claude-sonnet-5-5 | G1 | C2 | 63 | 21 | 1188896 | 64400 | 3.021792 | about 0.143895 |
| anthropic:claude-sonnet-5-5 | G1 | C3 | 63 | 43 | 1475776 | 81266 | 3.764212 | about 0.0875398 |
| anthropic:claude-sonnet-5-5 | G2 | C0 | 63 | 48 | 1233124 | 69841 | 3.164658 | 0.065930375 |
| anthropic:claude-sonnet-5-5 | G2 | C1 | 72 | 57 | 1504441 | 84128 | 3.850162 | about 0.0675467 |
| anthropic:claude-sonnet-5-5 | G2 | C2 | 63 | 57 | 1206187 | 66930 | 3.081674 | about 0.0540645 |
| anthropic:claude-sonnet-5-5 | G2 | C3 | 63 | 63 | 1280344 | 70235 | 3.263038 | about 0.0517943 |
