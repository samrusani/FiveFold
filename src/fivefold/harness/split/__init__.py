"""The split topology: the simulator and the gateway as HTTP services, for people who bring
their own agent (0009).

Record 0009's topology table: "split | Bringing your own agent: the gateway (MCP over
streamable HTTP), the simulator and Postgres run as separate Compose services |
Deterministic: No; faults still fire on counters". What runs where:

- The simulator's service (`simulator`) serves the simulator's API over HTTP: a POST to
  `/api/{call}` for each call of `fivefold.domain.api`, with the caller's credential as a
  bearer token. It runs the scripted approver and the faults that change the simulator's
  state, and, for a baseline's agent, the harness's report sink.
- The run's lifecycle (`lifecycle`) starts a run, ends it after the drain window, and
  scores it with the evaluator. `python -m fivefold.harness.split` runs it, and serves
  the simulator.

Each service runs on a `HostScheduler`, whose virtual time follows the host's monotonic
clock (0009). Every component reads time from it, so the simulator's and the fault
points' code runs as it does in the inproc topology. Each service writes the
harness's records of the traffic it sees (`hops`), and fires the plan's faults at the
points it owns, on the plan's counters (`faults`). Nothing here is deterministic.
"""
