# Changelog

## 0.2.1 (27 Nov 2026)

A patch release with fixes from the v0.2.0 audit. Runs require Python 3.12 and PostgreSQL 16.

### Security

- The gateway verifies that approval responses match the request. Split simulator replies must match the logged response and its correlation before the gateway uses them. Component errors expose their class to the agent and keep details in Python logs. [#157](https://github.com/samrusani/FiveFold/pull/157) · [#169](https://github.com/samrusani/FiveFold/pull/169)
- The split services refuse unsupported message bodies before recording them, enforce their caller boundary and ignore proxy settings from the environment. [#161](https://github.com/samrusani/FiveFold/pull/161)
- Database setup checks ownership and each database's migration history before adopting an unmarked database. It pins SQL function search paths and preserves explicit connection verification settings for component roles. [#160](https://github.com/samrusani/FiveFold/pull/160)
- Live runs reject ambient LangSmith routing and tracing. A paid attempt with unknown spend closes the cap. Provider SDK retries are disabled. [#167](https://github.com/samrusani/FiveFold/pull/167)
- The viewer accepts loopback Host headers and explicitly allowed names. Its database reads have a statement timeout. [#166](https://github.com/samrusani/FiveFold/pull/166) · [#169](https://github.com/samrusani/FiveFold/pull/169)
- The app and viewer images use pinned base images and run without root. CI actions are pinned, and publishing checks the release flag, tag, version and distribution links. [#163](https://github.com/samrusani/FiveFold/pull/163)
- Template and registry names cannot collide with each other or with another reserved FiveFold database name. [#172](https://github.com/samrusani/FiveFold/pull/172)
- B1 verifies the pinned Temporal CLI executable's SHA-256 before running it. [#172](https://github.com/samrusani/FiveFold/pull/172)

### Fixed

- The Docker build retries transient failures while downloading the checksum-pinned Temurin JRE, including TLS and HTTP protocol errors. It stops on HTTP 4xx errors other than 429. [#169](https://github.com/samrusani/FiveFold/pull/169) · [#172](https://github.com/samrusani/FiveFold/pull/172)
- Retrying split `end` can finish after both services have stopped. Split deliveries and attestations share a lock when reserving evidence timestamps. Split F5 requires 50 ms of scheduling headroom, and F4 copies still run after the original is dropped. [#161](https://github.com/samrusani/FiveFold/pull/161) · [#169](https://github.com/samrusani/FiveFold/pull/169)
- Tool calls that the MCP client cannot encode return the matching invalid-argument shape. F8 with no memory fact writes no fault row and leaves its occurrence available. Baseline cleanup preserves the scored result or the original error. [#162](https://github.com/samrusani/FiveFold/pull/162) · [#169](https://github.com/samrusani/FiveFold/pull/169)
- The MCP conformance runner keeps its 10 s connection setup limit and lets replies arrive within the configured evidence interval. [#162](https://github.com/samrusani/FiveFold/pull/162) · [#172](https://github.com/samrusani/FiveFold/pull/172)
- The shrinker checks its final database name before the first trial. [#162](https://github.com/samrusani/FiveFold/pull/162)
- Saved results validate strict integers and branch field pairs. CSV and Markdown exports escape fields that spreadsheets or Markdown could interpret, and invalid prices report one-line errors. [#164](https://github.com/samrusani/FiveFold/pull/164)
- The recorder checks that each cassette call can be encoded and read back before appending it. [#172](https://github.com/samrusani/FiveFold/pull/172)
- The API rejects integers in `Version` and `VirtualMs` that exceed the JSON encoder's limit. [#172](https://github.com/samrusani/FiveFold/pull/172)
- Reports escape free text in every table column. Saved scenario digests must be 64 lowercase hexadecimal characters. [#172](https://github.com/samrusani/FiveFold/pull/172)
- The gateway model checks reports against every action for an intent. The Python replay covers the second dispatched action from the TLC trace. [#158](https://github.com/samrusani/FiveFold/pull/158)

### Docs

- Records 0025 and 0026 are accepted. Their scoring, contract and database changes follow in later releases. [#159](https://github.com/samrusani/FiveFold/pull/159) · [#165](https://github.com/samrusani/FiveFold/pull/165)
- Documented split request limits, the live spend cap, the current quote-terms limit and the hosted Nightly model measurements. [#169](https://github.com/samrusani/FiveFold/pull/169)

## 0.2.0 (25 Nov 2026)

The first full release in the 0.2 series brings together the four pre-releases and the fixes below. Runs require Python 3.12 and PostgreSQL 16.

### Added

- B1 uses Temporal Activity retries (record 0018). Install the SDK with `fivefold[temporal]` and run `make temporal-setup` from a checkout for the pinned Temporal CLI. B2 uses DBOS Step retries (record 0020) on Linux and macOS with `fivefold[dbos]`. Both baselines are scripted. [Study guide](docs/studies.md)
- The MCP lost-reply conformance command checks for duplicate effects after a retry (record 0019). The repository includes synthetic fixtures for stdio and Streamable HTTP. [Conformance guide](docs/conformance.md)
- The bounded TLA+ model checks the G2 gateway in CI and `make check`. Contributors need Java 17 and `make tla-setup` for the verified TLC jar (record 0021). The model and TLC are development tools outside the installed package. [Model bounds](docs/limitations.md#the-bounded-gateway-model)
- Run shrinking finds and verifies a smaller fault plan for a scored, in-process scripted B0, G1 or G2 failure (record 0022). [Shrinking guide](docs/shrinking.md)
- F8 tests a memory correction after a proposal. It adds 80 expected outcomes on `dev-001-memory-po`, for 459 across both matrices. The optional AliceMemory exercise uses an operator's disposable local instance with `FIVEFOLD_ALICEMEMORY=1` and runs outside CI (record 0023). [Fault catalog](docs/faults.md#f8-in-the-memory-scenario) · [AliceMemory guide](docs/alicememory.md)

### Changed

- `make check` requires Temporal CLI 1.10.1, Java 17 and TLC. Run `make temporal-setup` for the pinned CLI and `make tla-setup` for the verified TLC jar.
- The app image bundles a Temurin 17 runtime, the TLC jar and the Temporal CLI.
- Database setup adds the `ff_dbos` role. Set `FIVEFOLD_DBOS_PASSWORD` on servers that require a password for this role.
- Memory-enabled scenarios add the `get_memory` tool, the `memory_dependency` scenario field and `memory/{supplier_id}/{sku}` facts.
- The split topology refuses memory-enabled scenarios.

### Fixed

- B1's handoff wait covers the full Temporal Activity retry backoff and a scheduling margin. An expired handoff identifies the Activity ID and attempt and stops the run before scoring (#124).
- The conformance and shrink commands print `python -m fivefold.conformance` and `python -m fivefold.shrink` on their respective usage lines (#128, #142).
- The MCP conformance runner reports a rejected first write as a fixture error before dropping its reply (#151).
- The shrinker explains why split runs are unsupported before checking the model adapter (#152).
- The study command rejects unsupported selections and missing baseline prerequisites before provisioning a database. Baseline infrastructure errors name the episode when available. Unknown selectors name the scenario's expected-outcome file (#154).
- The AliceMemory command prints help without loading exercise settings. `make tla-setup` reports download and checksum errors directly (#154).

### Docs

- Updated the fixture and study guides, baseline setup and cleanup, and TLC measurements. The AliceMemory guide uses v0.25.0 (#153).

## 0.2.0a4 (24 Nov 2026)

### Added

- F8 tests a memory correction after a proposal on `dev-001-memory-po` (record 0023). The scenario uses in-repo memory and checks 80 expected outcomes across five configurations and four destination contract levels.
- Run the optional AliceMemory exercise with `python -m fivefold.harness.alicememory`. It runs only with `FIVEFOLD_ALICEMEMORY=1` against an operator's disposable local instance and never in CI. [AliceMemory guide](docs/alicememory.md) · [Record 0023](docs/adr/0023-memory-correction.md)

### Docs

- The [fault catalog](docs/faults.md#f8-in-the-memory-scenario) describes the four F8 variants and their fault points. The [study guide](docs/studies.md) shows how to run the matrix, and the [limitations](docs/limitations.md#known-limits) explain when a late correction can still leave a stale write.

## 0.2.0a3 (24 Nov 2026)

### Added

- Run shrinking. Use `python -m fivefold.shrink` to find and verify a smaller fault plan for a scored, in-process scripted B0, G1 or G2 failure (record 0022).
- A bounded TLA+ model of the G2 gateway. CI and `make check` run the bounded profile. The model and TLC are development tools outside the installed package (record 0021).

### Docs

- Added the [run-shrinking walkthrough](docs/shrinking.md) and documented the [bounds of the gateway model](docs/limitations.md#the-bounded-gateway-model). F8 remains planned for a later 0.2 release under accepted record 0023.

## 0.2.0a2 (23 Nov 2026)

### Added

- B2, the scripted DBOS baseline (record 0020), runs on Linux and macOS. Install the optional SDK with `fivefold[dbos]`. At C2 and C3, retries of the same write Step use its original idempotency key.

### Fixed

- `python -m fivefold.conformance --help` prints the module name in its usage line (#128).

### Docs

- The tour compares B0 and B2 on a lost reply. The deterministic suite now checks 379 expected outcomes. This pre-release ships B1, B2 and the MCP lost-reply conformance test. The TLA+ model, run shrinking and F8 are planned for later 0.2 releases.

## 0.2.0a1 (22 Nov 2026)

### Added

- B1, the scripted Temporal baseline. Install the SDK with `fivefold[temporal]`. B1 needs a repository checkout and `make temporal-setup` to install the pinned Temporal CLI.
- MCP lost-reply conformance. Run `python -m fivefold.conformance` from the installed package. The repository contains the example fixture servers and the [two-minute walkthrough](docs/conformance.md).

### Fixed

- B1's handoff wait covers Temporal's full Activity retry backoff and a scheduling margin. A handoff that exceeds the bound raises `TemporalInfrastructureError` with the Activity ID and attempt and stops the run before scoring (#124).

### Docs

- Records 0018 and 0019 define B1 and the conformance test. Record 0020 (accepted) defines B2 for a later release.

## 0.1.2 (18 Nov 2026)

### Fixed

- Split G1 and G2 workers open a fresh MCP session when the gateway reports that the previous one is gone. G2 attaches an identical retry to the same action. G1 follows its timeout retry rules. A write whose reply was lost can be placed twice, as specified in record 0008. [Tracking issue #101](https://github.com/samrusani/FiveFold/issues/101)
- The viewer suggests B0 and G2 pairs only for runs from the same experiment cell. Any two runs can still be compared manually.

### Docs

- Record 0017 (accepted): the approval wait ends with the first status poll the gateway reads at or after the deadline. A regression test pins it (#114).

## 0.1.1 (18 Nov 2026)

### Security

- Demos replace their own databases on rerun. Recorder, playback, branching and split start require `--replace`. Replacement requires FiveFold's marker or its complete matching migration history. Playback validates the original run before provisioning.
- Evaluator CSVs mark formula-like values in text columns with a leading apostrophe.
- Split start creates a fresh owner-only run directory and creates each token file exclusively. A failed start removes the files it created and the directory if it is empty.
- Worker checkpoints use an explicit allowlist for message types.

### Docs

- The README compares configurations over the same fault cells (#102).

### Known issue

A split worker can stop with `MCPError: Session not found` when it loses its MCP session. Fixed in 0.1.2. [Tracking issue #101](https://github.com/samrusani/FiveFold/issues/101)

## 0.1.0 (18 Nov 2026)

FiveFold's first full release is a test suite and reference gateway for agents that take actions in a procurement simulator. It scores five properties from the simulator ledger and harness records.

- **M0, design freeze.** Defined action identity, grants, the gateway state machine, the five properties, tool contracts and the fault catalog. Set up CI and Docker Compose.
- **M1, thin slice.** Added the first scripted LangGraph agent, a deterministic scheduler and the reference gateway. Demo 1 shows what happens when a purchase-order reply is lost.
- **M2, authority and freshness.** Added the scripted approver, the C0 to C3 contract levels, revocation, expiry and stale-input checks. The evaluator covers all five properties, and Hypothesis checks invariants I1 to I7.
- **M3, full scenarios.** Added demo 2a (a quote changes after approval, F7), demo 2b (an approval is revoked, F6), and demo 3 (an outcome nobody can confirm, F2 with no resolver). The full scenario suite covers 11 fault types across 235 checked cells. Added recorded playback and an evidence viewer with timelines and scorecards.
- **M4, live study.** Published the local and API pilots and the full study of 1,482 live episodes. The [report](docs/report.md) links to the raw data and compares the baseline, approval gate and reference gateway.
- **M5, release.** Published the Python package, the two-minute setup and [twenty-minute tour](docs/tour.md), the overview video, the [limitations](docs/limitations.md) and the [security policy](SECURITY.md).

The release also includes the admin API for resolving unknown outcomes, experimental branching, the split topology for bringing your own agent, and live adapters for Ollama, Anthropic, OpenAI and Google.

### Known issue

In the split topology, an agent worker can end with `MCPError: Session not found` when its MCP session is lost. This happened once in about 40 CI runs. All 30 targeted runs passed. The demos, the 235 expected outcomes, and the full study use the in-process topology, which uses a separate transport. [Tracking issue #101](https://github.com/samrusani/FiveFold/issues/101)

Fixed in 0.1.2. G1 recovers from session loss in the same way it recovers from a timeout. A write whose reply was lost can be placed twice, as specified by record 0008. G2 attaches an identical proposal to the same action.

## 0.1.0a1 (17 Nov 2026)

First PyPI pre-release. It packaged the demo and its scenario and migration data so demo 1 could run from an installed wheel.
