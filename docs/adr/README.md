# Decision records

One file per decision, numbered in order. Copy `0000-template.md` to start a new one.

Status is **Proposed** until the maintainer marks it **Accepted**. Agents never accept their own records.

## Records for M0 (design freeze)

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-action-identity.md) | Action identity: intents, the action key, matching proposals to actions | Proposed |
| [0002](0002-authority-approvals-and-grants.md) | Authority: the approvals service as system of record, and the grant lifecycle | Proposed |
| [0003](0003-action-state-machine.md) | Action state machine, recovery, what the agent is told, invariants I1 to I7 | Proposed |
| [0004](0004-five-properties.md) | The five properties as checks over ground truth; verdicts, metrics and the report schema | Proposed |
| [0005](0005-tool-contract-levels.md) | Tool contract levels C0 to C3: capabilities, parameters, gateway behaviour, guarantees | Proposed |
| [0006](0006-fault-catalog.md) | Fault catalog F1 to F12, fault points, expected outcomes and demos | Proposed |
| [0007](0007-tooling.md) | Tooling: Python, uv, Ruff, mypy, psycopg, migrations, CI, layout | Proposed |
| [0008](0008-configurations-and-trust-boundaries.md) | Configurations B0, G1 and G2, the scripted policy, trust boundaries and database roles | Proposed |
| [0009](0009-runs-and-deterministic-execution.md) | Runs, the scenario format, the deterministic scheduler, the virtual clock, replay and branching | Proposed |

The original M0 list had seven records. Records 0008 and 0009 were added because some review questions did not fit 0001 to 0007. [m0-review-decisions.md](m0-review-decisions.md) maps each of the 44 review questions to its decision, lists every change to items the maintainer owns, and lists the 17 fixes made after an independent review of the drafts.

## Suggested review order

1. 0004 (the five properties), because every other record serves it.
2. 0003 and 0001 (states and identity).
3. 0002 and 0005 (authority and contracts).
4. 0006 (faults and expected outcomes).
5. 0008 and 0009 (configurations and runs).
6. 0007 (tooling), which unblocks the repository scaffold.

When a record is accepted, the matching sections of `docs/spec.md`, `docs/product.md`, `docs/plan.md` and `CLAUDE.md` are updated in the same pull request. Each record lists its "Spec changes on acceptance".
