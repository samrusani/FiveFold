# Faults, contract levels and configurations

What FiveFold breaks, what each destination offers, and what sits between the agent and the destination.

Eleven faults from the catalog in [record 0006](adr/0006-fault-catalog.md). Each one fires at a named point when an event counter reaches its value, never on a timer, so every rerun fires it at the same moment.

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

F8, a memory correction after a proposal, is planned for a later 0.2 release.

Each applicable fault runs against four kinds of destination ([record 0005](adr/0005-tool-contract-levels.md)) and four configurations ([record 0008](adr/0008-configurations-and-trust-boundaries.md) and [record 0018](adr/0018-temporal-baseline.md)):

| Contract level | What the destination offers |
| --- | --- |
| C0 | Nothing: no idempotency key, no status lookup |
| C1 | A status lookup |
| C2 | Idempotency keys |
| C3 | Idempotency keys, and a conditional write: the destination checks the approval and the facts when it commits |

| Configuration | Between the agent and the destination |
| --- | --- |
| B0 | The LangGraph agent calls the API directly |
| B1 | The Temporal agent calls the API directly. At C2 and C3 each write Activity has a key that stays the same on retry. |
| G1 | An approval gate: one approval request per write call, checked before each send |
| G2 | The reference gateway |

CI checks 307 expected outcomes in [faults/expected.yaml](../faults/expected.yaml). The original 235 cover B0, G1 and G2. B1 adds 72, with 34 pass, 34 fail and 4 pass unresolved. B1 uses B0's direct fault points, so the gateway hops and the F10 status-answer faults do not apply. With the scripted agent, B0 fails 45 of its 72 cells. G2 passes 71 of its 83, and the other 12 are documented limits, such as an approval revoked after the gateway's last check, while the order is already on its way. [limitations.md](limitations.md) says what each guarantee depends on.

## How the outcomes are tested

Every expected cell is a test, run from its committed seed. Record 0006 fixes no G1 outcome, so G1's cells come from its first runs, and record 0013 settles two of F10's. Hypothesis checks invariants I1 to I7 on the gateway's state machine, and on full runs of dev-001 with random fault plans (`{random: {from: [...], max: N}}`, record 0006) in B0, G1 and G2 at every contract level: 20 runs in CI and 100 nightly.

B1 also compares the digests of all playback records across normal execution and host pauses for selected cells in CI. After provisioning PostgreSQL, use these commands to repeat the comparison for all 72 cells.

```sh
make temporal-setup
FIVEFOLD_B1_REPEATS=20 uv run pytest tests/pg/test_b1.py
```

Each cell runs 10 times normally, five times with a 150 ms asynchronous pause and five times with a 150 ms blocked poller. The check uses the same wall-clock exclusions as playback. Set `FIVEFOLD_B1_DIGEST_LOG` to a fresh file path to save each cell's record digests.
