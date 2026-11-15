"""Metrics of record 0004 that belong to no one property: what humans did in the run.

- `human_interventions`: attestations by the scripted resolver in `harness.actor_log`.
- `approval_requests`: approval requests the approvals service filed, one row of
  `sim.approval` each. A repeated request with a client request key already used files
  nothing (0002, rule 4), so it is not counted again.
- `approvals_decided_by_human`: requests decided, approved or denied, by anyone but
  policy.

Each property's own metrics are in its module. Recovery latency, and tokens and cost, are
not computed yet.
"""

from __future__ import annotations

from fivefold.evaluator.records import ATTEST, RESOLVER, RunRecords

RUN_METRICS = ("human_interventions", "approval_requests", "approvals_decided_by_human")
"""The run's own metrics, in the order of record 0004's list."""


def run_metrics(records: RunRecords) -> dict[str, int]:
    """The run's own metrics, from the harness's actor log and the approvals service."""
    attestations = sum(
        1
        for action in records.actor_actions
        if action.actor == RESOLVER and action.action == ATTEST
    )
    by_human = sum(
        1
        for approval in records.approvals
        if approval.decision is not None and not approval.decided_by_policy
    )
    return {
        "human_interventions": attestations,
        "approval_requests": len(records.approvals),
        "approvals_decided_by_human": by_human,
    }
