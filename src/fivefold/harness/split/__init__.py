"""The split topology: the simulator and the gateway as HTTP services, for people who bring
their own agent (0009).

Record 0009's topology table: "split | Bringing your own agent: the gateway (MCP over
streamable HTTP), the simulator and Postgres run as separate Compose services |
Deterministic: No; faults still fire on counters". What runs where:

- The simulator's service (`simulator`) serves the simulator's API over HTTP: a POST to
  `/api/{call}` for each call of `fivefold.domain.api`, with the caller's credential as a
  bearer token. It runs the scripted approver and the faults that change the simulator's
  state, and, for a baseline's agent, the harness's report sink.
- The gateway's service (`gateway`) serves the gateway's tools over MCP's streamable HTTP
  transport at `/mcp`, and reaches the simulator only through the simulator's API, with
  its own credential (0008).
- The run's lifecycle (`lifecycle`) starts a run, ends it after the drain window, and
  scores it with the evaluator. `python -m fivefold.harness.split` runs it, and serves
  either service.

Each service runs on a `HostScheduler`, whose virtual time follows the host's monotonic
clock (0009). Every component reads time from it, so the gateway's, the simulator's and
the fault points' code runs as it does in the inproc topology. Each service writes the
harness's records of the traffic it sees (`hops`), and fires the plan's faults at the
points it owns, on the plan's counters (`faults`). Nothing here is deterministic.
"""

SERVICE_STOPPED = -32019
"""The JSON-RPC error code the gateway answers an agent's MCP request with once the run's
agents' phase is over, or the run has stopped.

It is an implementation-defined server error: JSON-RPC leaves -32000 to -32099 to servers,
and MCP keeps -32020 to -32099 for its own codes (mcp 2.3.0, `mcp_types/jsonrpc.py`). The
SDK uses -32000 and -32001 for its own ends of a call; -32000, `CONNECTION_CLOSED`, is how
a client reads a closed connection, so it cannot also mean that the run is over. The SDK
uses -32019 nowhere (checked in mcp 2.3.0 and mcp-types 2.3.0, and by a test)."""
