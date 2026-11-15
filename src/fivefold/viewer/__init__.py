"""The viewer (0012): a read-only API over the run registry and the run databases, and
the React frontend it serves.

- `source`: where the viewer reads, always as `ff_viewer`, in read-only transactions;
- `records`: what it reads from a run database: the run, its scorecard, its timeline's
  events, its ledger and fact history;
- `timeline`: how the timeline orders the harness's and the gateway's events, and marks
  the ones that share a virtual time;
- `app`: the HTTP API and the built frontend; `python -m fivefold.viewer` serves both.

The viewer never runs a model, never writes, and reads only the run registry and, in a
run database, the schemas `ff_viewer` may select from: `sim`, `gw`, `harness` and `eval`
(0008). Like the evaluator, it reads other components' tables, never their code.
"""
