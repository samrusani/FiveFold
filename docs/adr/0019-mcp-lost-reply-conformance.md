# 0019: MCP lost-reply conformance

Status: Proposed
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
- How the fixture signals that a write is held before commit, that it has
  committed and that both attempts have settled. Settlement means that the
  attempts can no longer produce effects. The author declares an upper bound
  in milliseconds for waiting for evidence.

The probe reads a zero count before the first write and reads again when both
attempts have settled. A fixture can signal a commit in the first case only
after its authoritative state reflects it. The runner uses those signals
to order observations. It cannot infer commit or settlement from a timeout,
an acknowledgement or a tool annotation.

### Checks and verdicts

| Case | Required setup and evidence | Pass | Fail | Not applicable |
| --- | --- | --- | --- | --- |
| Reply lost after commit | The fixture witnesses the first commit and withholds its reply from the caller. The caller's wait ends without that reply. The probe sees one committed effect before the runner issues one retry. | One matching effect after settlement. | The retry creates a second effect, or the settled count or effect is wrong. | The author declares that the fixture cannot hold a reply after commit. |
| Duplicate delivery | Send two valid requests for the same operation, each with its own request ID. The fixture witnesses delivery of both. Send the second after the first reply, with no induced reply loss. | One matching effect after both deliveries settle. | Delivery creates a second effect, or the settled count or effect is wrong. | The author declares that the fixture cannot exercise repeat delivery. |
| Timeout before commit | The fixture holds the first attempt before commit. The caller's wait ends and the probe sees zero effects while the first attempt is still held. The fixture establishes that the first attempt has ended without committing before the runner issues one retry. | The retry yields one matching effect after settlement. | The settled count or effect is wrong. | The author declares that the fixture cannot hold and end the first attempt before commit. |

A case that was declared supported but lacks valid timing, delivery, probe
or settlement evidence is inconclusive. For example, a missing reply and a
count of zero without a settlement signal cannot establish a failed write.
An invalid declaration or probe error is a test error. A declaration of no
retry recognition still runs all supported checks. A count of two in the
lost-reply case produces the reason `a retry places a second effect`.

### Transports, command and output

Propose `python -m fivefold.conformance --fixture PATH --seed INTEGER`.
The runner opens an SDK `Client` in auto mode for both transports. It
records the protocol the SDK negotiates through discovery or the legacy
handshake. It uses the underlying session's `call_tool` so the test
controls the number of write calls. The author cooperates with the runner
to withhold or delay replies inside the fixture. The probe and fixture
signals stay independent of the write reply. No SDK change is required.
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
Assert the report and all exit codes, including missing and invalid evidence
and an unsupported case. The tests use the locked SDK.

## Alternatives considered

- Infer the result from the retry's reply. Rejected because a reply cannot
  establish how many effects persisted.
- Make the test require an application idempotency key. Rejected because
  an author may deduplicate by arguments or may want to measure a tool that
  has no retry recognition.
- Judge success against the protocol spec. Rejected because the protocol
  permits both effect counts. The proposed test judges a tool's behavior
  against the declared operation under a specified fault.

## Consequences

The author must supply a test instance, an isolated probe and the evidence
needed for each supported fault. The report gives the conditions under
which a repeated call was observed to create a duplicate or produce one
matching effect. Its findings apply to the fixture and inputs tested.

Spec changes on acceptance: describe the fixture declaration, three cases and
verdicts in Later releases and Testing. Document the command and a local
example in `docs/conformance.md` when the command lands. Link the page from
the README's Go deeper table.
