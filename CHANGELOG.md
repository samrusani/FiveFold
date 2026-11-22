# Changelog

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
