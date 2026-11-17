# Admin API

The gateway's admin API lists its actions, grants, action events and open escalations, and resolves an escalation when a human attests what happened (records 0003 and 0010). Approvals stay in the approvals service. In-process runs, the demos and the scripted resolver call the gateway's admin path directly. The HTTP API, built with FastAPI, is for a gateway served over HTTP: `fivefold.gateway.admin_api.admin_app(admin, records, token=TOKEN)` returns it as an ASGI app, to mount under a path such as `/admin`. The split topology's gateway serves it at `/admin` (see [Bring your own agent](your-own-agent.md)). It reads time only from the gateway's virtual clock and never contacts the simulator.

Every request needs `Authorization: Bearer TOKEN`. A deployment reads the token from `FIVEFOLD_ADMIN_API_TOKEN`, and the app refuses an empty one, since the API attests outcomes.

| Request | Answer |
| --- | --- |
| `GET /actions` | Every action: its state, intent, operation, target, arguments and the approval its grant names |
| `GET /actions/A-0001` | One action, its escalation and its action events in seq order |
| `GET /grants` | Every grant |
| `GET /events?from_seq=N` | The run's action events from seq N on, in seq order |
| `GET /escalations` | The open escalations, each with `not_committed_from_vt` |
| `POST /escalations/ESC-0001/resolve` | Attests the outcome, and answers with the action's result |

To attest that order PO-0001 committed:

```
curl -X POST http://HOST/admin/escalations/ESC-0001/resolve \
    -H "Authorization: Bearer $FIVEFOLD_ADMIN_API_TOKEN" -H "Content-Type: application/json" \
    -d '{"resolution": "committed", "effect_id": "PO-0001", "actor": "ops-1"}'
```

`{"resolution": "not_committed", "actor": "ops-1"}` attests that nothing committed. The gateway accepts it only from S after the action's latest send, when every request has committed or never will; S is 10,050 ms of virtual time in dev-001. Errors answer `{"reason": ..., "message": ...}`: 401 without the token, 404 for an unknown action or escalation, 409 for an escalation resolved already or a "not committed" that comes too early (with `not_committed_from_vt`), and 422 for a body that does not parse.
