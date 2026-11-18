# Bring your own agent (split topology)

The split topology runs the gateway and the simulator as separate services, so you can point your own agent at them (record 0009). It is not deterministic: virtual time follows the host's monotonic clock, so a run takes wall time, and its timing changes from run to run. Faults still fire on counters.

What your agent reaches depends on the configuration:

- **G1 and G2:** the gateway's tools over MCP's streamable HTTP transport, at `http://gateway:8000/mcp`. Run your agent in a container on the Docker network `fivefold-agent`, for example with `docker run --network fivefold-agent`. That network joins the gateway and your agent only, and the agent holds no simulator credential, so it reaches the simulator only through the gateway (record 0008). The boundary holds only for an agent in that container. An agent on the host can reach the simulator at `127.0.0.1:8100`, the Compose PostgreSQL at `127.0.0.1:5432`, which trusts every connection from 127.0.0.1, and the run's directory, which holds the gateway's and the harness's tokens. The gateway's `http://127.0.0.1:8000/mcp` answers an agent on the host too, but outside record 0008's boundary.
- **B0:** the simulator's API over HTTP, at `http://127.0.0.1:8100/api/CALL`. Each call of `src/fivefold/domain/api.py`, such as `get_quote`, `request_approval` or `submit_purchase_order`, is a POST with the request as a JSON body and the header `Authorization: Bearer TOKEN`. `TOKEN` is the agent's credential for this run: `start` prints it and writes it to `agent-1.token` in the run's directory. The final report goes to `/api/submit_final_report`. Every answer the API defines comes with status 200, apart from a refused credential: 401 for a token that is not this run's, 403 for one the configuration does not allow. In G1 and G2 the simulator refuses the agent's token. A request the API never takes gets 404 (no such call) or 422 (a body that is not the call's request, or one holding a NUL character, `\u0000`, which the harness's log cannot store), and once `end` begins, 503. The gateway refuses an MCP message holding a NUL the same way, with invalid params. The simulator publishes its port on 127.0.0.1 only, so run a B0 agent on the host, or in a container on the host's network (`docker run --network host`).

With Docker Compose, from the repository's root:

```
docker compose --profile split run --rm split start \
    --scenario scenarios/dev/dev-001-single-po.yaml --config G2 --contract C2 --plan none
docker compose --profile split up --detach --wait simulator gateway
# Run your agent here: in G1 and G2, in a container on the network fivefold-agent,
# pointed at http://gateway:8000/mcp. To check the setup first, run the scripted agent:
docker compose --profile split run --rm agent \
    --scenario scenarios/dev/dev-001-single-po.yaml --config G2 --contract C2 \
    --gateway-url http://gateway:8000/mcp
docker compose --profile split run --rm split end
docker compose --profile split run --rm split score
docker compose --profile split down
```

Start a run before the services, and start the services again for each run. Without Docker, run the same commands with `uv run python -m fivefold.harness.split` against your PostgreSQL server, and serve each service in its own terminal: `serve simulator`, then `serve gateway`. Without Docker there is no agent network, so a G1 or G2 agent there is outside record 0008's boundary. The run's directory is `split-run` unless `--run-dir` or `FIVEFOLD_RUN_DIR` says otherwise. It holds the run's secrets, so keep it to yourself; git ignores it.

`start` takes the scenario, the configuration, the contract level, the seed (default 7) and the fault plan: the scenario's own by default, `--plan none` for no fault, or `--plan FILE`, a YAML list in the format of a scenario's `faults`. A random plan runs only in the inproc topology, so `start` refuses a scenario that has one. `--model NAME` names the model your agent uses; the run records it with the model adapter `external`, since the harness runs no model here. Each run gets its own database, made from a migrated template database, and a row in the run registry, as every run does (see [Viewer](viewer.md)). `start` refuses a taken database name. `--replace` requires FiveFold's marker or its complete matching migration history. The database must hold the same run or no run yet. Use an unused name for an unrelated database. For example, F2 on the purchase order's answer in G1 or G2:

```yaml
- fault: F2
  at: {point: net.gw_sim.answer, match: {api: submit_purchase_order, answer: committed}, occurrence: 1}
```

`end` ends the agents' phase: G2 closes the run, and both services refuse your agent from then on. After the drain window, `2 × (D + max_hop_latency) + L` of wall time (20,100 ms with dev-001's values), the run stops. `score` then scores it with the evaluator, and `--out FILE` saves its result JSON for `python -m fivefold.evaluator report`.

The split topology fires faults only on the network and in the simulator, at these points:

| Point | Faults | Configurations |
| --- | --- | --- |
| `net.agent_gw.request` | F4, F12 | G1, G2 |
| `net.gw_sim.request` | F4, F6 late, F7 late, F12 | G1, G2 |
| `net.gw_sim.answer` | F2 | G1, G2 |
| `net.agent_sim.request` | F4, F6 late, F7 late, F12 | B0 |
| `net.agent_sim.answer` | F2, F6 early, F7 early | B0 |
| `sim.write.received` | F5 | all |
| `sim.status.answer` | F10 | all, at C1 |

`start` refuses, in one line, a plan with a fault inside the gateway or your agent: F1, F3, F9 and F11, and F6 and F7 early in G1 and G2. A gateway crash would mean the gateway's process exits and restarts, and the harness has no hold on your agent. It also refuses a scenario with a scripted resolver: here a human resolves escalations.

When the gateway escalates an action whose outcome is unknown, you resolve it through the gateway's [admin API](admin-api.md) at `http://127.0.0.1:8000/admin`. The gateway serves it when `FIVEFOLD_ADMIN_API_TOKEN` holds the API's bearer token, in the environment of `serve gateway`, or where you run Compose. The admin API shares its listener with the MCP endpoint, so your agent's network reaches it too, and the bearer token is its only guard. Make the token long and random, for example with `python -c "import secrets; print(secrets.token_urlsafe(32))"`, and never give it to your agent. Each attestation is also logged in the harness's actor log, as the scripted resolver's are in the inproc topology, so the evaluator counts it (record 0004). Once `end` stops the gateway, the admin API takes no attestation: it answers 503 and records nothing.

Limits: a split run has one agent worker, `agent-1`. Start each service once per run: a service keeps its fault counters, whether the agents' phase is over, and the scripted approver's timers in its memory, so one started again mid-run would fire a fault again from its first occurrence, serve your agent after the agents' phase, and never decide an approval it had scheduled. `serve` refuses a run that has ended. Every service must run on one host, since they share its monotonic clock, and each hop is assumed to take at most `max_hop_latency_ms` (50 ms by default). The harness logs your agent's messages when they reach a service, and an answer to your agent as delivered when the service hands it to its HTTP server: it cannot see whether your agent read it. The simulator logs an answer as dropped if your agent's connection has closed; the gateway's MCP library writes its answers itself, so the gateway logs each one as delivered even after your agent has gone. It counts no tokens and no cost, since it runs no model.
