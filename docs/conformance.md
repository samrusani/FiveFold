# MCP lost-reply conformance

FiveFold sends a synthetic write twice and uses a read-only probe to check how
many effects committed. The verdict describes one tool, input and retry
condition. MCP leaves the effect guarantee and retry identity to the
application. See [record 0019](adr/0019-mcp-lost-reply-conformance.md).

## Two-minute example

From the repository root, run `uv sync --locked` once. This fixture starts a
local stdio server backed by an in-memory ledger. It needs no database or
credentials.

```sh
fixture=$(mktemp)
cat > "$fixture" <<'JSON'
{
  "version": "1",
  "transport": {
    "kind": "stdio",
    "command": ".venv/bin/python",
    "args": ["tests/fixtures/mcp_conformance_server.py", "deduplicating"]
  },
  "write": {
    "name": "add",
    "arguments": {
      "namespace": "{{namespace}}",
      "operation_id": "{{operation_id}}",
      "value": "synthetic item"
    }
  },
  "expected_effect": "synthetic item",
  "probe": {
    "name": "read",
    "arguments": {"namespace": "{{namespace}}", "expected": "{{expected_effect}}"}
  },
  "retry_identity": "key",
  "key_argument": "key",
  "supported_cases": ["lost_reply", "duplicate_delivery"],
  "terminal_replies_settle": true,
  "caller_wait_ms": 20,
  "evidence_wait_ms": 3000
}
JSON
uv run python -m fivefold.conformance --fixture "$fixture" --seed 7
```

Both checks pass with one effect each. The pre-commit timeout is
`not_applicable` because the fixture declares only the two reply-drop checks.
Change `"deduplicating"` to `"repeating"` and run the last command again. It
reports two effects for each check and exits with 1. Delete the temporary
fixture file when done.

## Use your own test instance

Point `transport` at a disposable local instance that writes only synthetic
state. Use `stdio` with a command and argument list, or
`{"kind": "streamable_http", "url": "http://127.0.0.1:8000/mcp"}` for a
local endpoint using a literal loopback IP. The report records the protocol
the SDK negotiated.

Declare the write tool, its arguments and the expected effect. Declare a
read-only probe that returns MCP structured content with an integer `count`
and a Boolean `matches`. The count is the number of committed effects,
including multiple updates that leave the same final value. The match flag
says whether the effects match the expected contents. If work can continue
after the write replies, set `terminal_replies_settle` to false and have the
probe include `"settled": true` only when neither attempt can still commit.
The only fields are `count`, `matches` and optional `settled`. An extra
field is a test error.

Use `{{namespace}}` as an argument value in every tool declaration. Each
invocation uses a fresh namespace, and each case has its own scope.
The probe takes `{{expected_effect}}` as an argument value. The write can
also take `{{operation_id}}` as a value. The same seed fixes case order and
operation IDs. The server controls its own commit timing and results.

Set `retry_identity` to `key` when the write accepts a key. Name the argument
with `key_argument` and leave it out of the write arguments. The runner
supplies the same key for both deliveries. Use `same_arguments` if the tool
recognizes the repeated arguments, or `none` if it has no retry recognition.
The two writes carry the same business arguments and different MCP request IDs.

## What the command checks

| Case | What happens | What passes |
| --- | --- | --- |
| `lost_reply` | The runner holds the first reply until the probe sees a commit, drops it, waits out the caller's deadline, then retries once. | One matching effect after both attempts settle. |
| `duplicate_delivery` | The runner drops the first reply and sends the planned duplicate immediately. | One matching effect after both deliveries settle. |
| `timeout_before_commit` | The fixture holds the first request before commit. The caller times out and the probe sees zero effects. The fixture ends the held request before the runner retries. | One matching effect after the retry settles. |

The runner drops replies between the MCP SDK and the simulated caller, so the
SDK exchange finishes and the connection stays open. Both reply-drop cases
work with the write and probe alone.

To add the pre-commit case, list it under `supported_cases` and supply a
`pre_commit` object with three tool declarations in the same
`{"name": "...", "arguments": {...}}` format. `arm` prepares the next write and
returns `{"armed": true}`. `held` returns `{"held": true}` while that write is
still blocked before commit. `abort` ends the blocked write without committing
and returns `{"aborted": true}`. The repository fixtures enable these hooks
with `--hooks`. This is their `pre_commit` block:

```json
{
  "pre_commit": {
    "arm": {"name": "arm", "arguments": {"namespace": "{{namespace}}"}},
    "held": {"name": "holding", "arguments": {"namespace": "{{namespace}}"}},
    "abort": {"name": "abort", "arguments": {"namespace": "{{namespace}}"}}
  }
}
```

The caller wait and the evidence deadline are in milliseconds. Choose a
caller wait below the evidence deadline. A settled count or effect that
differs from the declaration fails the check. A supported case is
`inconclusive` if it lacks evidence. Invalid declarations and invalid probe
results are test errors.

Add `--json` to get the same report as JSON. Reports include versions,
case milestones, observed waits and the probe counts.

| Exit code | Meaning |
| --- | --- |
| 0 | At least one case applies and every applicable case passes. |
| 1 | At least one case reports a failure. |
| 2 | There are errors, inconclusive results or zero applicable cases. |
