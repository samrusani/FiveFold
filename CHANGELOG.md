# Changelog

## 0.1.0 (18 Nov 2026)

FiveFold's first full release is a test suite and reference gateway for agents that take actions in a procurement simulator. It scores five properties from the simulator ledger and harness records.

- **M0, design freeze.** Defined action identity, grants, the gateway state machine, the five properties, tool contracts and the fault catalog. Set up CI and Docker Compose.
- **M1, thin slice.** Added the first scripted LangGraph agent, a deterministic scheduler and the reference gateway. Demo 1 shows what happens when a purchase-order reply is lost.
- **M2, authority and freshness.** Added the scripted approver, the C0 to C3 contract levels, revocation, expiry and stale-input checks. The evaluator covers all five properties, and Hypothesis checks invariants I1 to I7.
- **M3, full scenarios.** Added the stale-approval and unknown-outcome demos, 11 fault types across 235 checked cells, recorded playback and an evidence viewer with timelines and scorecards.
- **M4, live study.** Published the local and API pilots and the full study of 1,482 live episodes. The [report](docs/report.md) links to the raw data and compares the baseline, approval gate and reference gateway.
- **M5, release.** Published the Python package, the two-minute setup and [twenty-minute tour](docs/tour.md), the overview video, the [limitations](docs/limitations.md) and the [security policy](SECURITY.md).

## 0.1.0a1 (17 Nov 2026)

First PyPI pre-release. It packaged the demo and its scenario and migration data so demo 1 could run from an installed wheel.
