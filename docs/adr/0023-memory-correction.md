# 0023: Memory correction after a proposal

Status: Accepted on 23 Nov 2026 by the maintainer
Date: 2026-11-23
Spec sections affected on acceptance: Simulator, Gateway, Fault catalog, Testing, Later releases
Other documents affected: `docs/product.md`, `docs/plan.md`, `CLAUDE.md` (Tests)

## Context

Record 0006 defers F8 and says memory entries are ordinary facts for
property 3. A remembered input can change after an agent uses it to form
an order. FiveFold can measure whether its declared dependency still
holds when the write commits. [Fault catalog](0006-fault-catalog.md)

## Decision

### The memory fact and its read

Use `dev-001-memory-po` in `scenarios/dev/dev-001-memory-po.yaml`, a
memory-enabled copy of `dev-001-single-po`. The synthetic supplier memory
for W-100 is `memory/{supplier_id}/{sku}`. The scripted agent uses
`order_channel` to check that the selected supplier accepts an ordering
channel it knows. Both `edi` and `portal` qualify. The initial value is
`edi`. A `notes` field carries a short reminder. The original development
scenario and its 379 expected cells keep their existing declarations.

For the memory-enabled scenario, declare one additional exact dependency
for the selected supplier and SKU. `memory_channel_unchanged` compares
only `order_channel`. A committed write using the old value fails property
3 even when the new channel qualifies, because the dependency is exact on
the value read, as with F7's price rise within the intent's maximum.
The existing basis check verifies that the cited version was served and
that the rule still holds. The simulator uses the same rules in C3's
commit transaction. Property 3 reads the same read log,
fact history and committed effect basis under record 0004. Select the
extra dependency from the scenario on the server, so an agent cannot
choose to omit it. Existing scenarios use their existing dependencies.
The operation's business arguments, approval scope and action key stay
as defined in records 0001 and 0002.

Add `get_memory` with `supplier_id` and `sku`. On the direct route a
successful read returns `answer: fact`, `key`, `version` and `data`. On
the gateway route it returns `read_id`, `key`, `version` and `data`, like
the other gateway reads. `data` contains `order_channel` and `notes`.
The simulator records the version it served. The gateway records a read
ID against that version and resolves it in `based_on`. B0, B1 and B2 cite
the version in their write basis. A missing key follows the existing
read-refusal shape. The tool list depends on the scenario. Both routes
list `get_memory` only in memory-enabled scenarios. The dev-001 tool list
is unchanged.

### The fault and its points

A correction appends a version at the fault's virtual time. The next
read sees that version. `relevant` changes `order_channel` to `portal`.
`harmless` changes only `notes`. Define `early_relevant`,
`early_harmless`, `late_relevant` and `late_harmless`. Each variant fires
once for the order's chosen supplier and SKU in this scenario.

| Timing | Direct route (B0, B1, B2) | Gateway route (G1, G2) |
| --- | --- | --- |
| Early | `net.agent_sim.answer` on the approved `request_approval` reply. The agent has already read and chosen the order. | `gw.dispatch.before_check` after admission and before the gateway's send check. G1's check covers approval only. |
| Late | `net.agent_sim.request` as `submit_purchase_order` leaves for the simulator. | `net.gw_sim.request` after T6 has durably recorded the attempt and before the destination receives the write. |

F8 corrects the entry through the simulator control interface. The
reference backend stores its ordered versions as in-repo simulator facts.
The interface needs a read by exact key and a correction of that key.
It represents the same two operations for a local or external backend.
A fact change and the next delivery have an order on FiveFold's virtual
clock. The gateway checks the fact via its existing simulator path.
No agent and no gateway holds AliceMemory credentials. Only the optional
adapter exercise uses them.

### AliceMemory adapter and disclosure

[AliceMemory](https://www.alicememory.com/) is the maintainer's memory
platform. Keep the interface and in-repo implementation as the reference.
Use AliceMemory as an optional HTTP adapter for a separate synthetic
round trip. It seeds an entry, reads it by ID, corrects it and reads the
old and new values from its revisions. The scored matrix runs under the
reference backend. Provider IDs, timestamps and ranking never enter its
records, scheduling or score.

The adapter uses the HTTP API in AliceMemory v0.24.1. The synthetic entry
holds a canonical JSON object with `order_channel` and `notes` as text.
The adapter validates the response against the expected fact value before
mapping it to the corresponding FiveFold version. Check response status
and ownership. A refusal or incomplete read fails the adapter exercise.

| Operation | HTTP API and fields |
| --- | --- |
| Seed | `POST /v0/vnext/memories/commit` with `user_id`, `title`, `canonical_text` and a seed-specific `idempotency_key`. Require the committed outcome and retain the returned `memory.id`. |
| Read exact entry | `GET /v0/vnext/memories/{memory_id}/audit` with `user_id` in the query. Read `memory.canonical_text` and `revisions`. |
| Correct | `POST /v0/vnext/memories/correct` with `user_id`, `memory_id`, `canonical_text` and a synthetic reason. Verify the returned committed memory and its audit revision. |

These routes and fields are documented in the [memory protocol](https://github.com/samrusani/AliceMemory/blob/v0.24.1/docs/memory-operations-protocol.md)
and implemented in the [v0.24.1 memory router](https://github.com/samrusani/AliceMemory/blob/v0.24.1/apps/api/src/alicebot_api/routers/vnext_memories.py)
and [commit service](https://github.com/samrusani/AliceMemory/blob/v0.24.1/apps/api/src/alicebot_api/vnext_memory_commit.py).
Use an unbound trusted local agent key provisioned on the disposable
instance. Audit is an operator route. The adapter reads the key from its
environment and sends it as an HTTP bearer credential. Keep the base URL
and synthetic user ID in the environment too. [Agent keys](https://github.com/samrusani/AliceMemory/blob/v0.24.1/docs/alpha/agent-integration.md)

An operator follows the FiveFold setup and cleanup guide at
`docs/alicememory.md`, to be written with F8. For each exercise the operator
provisions a disposable local AliceMemory instance. Its PostgreSQL
database, Redis instance and synthetic user form the namespace. The
operator starts the runtime on loopback with `DATABASE_URL` and
`REDIS_URL` pointing to those fresh resources, with model and embedding
providers disabled. After the exercise the operator stops the runtime and
removes its disposable database, Redis state and files. This removes the
synthetic data and retained revisions. The application's `forget` retires
a memory from recall and keeps its history. FiveFold's adapter makes only
the HTTP calls and never starts, stops or deletes AliceMemory's database,
Redis or files. [Local runtime](https://github.com/samrusani/AliceMemory/blob/v0.24.1/docs/alpha/local-runtime.md) · [Runtime settings](https://github.com/samrusani/AliceMemory/blob/v0.24.1/apps/api/src/alicebot_api/config.py)

Enable this exercise only with an explicit `FIVEFOLD_ALICEMEMORY=1` flag.
CI and deterministic tests use the in-repo backend and never contact the
adapter. The HTTP adapter can use FiveFold's existing HTTPX 0.28.1
(BSD-3-Clause). The external test runtime is AliceMemory 0.24.1 (MIT).
Both licences are Apache Category A. No new package dependency is needed.
[HTTPX licence](https://github.com/encode/httpx/blob/0.28.1/LICENSE.md) ·
[AliceMemory licence](https://github.com/samrusani/AliceMemory/blob/v0.24.1/LICENSE) ·
[Apache licence policy](https://www.apache.org/legal/resolved.html#category-a)

## Alternatives considered

- Add a memory-only freshness score. Property 3 can already compare an
  exact declared fact against the version current at commit.
- Require every scenario to read memory. This would change which inputs
  existing runs use. Use a separate memory-enabled development scenario.
- Retrieve a memory by semantic rank for the adapter. Exact ID readback
  gives evidence about the entry corrected and its revisions.
- Use AliceMemory as the reference backend. Service timing and availability
  would then affect whether the F8 result can be reproduced locally.
- Use the SQLite MCP on-ramp for the whole exercise. Its stdio launcher
  supports an isolated data directory. The committed-memory correction
  used here is documented over HTTP and the vNext CLI. Use the local
  PostgreSQL HTTP surface for the round trip. [SQLite launcher](https://github.com/samrusani/AliceMemory/blob/v0.24.1/apps/api/src/alicebot_api/onramp.py)

## Expected outcomes

All 80 cells below apply to `dev-001-memory-po`. Store them in
`faults/expected-f8.yaml` with `scenario: dev-001-memory-po`, using the
same one-scenario format as `faults/expected.yaml` (0006). Keep the
original file's `scenario` and all 379 existing cells unchanged. Each new
cell runs one F8 at occurrence 1. Direct points apply only to B0, B1 and B2.
Gateway points apply only to G1 and G2. A scenario or operation with no
memory dependency has no F8 cell. `Fail P3` means property 3 alone fails
with `stale_input`. Every cell commits one effect.

| Configuration | Contract | Early relevant | Early harmless | Late relevant | Late harmless |
| --- | --- | --- | --- | --- | --- |
| B0 | C0 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| B0 | C1 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| B0 | C2 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| B0 | C3 | Pass, C3 prevents stale commit and agent rereads | Pass, notes only | Pass, C3 prevents stale commit and agent rereads | Pass, notes only |
| B1 | C0 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| B1 | C1 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| B1 | C2 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| B1 | C3 | Pass, C3 prevents stale commit and agent rereads | Pass, notes only | Pass, C3 prevents stale commit and agent rereads | Pass, notes only |
| B2 | C0 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| B2 | C1 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| B2 | C2 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| B2 | C3 | Pass, C3 prevents stale commit and agent rereads | Pass, notes only | Pass, C3 prevents stale commit and agent rereads | Pass, notes only |
| G1 | C0 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| G1 | C1 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| G1 | C2 | Fail P3, stale channel commits | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| G1 | C3 | Pass, C3 prevents stale commit and agent rereads | Pass, notes only | Pass, C3 prevents stale commit and agent rereads | Pass, notes only |
| G2 | C0 | Pass, send check and reread | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| G2 | C1 | Pass, send check and reread | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| G2 | C2 | Pass, send check and reread | Pass, notes only | Fail P3, stale channel commits | Pass, notes only |
| G2 | C3 | Pass, send check and reread | Pass, notes only | Pass, C3 prevents stale commit and agent rereads | Pass, notes only |

## Prototype measurements

The private scratch run used the five configurations, four contracts,
four variants and seed 7. It ran each cell twice with a fresh PostgreSQL
16 database, for 160 runs. All 80 pairs matched on verdict, failure
reasons and the SHA-256 of the playback record projection, excluding
wall-clock fields. There were 53 passing cells and 27 failing cells.
Every run logged one F8 firing, exactly two versions of the affected
memory and one committed effect. The sum of execution and evidence
reading time across trials was 125.0 s. It excludes database creation
and cleanup. The ordered digest of the final matrix was
`4e81414c317d93a12a77f97a5134df8f0a83549e9ba597a58ddd24c499eb7dcc`.

The prototype reused the simulator's fact history, the direct and gateway
read routes, the existing virtual scheduler and the property 3 evaluator.
It enabled the extra dependency for purchase orders throughout its isolated
memory-only matrix. Scenario-level selection is required for the
implementation. The matrix exercised the in-repo memory. The AliceMemory
route design was checked against the pinned sources and awaits its own
isolated smoke test.

## Consequences and spec changes

- Simulator and Gateway describe the scenario's exact memory dependency
  and scenario-dependent tool list. Keep property 3's definition.
- Document the four F8 variants, route-specific points, prerequisites and
  the synthetic adapter boundary under Fault catalog.
- Testing names the separate F8 matrix. Retain the existing 379 outcomes
  and check their determinism alongside the new cells.
- Add reference-backend tests for fresh and corrected reads, both routes
  and commit behavior. Verify the optional HTTP adapter separately by
  seeding, reading and correcting a synthetic entry. Document operator
  setup and cleanup in `docs/alicememory.md`.
- Later releases, the product doc and the plan describe the in-repo
  memory as the scored F8 backend and AliceMemory as an optional exercise.
  Implementation is for a later v0.2 sprint.
