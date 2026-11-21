# 0019: MCP lost-reply conformance

Status: Accepted on 19 Nov 2026 by the maintainer
Date: 2026-11-18
Spec sections affected: Later releases (MCP lost-reply conformance test), Testing

## Context

[MCP issue #3394](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394)
asks how the specification should describe a mutating call whose reply is lost
and who is responsible for avoiding duplicate effects. It suggests an example
that counts effects after a retry, with a stable application key as a control.
MCP defines the request ID and the tool call. It leaves retry identity and the
effect guarantee to the application. A FiveFold verdict measures retry
behavior for one tool, input and test condition. A duplicate is compatible with
the MCP protocol. See the request ID rules and tool semantics for
[2025-11-25 requests](https://modelcontextprotocol.io/specification/2025-11-25/basic#requests),
[2025-11-25 tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
[2026-07-28 requests](https://modelcontextprotocol.io/specification/2026-07-28/basic#requests)
and [2026-07-28 tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).

## Decision

### What the author declares

The author supplies a disposable, local test instance that writes only to
synthetic state. For stdio, the author supplies the executable and argument
list. For Streamable HTTP, the author supplies the local MCP endpoint. Each
case starts with a fresh namespace and declares the following.

- The write tool, its arguments, the expected effect and the supported cases.
- A read-only probe of authoritative state for that namespace. It counts
  every effect in the namespace and checks the contents against the expected
  result. For an update, the probe must count commits, for example from an
  audit log, even when two updates leave the same final value.
- The application's retry identity. Use the same arguments when the tool
  recognizes them, reuse a key in the declared argument when the tool accepts
  one, or declare no recognition. Reuse the same operation and arguments in
  all three modes. Each request gets its own SDK-assigned ID.
- The upper bound in milliseconds for waiting for evidence. Settlement
  means the attempts can no longer produce effects. For work that can
  continue after the tool replies, the probe must also establish that
  the attempts have settled.

The probe reads a zero count before the first write and reads again when
both attempts have settled. When the author declares that a tool finishes
all work before it replies, the runner can establish settlement from
both terminal replies, including a reply it drops. A timeout, early
acknowledgement or tool annotation cannot establish settlement.

Only the pre-commit timeout case needs fixture hooks. They hold the first
attempt before commit and establish that it ended without committing
before the retry. The other two cases use an ordinary test instance and
the same read-only probe.

### Checks and verdicts

| Case | Required setup and evidence | Pass | Fail | Not applicable |
| --- | --- | --- | --- | --- |
| Reply lost after commit | The runner receives and holds the first tool reply. The probe witnesses a committed effect while the reply is held. The runner drops the reply, lets the caller's wait expire, then issues one retry. | One matching effect after settlement. | The retry creates a second effect, or the settled count or effect is wrong. | The author declares the operation unavailable for this check. |
| Duplicate delivery | The runner plans two valid requests for the same operation, each with its own request ID. It drops the first reply and then sends the planned duplicate without waiting for a caller timeout. It observes a terminal reply for each delivery. | One matching effect after both deliveries settle. | Delivery creates a second effect, or the settled count or effect is wrong. | The author declares the operation unavailable for repeat delivery. |
| Timeout before commit | The fixture holds the first attempt before commit. The caller's wait ends and the probe sees zero effects while the first attempt is still held. The fixture establishes that the first attempt has ended without committing before the runner issues one retry. | The retry yields one matching effect after settlement. | The settled count or effect is wrong. | The author declares that the fixture cannot hold and end the first attempt before commit. |

A case that was declared supported but lacks valid timing, delivery, probe
or settlement evidence is inconclusive. For example, a missing reply and a
count of zero without a settlement signal cannot establish a failed write.
An invalid declaration or probe error is a test error. A declaration of no
retry recognition still runs all supported checks. A count of two in the
lost-reply case produces the reason `a retry places a second effect`.

### Transports, command and output

Use `python -m fivefold.conformance --fixture PATH --seed INTEGER`.
The runner opens an SDK `Client` in auto mode for both transports. It
records the protocol the SDK negotiates through discovery or the legacy
handshake. It uses the underlying session's `call_tool` so the test
controls the number of write calls. The runner receives each tool reply
from the SDK before handing it to the simulated caller. This is the
fault point shared by both transports. The runner discards only the
selected first reply and records that it received and dropped it. The
SDK exchange runs to completion, so the injected loss leaves the
server connection open. The simulated caller has its own wait deadline.
The probe and pre-commit fixture signals stay independent of the write
reply. No SDK change is required.
See the MCP transport specifications for
[2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
and [2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports).

Checked in the locked MCP SDK 2.3.0 source:

| API | Source in the installed package |
| --- | --- |
| `Client` in auto mode, `session`, `protocol_version` | `mcp/client/client.py` |
| `StdioServerParameters`, `stdio_client` | `mcp/client/stdio.py` |
| `streamable_http_client` | `mcp/client/streamable_http.py` |
| `ClientSession.call_tool` with `read_timeout_seconds` | `mcp/client/session.py` |

Emit a plain-text report for an issue and a machine-readable report with the
same fields. Include FiveFold, SDK, negotiated protocol and fixture versions,
transport, tool name, seed, identity mode and a row per case. Each row gives
the observed evidence, scoped counts, verdict and reason. Exclude raw
arguments, keys, credentials and reply bodies. Exit with 1 on any observed
failure. Otherwise exit with 2 on a test error, an inconclusive case or zero
applicable cases. Exit with 0 when at least one check applies and every
applicable check passes.

### Reproducibility and tests

The seed fixes input generation, operation IDs within the namespace and
case order. Give each invocation its own empty namespace. Milestones, such
as a witnessed commit before retry, fix the sequence of the test. The
server controls commit latency, cancellation, the recorded effect and the
eventual outcome. Report observed wait times in milliseconds. A seed
reproduces the input and schedule, with no guarantee about wall time or
the server's results.

Use two small fixture servers in the repo, each run over stdio and local
Streamable HTTP. One persists deduplication at the synthetic effect boundary.
The other records every delivered write that reaches commit. The first
passes the three cases. The second fails the first two with a count of two
and passes the pre-commit case because the first attempt never commits.
Run the two reply-drop checks with the pre-commit hooks disabled. Assert
that the runner drops one reply, that there are exactly two write requests,
and that the effect counts come from the probe. Assert the report and
all exit codes, including missing and invalid evidence and an unsupported
case. The tests use the locked SDK.

## Alternatives considered

- Infer the result from the retry's reply. Rejected because a reply cannot
  establish how many effects persisted.
- Make the test require an application idempotency key. Rejected because
  an author may deduplicate by arguments or may want to measure a tool that
  has no retry recognition.
- Judge success against the protocol spec. Rejected because the protocol
  permits both effect counts. The proposed test judges a tool's behavior
  against the declared operation under a specified fault.
- Interrupt the stdio connection or close the HTTP response stream.
  Either can interfere with server work. Modern Streamable HTTP uses
  stream closure to signal cancellation. Both transports deliver a tool
  reply to the SDK, so the runner can inject the loss at its own handoff
  to the caller. See [Streamable HTTP cancellation](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#cancellation).

## Consequences

The author needs a test instance and an isolated probe for the two
reply-drop checks. The pre-commit timeout also needs the fixture hooks.
The report gives the conditions under which a repeated call was observed
to create a duplicate or produce one matching effect. Its findings apply
to the instance and inputs tested.

Spec changes on acceptance: describe the fixture declaration, three cases and
verdicts in Later releases and Testing. Document the command and a local
example in `docs/conformance.md` when the command lands. Link the page from
the README's Go deeper table.
