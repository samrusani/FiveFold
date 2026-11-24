# Faults, contract levels and configurations

What FiveFold breaks, what each destination offers, and what sits between the agent and the destination.

Twelve faults from the catalog in [record 0006](adr/0006-fault-catalog.md) and the F8 addition in [record 0023](adr/0023-memory-correction.md). Each fires at a named point on an event counter, so a rerun with the same inputs fires it at the same virtual time.

| Fault | What goes wrong |
| --- | --- |
| F1 | A crash just before the request goes out |
| F2 | The reply is lost after the action commits |
| F3 | A crash after the reply arrives, before it is recorded |
| F4 | The same request is delivered twice |
| F5 | The action commits late, after the caller has timed out |
| F6 | The approval is revoked before the action commits |
| F7 | The quote changes after the approval |
| F8 | The memory that an order depends on changes after the proposal |
| F9 | Two workers pick up the same task |
| F10 | Status lookups answer late, or with stale data |
| F11 | The agent is killed and resumed from its checkpoint |
| F12 | The request is lost before it reaches the destination |

Each applicable fault runs against four kinds of destination ([record 0005](adr/0005-tool-contract-levels.md)) and five configurations ([record 0008](adr/0008-configurations-and-trust-boundaries.md), [record 0018](adr/0018-temporal-baseline.md) and [record 0020](adr/0020-dbos-baseline.md)):

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
| B2 | The DBOS agent calls the API directly through the harness. At C2 and C3 each write Step has a key that stays the same on retry. |
| G1 | An approval gate: one approval request per write call, checked before each send |
| G2 | The reference gateway |

CI checks 379 expected outcomes on dev-001 in [faults/expected.yaml](../faults/expected.yaml). The original 235 cover B0, G1 and G2. B1 and B2 add 72 each. Each durable baseline passes 34, fails 34 and passes 4 unresolved. Both use B0's direct fault points, so the gateway hops and the F10 status-answer faults do not apply. On dev-001, B0 fails 45 of its 72 cells. G2 passes 71 of its 83, and the other 12 are documented limits, such as an approval revoked after the gateway's last check, while the order is already on its way. [limitations.md](limitations.md) says what each guarantee depends on.

## F8 in the memory scenario

`dev-001-memory-po` adds the selected supplier's order channel as an exact
dependency. A relevant correction changes `order_channel` from `edi` to
`portal`. A harmless correction changes `notes` only. The correction fires
once at occurrence 1, before the scheduler delivers the next message.

| Variants | B0, B1 and B2 | G1 and G2 |
| --- | --- | --- |
| `early_relevant`, `early_harmless` | `net.agent_sim.answer` on the approved `request_approval` reply | `gw.dispatch.before_check` before the gateway's send check |
| `late_relevant`, `late_harmless` | `net.agent_sim.request` as the purchase order leaves for the simulator | `net.gw_sim.request` after the gateway records its attempt |

The [F8 matrix](../faults/expected-f8.yaml) covers four variants at C0 to
C3 in all five configurations. G2 catches an early relevant correction
and rereads. A late relevant correction can still commit on stale input
at C0 to C2. At C3 the destination catches it at commit. Every harmless
case passes. F8 uses a fixed in-process plan. [Record 0023](adr/0023-memory-correction.md)

The [AliceMemory guide](alicememory.md) shows how to run the optional
exercise on a disposable local instance.

## How the outcomes are tested

Every expected cell is a test, run from its committed seed. Record 0006 fixes no G1 outcome, so G1's cells come from its first runs, and record 0013 settles two of F10's. Hypothesis checks invariants I1 to I7 on the gateway's state machine, and on full runs of dev-001 with random fault plans (`{random: {from: [...], max: N}}`, record 0006) in B0, G1 and G2 at every contract level: 20 runs in CI and 100 nightly.

The 80 F8 cells run twice and compare playback record digests. B1 also
compares records across normal execution and host pauses for selected
dev-001 cells in CI. After provisioning PostgreSQL, use these commands
to repeat the comparison for all 88 B1 cells (72 dev-001 and 16 F8).

```sh
make temporal-setup
FIVEFOLD_B1_REPEATS=20 uv run pytest tests/pg/test_b1.py
```

Each cell runs 10 times normally, five times with a 150 ms asynchronous pause and five times with a 150 ms blocked poller. The check uses the same wall-clock exclusions as playback. Set `FIVEFOLD_B1_DIGEST_LOG` to a fresh file path to save each cell's record digests.

B2 checks all playback records the same way. CI repeats four cells under
host pauses. On Linux or macOS, repeat all 88 cells from a development checkout
after provisioning PostgreSQL.

```sh
FIVEFOLD_B2_REPEATS=20 uv run pytest tests/pg/test_b2.py
```

Each cell runs 10 times normally, five times with a 150 ms asynchronous pause
and five times with the DBOS child stopped for 150 ms. Set
`FIVEFOLD_B2_DIGEST_LOG` to a fresh file path to save the comparison digests.
