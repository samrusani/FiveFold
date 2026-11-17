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

F8, a memory correction after a proposal, comes in v0.2.

Each fault runs against four kinds of destination ([record 0005](adr/0005-tool-contract-levels.md)) and in three configurations ([record 0008](adr/0008-configurations-and-trust-boundaries.md)):

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

That gives 235 expected outcomes in [faults/expected.yaml](../faults/expected.yaml), and CI checks every one. With the scripted agent, B0 fails 45 of its 72 cells. G2 passes 71 of its 83, and the other 12 are documented limits, such as an approval revoked after the gateway's last check, while the order is already on its way. [limitations.md](limitations.md) says what each guarantee depends on.

## How the outcomes are tested

Every expected cell is a test, run from its committed seed. Record 0006 fixes no G1 outcome, so G1's cells come from its first runs, and record 0013 settles two of F10's. Hypothesis checks invariants I1 to I7 on the gateway's state machine, and on full runs of dev-001 with random fault plans (`{random: {from: [...], max: N}}`, record 0006) in every configuration and contract level: 20 runs in CI and 100 nightly.
