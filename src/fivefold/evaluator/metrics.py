"""Metrics of record 0004 that belong to no one property: what humans did in the run, and
the tokens its model calls used.

- `human_interventions`: attestations by the scripted resolver in `harness.actor_log`.
- `approval_requests`: approval requests the approvals service filed, one row of
  `sim.approval` each. A repeated request with a client request key already used files
  nothing (0002, rule 4), so it is not counted again.
- `approvals_decided_by_human`: requests decided, approved or denied, by anyone but
  policy.
- `tokens_in` and `tokens_out`: the input and output tokens of the run's model calls in
  `harness.model_call`, summed. A call whose reply reported no usage counts 0, as every
  scripted reply does (0009). A call the spend cap refused was never sent, so it has no
  tokens and counts 0 too.

Each property's own metrics are in its module. Recovery latency, and cost and cost per
passing run, are not computed yet.
"""

from __future__ import annotations

from fivefold.evaluator.records import ATTEST, RESOLVER, RecordsError, RunRecords

RUN_METRICS = ("human_interventions", "approval_requests", "approvals_decided_by_human")
"""The run's own counts of human actions and approvals, in the order of record 0004's
list."""

TOKEN_METRICS = ("tokens_in", "tokens_out")
"""The tokens of the run's model calls, in the order of record 0004's list."""


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


def token_metrics(records: RunRecords) -> dict[str, int]:
    """The tokens of the run's model calls, in and out, from the harness's model-call log.

    Raises `RecordsError` for a call that breaks the log's rules: a call of another model
    than the run's, since the report prices every token of a run at its model's price; a
    negative token count; or a refused call with tokens.
    """
    for call in records.model_calls:
        if call.model != records.run.model:
            raise RecordsError(
                f"model call {call.call_no} names the model {call.model}, but the run's model"
                f" is {records.run.model}"
            )
        tokens = [count for count in (call.input_tokens, call.output_tokens) if count is not None]
        if any(count < 0 for count in tokens):
            raise RecordsError(f"model call {call.call_no} has a negative token count")
        if call.refused is not None and tokens:
            raise RecordsError(
                f"model call {call.call_no} was refused ({call.refused}), so it was never sent,"
                " but it has tokens"
            )
    return {
        "tokens_in": sum(call.input_tokens or 0 for call in records.model_calls),
        "tokens_out": sum(call.output_tokens or 0 for call in records.model_calls),
    }
