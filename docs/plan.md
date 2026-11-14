# Build plan

Six milestones over roughly six weeks reach a public v0.1. The first is a one-week design freeze that the maintainer owns, because agent teams build fastest against fixed contracts. Six weeks is an estimate: review time limits the pace, not the number of agents.

Status: draft, 14 Nov 2026.

## Roadmap

| Milestone | Week | Focus | Gate |
| --- | --- | --- | --- |
| M0 | 1 | Design freeze | Contracts frozen |
| M1 | 2 | Thin slice | Demo 1 runs from a seed |
| M2 | 3 | Authority and freshness | I1 to I6 pass |
| M3 | 4 | Full scenarios | All 3 demos run |
| M4 | 5 | Live study | Report drafted |
| M5 | 6 | Release v0.1 | Repo public |

After v0.1: Temporal and DBOS baselines, the MCP conformance test, the TLA+ model and run shrinking (v0.2), then receipt export (v0.3).

## Teams

| Team | Owns | First deliverable |
| --- | --- | --- |
| Maintainer (architect) | The semantics: action identity, grants, states, the five property definitions; approves every contract change | Frozen design doc |
| T1 Gateway | Gateway, state machine, recovery, policy | Recovery from a lost reply |
| T2 Simulator and faults | Simulator services, tool contracts, effects ledger, fault scheduler | Purchase-order service with fault F2 |
| T3 Evaluator | Property checks, metrics, report generator | Scoring for properties 1 and 5 |
| T4 Agents and baselines | LangGraph agent, model adapter, B0, later B1 and B2 | Agent completing demo 1 |
| T5 Viewer and docs | Viewer, README, decision records | Run timeline |
| T6 Red team (independent) | Held-out scenarios; reviews every claim in the report | 10 held-out scenarios |

Teams code only against the `docs/` contracts. Any contract change is a decision record that the maintainer approves. T6 keeps held-out scenarios in a separate private location that the other teams never read.

## Exit criteria

1. **M0 Design freeze.** Decision records cover action identity, grant lifecycle, state machine, property definitions, tool contracts and the fault catalog. Repo scaffold, CI and Docker Compose work.
2. **M1 Thin slice.** One agent, submit_purchase_order, fault F2, gateway states and recovery, evaluator properties 1 and 5. Demo 1 runs from a seed; B0 duplicates the order and G2 does not.
3. **M2 Authority and freshness.** Grants, approvals, recorded reads, contract levels C0 to C3, faults F6, F7, F9 and F11, properties 2 to 4. Hypothesis tests for I1 to I6 pass.
4. **M3 Full scenarios.** Demos 2 and 3, the remaining faults, recorded playback, viewer v1 and T6's held-out set. AliceMemory adapter for F8 if chosen for v0.1.
5. **M4 Study.** Pilot 10 to 20 live episodes, set the spend cap, run B0, G1 and G2 across contract levels, draft the report.
6. **M5 Release v0.1.** README with a two-minute and a twenty-minute path, a three-minute video, the report with raw data, a limitations section and full citations.

## First-week tickets

- [ ] Maintainer: write the action contract: what one logical action is, the action_key fields, what a grant permits (drafted as decision record 0001, Proposed)
- [ ] Maintainer: define the five properties as checks over ground truth (drafted as decision record 0004, Proposed)
- [ ] Maintainer: choose the name and license; check GitHub, PyPI and npm
- [ ] T1: gw schema with migrations; action and grant tables with unique constraints
- [ ] T1: state machine module with the transition table and I1 to I6 as assertions
- [ ] T2: purchase-order service with a C0 to C3 contract switch and the effects ledger
- [ ] T2: fault scheduler that fires F2 on an event counter
- [ ] T3: evaluator that reads the sim ledger and scores properties 1 and 5 to JSON
- [ ] T4: LangGraph agent for the procurement task over gateway tools, plus the B0 variant
- [ ] T5: repo scaffold, CI with pytest and Hypothesis, Docker Compose, a `make demo` stub
- [ ] T6: 10 held-out scenarios in a location the other teams cannot see

## Budget

- **GPUs:** none. Local models run on contributors' own machines through Ollama.
- **Live model spend:** set the cap after the pilot, which measures the real cost per episode. Runs needed = scenarios × configs × trials, for example 30 × 3 × 5 = 450.
- **Hosting:** none; everything runs locally.

## Risks

| Risk | Mitigation |
| --- | --- |
| LIMBO's authors release overlapping code | Ship v0.1 quickly; cite and integrate it rather than compete |
| The evidence is preprints, several single-author | Cite full titles and IDs, label them as preprints, reproduce key findings yourself |
| Agents overfit the development scenarios | T6 owns a held-out set the other teams never see |
| Scope creep into frameworks, routers or receipts | The non-goals list; any addition needs a decision record |
| Noisy live results | Five trials per cell; publish denominators and spread |
| Race windows on C0 to C2 destinations | Document them as limits; C3 closes them in the simulator |

## Launch checklist

- [ ] Demo 1 reproduces from the README in under 15 minutes on a clean machine
- [ ] Report includes raw results, denominators, configurations and model versions
- [ ] Limitations section states what each guarantee depends on
- [ ] Citations give full titles and arXiv IDs, labelled as preprints
- [ ] Report shared with the cited papers' authors; MCP issue #3394 gets a link once the conformance test exists
- [ ] Open-source files in place: LICENSE, CODE_OF_CONDUCT and a security policy
- [ ] Docs and git history audited for secrets and personal identifiers
