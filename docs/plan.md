# Build plan

Six milestones over roughly six weeks reach a public v0.1. The first is a one-week design freeze that the maintainer owns, because agent teams build fastest against fixed contracts. Six weeks is an estimate: review time limits the pace, not the number of agents.

Status: M0 done, 14 Nov 2026; M1 done, 15 Nov 2026; M2 done, 15 Nov 2026; M3 done but for T6's held-out set (viewer v1 is in, record 0012); M4 started (G1, the evaluator's report with tokens, cost and recovery latency, record 0014, the live model adapter with Ollama, Anthropic, OpenAI and Google, and the study runner are in; the live study waits for the maintainer's models, keys, prices and spend cap); M5 started (the README's two paths, the limitations page, the security policy, the code of conduct and an installable package are in). The conduct contact is a placeholder. Records 0015 and 0016 are accepted. The spec's admin API over HTTP, experimental branching and split topology are in too. Milestone contents follow decision record 0006.

## Roadmap

| Milestone | Week | Focus | Gate |
| --- | --- | --- | --- |
| M0 | 1 | Design freeze | Contracts frozen |
| M1 | 2 | Thin slice | Demo 1 runs from a seed |
| M2 | 3 | Authority and freshness | I1 to I7 pass |
| M3 | 4 | Full scenarios | All 3 demos run |
| M4 | 5 | Live study | Report drafted |
| M5 | 6 | Release v0.1 | v0.1 released |

After v0.1: Temporal and DBOS baselines, the MCP conformance test, the TLA+ model, run shrinking and the AliceMemory adapter for F8 (v0.2), then receipt export (v0.3).

## Teams

| Team | Owns | First deliverable |
| --- | --- | --- |
| Maintainer (architect) | The semantics: action identity, grants, states, the five property definitions; approves every contract change | Frozen design doc |
| T1 Gateway | Gateway, state machine, recovery, grants, admin API | Recovery from a lost reply |
| T2 Simulator, harness and faults | Simulator services, approvals service, tool contracts, effects ledger, the harness (scheduler, virtual clock, transports) and the fault scheduler | Purchase-order service with fault F2 |
| T3 Evaluator | Property checks, metrics, report generator | Scoring for properties 1 and 5 |
| T4 Agents and baselines | LangGraph agent, model adapter, B0, later B1 and B2 | Agent completing demo 1 |
| T5 Viewer and docs | Viewer, README, decision records | Run timeline |
| T6 Red team (independent) | Held-out scenarios; reviews every claim in the report | 10 held-out scenarios |

Teams code only against the `docs/` contracts. Any contract change is a decision record that the maintainer approves. T6 keeps held-out scenarios in a separate private location that the other teams never read.

## Exit criteria

1. **M0 Design freeze.** Decision records cover action identity, grant lifecycle, state machine, property definitions, tool contracts and the fault catalog (done: 0001 to 0009 accepted on 14 Nov 2026). Repo scaffold, CI and Docker Compose work (done: merged on 14 Nov 2026).
2. **M1 Thin slice.** One agent with the scripted model, submit_purchase_order at contract level C2, approval by policy with grant binding, recorded reads, faults F2 and F11, the deterministic scheduler and virtual clock, gateway states and recovery, evaluator properties 1 and 5. Demo 1 runs from a seed; B0 places a second order and G2 does not.
3. **M2 Authority and freshness.** The scripted approver, revocation, expiry and stale-input rejections, contract levels C0 to C3, faults F1, F6, F7, F9 and F12, properties 2 to 4. Hypothesis tests for I1 to I7 pass.
4. **M3 Full scenarios.** Demos 2 and 3, faults F3, F4, F5 and F10, recorded playback, viewer v1 (its tooling gets its own decision record) and T6's held-out set.
5. **M4 Study.** Pilot 10 to 20 live episodes, set the spend cap, run B0, G1 and G2 across contract levels, draft the report.
6. **M5 Release v0.1.** README with a two-minute and a twenty-minute path, a three-minute video, the report with raw data, a limitations section and full citations.

## First-week tickets

- [x] Maintainer: write the action contract: what one logical action is, the action_key fields, what a grant permits (decision records 0001 and 0002)
- [x] Maintainer: define the five properties as checks over ground truth (decision record 0004)
- [x] Maintainer: choose the name and license (FiveFold, Apache-2.0)
- [x] T1: gw schema with migrations; action and grant tables with unique constraints, and the Postgres store tested against the in-memory one
- [x] T1: state machine module with the transition table and I1 to I7 as assertions, tested by Hypothesis in memory and against PostgreSQL
- [x] T2: purchase-order service with the idempotency_key capability (C2), the effects ledger and fact history, plus policy approvals in the approvals service
- [x] T2: deterministic scheduler with the virtual clock and in-process transports, tested for ordering, timers, crash and restart
- [x] T2: fault scheduler that fires F2 on an event counter
- [x] T3: evaluator that reads the sim ledger and the harness message log, and scores properties 1 and 5 to JSON
- [x] T4: LangGraph agent for the procurement task over gateway tools, the B0 variant and the scripted model
- [x] T5: repo scaffold, CI with pytest and Hypothesis, Docker Compose, a `make demo` stub
- [ ] T6: 10 held-out scenarios in a location the other teams cannot see

## Budget

- **GPUs:** none. Local models run on contributors' own machines through Ollama.
- **Live model spend:** set the cap after the pilot, which measures the real cost per episode. Runs needed = scenarios × configs × contract levels × trials, for example 30 × 3 × 4 × 5 = 1,800.
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
| The deterministic scheduler around LangGraph and MCP is new work | Build it first in M1, with tests for ordering, timers, crash and restart; keep the split topology for real processes |

## Launch checklist

- [ ] Demo 1 reproduces from the README in under 15 minutes on a clean machine
- [ ] Report includes raw results, denominators, configurations and model versions
- [x] Limitations section states what each guarantee depends on
- [x] Citations give full titles and arXiv IDs, labelled as preprints
- [x] Every figure quoted from a preprint is checked against the paper; unconfirmed citations are fixed or removed
- [ ] Report shared with the cited papers' authors; MCP issue #3394 gets a link once the conformance test exists
- [ ] Open-source files in place: LICENSE, CODE_OF_CONDUCT and a security policy
- [x] Docs and git history audited for secrets and personal identifiers
