# Limitations

What each of FiveFold's guarantees depends on, and the limits known so far. The guarantees are those of the gateway in configuration G2. Decision records are cited by number and live in [adr/](adr/README.md).

Status: updated for the full study on 17 Nov 2026. It reflects the accepted decisions through 0016. The viewer is implemented under record 0012.

## How to read this page

- The guarantees are claims about G2. The invariants I1 to I7 are claimed for G2 only (0003). G1, authorization only, follows a subset of them, and B0 has no gateway at all (0008).
- A guarantee holds only while everything it depends on holds. When a dependency breaks and the stack goes wrong, the evaluator still sees it, because it scores every run from the simulator's and the harness's records, never from the gateway's (0004).
- The results cover one domain and one development scenario, using a scripted agent and the two live models in the [full study](report.md) (see [Known limits](#known-limits)).

## The five properties

### 1. Exactly once

G2 keeps at most one live or Confirmed action per intent, and a repeated proposal attaches to it (I7, 0001). A model that replans after a crash cannot place a second order for an intent whose first order is live or done, at any contract level (0001). G2 retries only at C2 and C3, always with the action's one idempotency key (I4).

It depends on:

- the task listing its intents, and every write naming one; the agent cannot create intents (0001);
- at C0 and C1, the transport not delivering a request twice to the destination, which has no keys to stop the copy (F4 on `gw_sim`, 0006);
- at C1, the commit deadline D and the status lag L: G2 marks a lost request Failed only on an empty status answer accurate to `sent_vt + S`, and the agent then orders again (F5 beyond the deadline, 0006);
- at C2 and C3, the destination's key contract: the stored result for a key that committed, `in_progress` for a key in flight, and key uniqueness enforced inside the commit transaction (0005).

### 2. Authorized at commit

G2 binds one approval to one action with a grant, which the first dispatch consumes (0002). Before every first send and every C2 retry, it asks the approvals service, and sends only if the approval is approved, not revoked, in scope and not expiring at or before the send's time plus S (0002, rule 17; 0010, change 4).

It depends on:

- the approvals service, the system of record for authority, which enforces the policy in the scenario (0002);
- at C0 to C2, no revocation between the check and the commit: a revocation in that window does not stop the order (0002, rule 21; F6 late, 0006);
- at C3, the destination's atomic check of the approval at commit, which G2's C3 retries rely on instead of a check of their own (0002, rule 20; 0005).

### 3. Inputs still fresh

Before the first send, G2 checks every dependency rule against the reads the proposal cites. A C2 retry re-checks only the exact rules, because the action's own first commit, if it landed, has already changed the budget (I3, 0003).

It depends on:

- the agent citing reads that cover the operation's dependencies; G2 refuses a proposal that does not (`missing_basis`, 0001);
- the dependency rules, which are code shared by the gateway, the C3 destination and the evaluator, and change only by decision record (0004);
- at C0 to C2, no change to a cited fact between the check and the commit (F7 late, `price_up`, 0006);
- at C2, no change to the budget between a first attempt that never landed and its retry: a retry does not re-check the budget, so it can overdraw it (0005);
- at C3, the destination's atomic check of the basis at commit (0005).

### 4. Actually committed

Authorized actions form an outbox. After a gateway crash, startup recovery returns them to it, and moves open attempts to OutcomeUnknown to resume their contract's recovery (0003). At weak contracts, G2 gives up liveness rather than safety (0004).

It depends on:

- the contract level. At C0 a lost request looks like a lost reply, so G2 escalates instead of resending (F1b, and F12 on `gw_sim`, 0006). At C1 the effect lands only after the settle time S and a new proposal from the agent. At C2 and C3, G2 retries (0005);
- valid authority and unchanged exact inputs at a C2 retry: if the retry check fails, G2 does not send, and escalates (0002, rule 19);
- at C1, status answers that arrive in time: an answer later than T counts as unanswered, and after 20 unanswered probes G2 escalates. F10's delayed cell delays 5 answers, so G2 hears the sixth and confirms the order (0005; 0013);
- a human to resolve escalations (see [Escalations](#escalations));
- the two excuses: a missing effect is excused only when the authority withdrew it, or when it is infeasible even without the run's own effects. The stack's own spending never excuses it (0004).

### 5. Truthfully reported

G2 reports each action's state as it is, never reports OutcomeUnknown as confirmed, failed or rejected, and accepts no final report that contradicts its records (I5). It closes the run in the transaction that accepts the last report it expects, and an earlier report's accepted `not_done` claim closes its intent the same way (0010, item 6). The scripted policy then stops working on that intent, so its claim stays `not_done` (0011). A G2 stack that follows its rules therefore cannot fail property 5 through timing (0004).

It depends on:

- the destination keeping its contract. When it breaks one, the evidence can mislead the stack, and the run fails (0004). F10 stale beyond L, at C1, shows it: the status answers omit an order that committed, G2 marks the action Failed, and the report says "not done" while the order exists (0006; 0013);
- the agent submitting a report: with no valid report, property 5 fails for every intent (0004).

## Contract levels

What each level asks of the destination, and what G2 does when a send's outcome is unknown (0005). The levels are presets, not cumulative: C2, for example, has no status lookup.

| Level | The destination | G2 after a timeout or a lost reply | G2 escalates |
| --- | --- | --- | --- |
| C0 | Offers no capability | Never retries | At once |
| C1 | Answers which effects exist for an intent, with the time the answer is accurate to | Probes status every 1,000 ms. A listed effect gives Confirmed. An empty answer gives Failed only when it is accurate to `sent_vt + S`. Never retries. | After 20 probes |
| C2 | Returns the stored result for a key that committed, answers `in_progress` for a key in flight, and enforces key uniqueness at commit | Retries with the same key, after re-checking authority and the exact rules | When the retry check fails, or after 5 attempts |
| C3 | As C2, and checks the request's basis and approval atomically at commit, after the key lookup | Retries with the same key, and the destination decides | After 5 attempts |

Retries wait T (2,000 ms), then double, up to 8 × T, counted from the latest send (0005; 0010, reading 9). After an escalation, G2 sends no more probes or retries (0010, reading 7).

When the destination keeps its contract, each level guarantees (0005):

| Guarantee | C0 | C1 | C2 | C3 |
| --- | --- | --- | --- | --- |
| No duplicate after a lost reply (property 1) | yes, by never retrying | yes | yes | yes |
| No duplicate when the transport delivers a request twice on the gateway-to-destination hop | no | no | yes | yes |
| The required effect lands after a lost request (property 4) | no | yes, after the settle time and a re-proposal by the agent | yes | yes |
| Authority and freshness hold at commit, not only at dispatch (properties 2 and 3) | no | no | no | yes |
| A retry cannot overdraw the budget or inventory when the first attempt never landed | yes, no retries | yes, no retries | no | yes |
| The report is honest (property 5) | yes | yes | yes | yes |

Every "no" is a documented limit (0005).

## The bounds D, S and L

Every destination publishes three promises, with these defaults in virtual milliseconds (0005):

- `max_hop_latency`, 50: every message arrives within it on one hop, or counts as dropped;
- the commit deadline D, 10,000: a request not committed within D of its arrival is aborted and never commits;
- the status lag L, 0: a status answer reflects every commit up to its `as_of_vt`, which is at most L before the answer.

The settle time is S = `max_hop_latency` + D, 10,050 ms by default: a request sent at `vt` commits by `vt + S` or never (0010). G2 and the evaluator apply the same settle rules (0004, 0005):

- At C1, an empty status answer settles a send only when it is accurate to `sent_vt + S`, so declaring a lost request failed takes up to S, about 10 virtual seconds (0005).
- A human can attest "not committed" only at or after S past the action's latest send (0010, reading 8).
- An approval that expires at or before a send's time plus S counts as expired, so every approval stops being usable S before it expires: 0.3% of the example scenario's one-hour lifetime (0010, change 4).
- Retries are at least T apart, and T exceeds `max_hop_latency`, so a definitive rejection of a retry settles the earlier attempts (0005).

Scenarios can override each value, and each run records the values it used (0005). If a destination is slower than it promised, the settle rules can conclude "not committed" when a request will still commit. The catalog's contract violations show what then breaks: F5 beyond the deadline at C1, and F10 stale beyond L (0006; 0013).

## The gateway's database, credentials and the network boundary

- **No simulator credential in G1 and G2.** The agent holds no simulator credential, and only the gateway writes to the simulator. In the inproc topology, the agent receives only an MCP client bound to the gateway. In the split topology, the agent's container is only on a network it shares with the gateway, `fivefold-agent`. The boundary holds only for an agent in such a container. An agent on the host can reach the simulator, the Compose PostgreSQL, which trusts every connection from 127.0.0.1, and the run's directory, which holds the gateway's and the harness's tokens. The gateway's admin API shares its listener with the MCP endpoint, so the agent's network reaches it too, and its bearer token is its only guard. A test checks that the simulator refuses a write from the agent's identity in G modes (0008). An agent with its own route to the simulator would bypass every check. B0 holds a credential by design: that is what it measures.
- **Separate database roles.** Each component connects to the run's database with its own role. The gateway's role has no privileges on the simulator's schema, and the simulator's role none on the gateway's, so the two can never share a transaction. Without this rule, exactly once would be trivial and the results meaningless (0008). A test asserts every cell of the privilege table. The roles separate the components, not people: the Compose database trusts every connection from 127.0.0.1 ([SECURITY.md](../SECURITY.md)).
- **The gateway's own database.** Every state change is one transaction that holds a row lock on the action and writes one event to an append-only log (I6), and an attempt row is committed before its request is sent (0003). The gateway's role cannot update or delete that log (0008). After a crash, startup recovery rebuilds the gateway's work from this database alone (0003). The catalog crashes the gateway's process, never its database (F1, F3; 0006), so every guarantee assumes the database survives.

## Escalations

When G2 cannot settle an outcome safely, it escalates to a human, and its automatic recovery for that action ends (0003; 0010, reading 7). Only the human's attestation, committed or not committed, then moves the action. Without one, the action stays OutcomeUnknown at the end of the run, and the honest report says "unknown" (0003). The human checks the destination out of band, as the scripted resolver does (0008), and the scripted resolver always tells the truth (0009). A false attestation can make G2 wrong; the evaluator still compares the report with the ledger (0004).

## The agent framework

- B0, G1 and G2 share one LangGraph agent: a model node and the prebuilt `ToolNode`, with the library's default error handling and a retry policy with library defaults: 3 attempts in all, and exponential backoff from 0.5 s with jitter. These facts were checked in langgraph 1.2.22 and langgraph-prebuilt 1.1.2. B0's results depend on them, and an upgrade must check them again (0007, 0008).
- When the retries run out, the graph raises, and the worker ends without a report (0008).
- An agent killed mid-call resumes from its last checkpoint, and LangGraph re-runs the node it was in (0006). In G2 the repeated call attaches to the same action (0001); B0 has no such guard.
- All configurations in one comparison use the same model, task text, intents, authority policy and budgets. Their tool surfaces differ, and the report says so (0008).

## Scripted and live models

- Deterministic tests, the demos and `faults/expected.yaml` use a scripted model, which follows one fixed policy in every configuration (0008, amended by 0011 and 0015). Record 0006 fixes some expected outcomes; the rest come from the first reviewed deterministic run, and all are frozen before any live-model run (0006). They show what each configuration does under that policy. A live model can act differently, for example by calling B0's status tool at C1, which the scripted policy never does.
- The scripted policy waits at most three times, each for no more than the scenario's `max_wait_ms` (0015). A lower limit can leave a slow approval unused.
- A run is deterministic given its model's outputs (0009). A live run can be reproduced only from its cassette, which records every model call. A replay fails if a request differs from the recorded one, or if the run asks for more or fewer calls than the cassette holds ([spec](spec.md), Playback and branching).
- Every model call takes `think_ms` of virtual time, 1,000 ms by default, however long the model really takes (0009). A slow model and a fast one see the same virtual timings.
- A live run needs `FIVEFOLD_LIVE=1`, a spend cap in USD and a price file that prices the model. [Known limits](#known-limits) says what the cap and the token counts do not cover.

## Virtual time

- Every component, the gateway included, reads time only from the run's virtual clock, and randomness only from the run's seeded generators (0009). Timeouts, probes, polls, approver delays and restart delays are timer events in virtual time. Lint bans wall-clock calls and global randomness outside the clock and generator modules (0007).
- Message latency is drawn from the seed, between 5 and 50 ms a hop (0009), and the simulator handles one request at a time ([spec](spec.md), Simulator). A virtual time in a result says when something happened in the run, not how fast a real system is.
- Wall-clock time is recorded for metrics only, and never decides anything (0009).
- Recovery latency is virtual time too: from the first fault that touched an intent until the stack's evidence settled its outcome (0014). It says how long the stack took to know, not how fast a real system recovers, and its wall-clock twin depends on the machine. An intent whose outcome the evidence never settles has no latency, and counts as unresolved. Latency alone does not rank configurations: with F2 at C2, B0 settles in 2,013 ms by placing a second order, which fails properties 1, 2 and 3.
- One known source of nondeterminism remains: LangGraph's retry backoff sleeps in wall time, with jitter from the global random generator. It changes wall-clock metrics only (0008, 0009).
- The determinism contract covers the inproc topology only. The split topology runs the gateway, the simulator and PostgreSQL as separate services, for people who bring their own agent. It is not deterministic: its virtual clock follows the host's monotonic clock, though faults still fire on counters (0009).

## Known limits

1. **Race windows at C0 to C2.** G2 checks the approval and the inputs, then sends, and the destination commits later. A revocation or a price change in between lets the order commit on dead authority or a stale price. So G2 fails property 2 under F6 late, and property 3 under F7 late, `price_up`, at C0 to C2, by design ([plan](plan.md), Risks; 0002, rule 21; 0006). C3 closes both windows in the simulator, whose C3 destination checks the basis and the approval atomically at commit (0005). A real destination closes them only if it does the same. One that checks versions only, as with an ETag, leaves the authority race open (0005, Alternatives considered).
2. **G2's documented limits.** Record 0006 lists the G2 cells that fail or stay unresolved by design. The failing properties are those of dev-001, whose budget covers one order but not two; another scenario can fail a different set.

   | Cell | G2 verdict | Why (0006) |
   | --- | --- | --- |
   | F1b at C0 | fail (4) | The request may never have left, and C0 offers no way to tell. Safety is kept; liveness is lost. |
   | F12 on `gw_sim` at C0 | fail (4) | The same reason |
   | F4 on `gw_sim` at C0 and C1 | fail (1, 2, 3) | The transport duplicated the request. Without keys, the destination commits a second order that cites the same approval and overdraws the budget. |
   | F5 beyond the deadline at C1 | fail (1, 3) | The destination broke its deadline, so Failed was wrong. The agent's second order committed, then the late first one overdrew the budget. |
   | F6 late at C0 to C2 | fail (2) | Authority changed after the check and before the commit |
   | F7 late, `price_up`, at C0 to C2 | fail (3) | The quote changed after the check and before the commit |
   | F2 with F10 stale beyond L, at C1 | fail (5) | The stale status made Failed wrong: every answer omits the order that committed. The agent finds the budget spent, orders nothing more, and the report says "not done" while the order exists (0013). |
   | F2, F3 and F5 (both variants) at C0, with no resolver | pass_unresolved | The honest answer is "unknown", and a human must reconcile |

   F10 has three cells, all at C1 in G2, as record 0013 settles them: delayed and stale within L pass, and stale beyond L fails property 5, as above.
3. **B0 has no F10 cells.** F10 changes status answers, and B0's scripted agent never calls the status tool, so F10 never fires in B0. Record 0006's B0 column for F10 reads "same, if the agent calls the status tool". G1 has no F10 cell either: its gateway never probes, and its agent has no status tool (`faults/expected.yaml`).
4. **F8 is deferred to v0.2.** Memory correction after a proposal is not tested. F7 exercises property 3 in the meantime (0006).
5. **One domain.** v0.1 covers procurement only: quotes, suppliers, budgets, approvals, purchase orders and inventory reservations ([product](product.md), Scope). The expected outcomes cover one development scenario, dev-001, a task with one intent (0006). The [full study](report.md) uses that scenario with two live models and three seeds; it does not establish results for other scenarios or models.
6. **A fixed fault catalog.** Faults fire at named points, on per-point event counters, never on wall-clock time (0006). FiveFold is not a general chaos tool ([product](product.md), Scope), so failures outside the catalog are not tested.
7. **The split topology is not deterministic** (0009; see [Virtual time](#virtual-time)). It fires faults only on the network and in the simulator, so it refuses F1, F3, F9 and F11, and F6 and F7 early in G1 and G2. It runs one agent worker, its services must share one host, and each service must start once per run: its fault counters live in its memory, so a service started again mid-run fires a fault again from its first occurrence. It counts no tokens and no cost, since it runs no model ([Bring your own agent](your-own-agent.md)).
8. **Failed is final, and nothing is undone.** A commit after Failed happens only when the destination breaks its contract; G2 does not reverse Failed, and the evaluator still scores the ledger (0003). v0.1 has no compensation: if one intent's effect commits and another's fails, nothing is undone, and the report must say so (0004).
9. **An agent cannot withdraw a live proposal.** It waits for the approval, a denial or the approval timeout (0001).
10. **One baseline without a gateway.** B0 keeps the framework's defaults. A B0 with `ToolNode`'s error handling turned on may come later as a separate configuration, and G2 with one feature switched off at a time is deferred (0008). The Temporal and DBOS baselines, B1 and B2, come in v0.2 ([plan](plan.md)).
11. **G1 is a pattern, not a product.** G1 follows the single-use grant pattern, and no claim is made that it reproduces any specific product (0008). Record 0006 fixes no G1 outcome, so G1's cells come from its first deterministic runs (`faults/expected.yaml`).
12. **Comparisons with prior work.** The metric `success_claimed_with_duplicate` is comparable in spirit to the false-success measure in "Where Does Exactly-Once Live? Model, Harness, and Tool-Contract Effects on Duplicate Side Effects in LLM Agents" (LIMBO, arXiv 2609.29095, a preprint). The two definitions are not claimed to match (0004).
13. **The development scenarios are public.** Code can be fitted to the scenarios in this repository. The plan guards against it with held-out scenarios that a separate team keeps outside the repository ([plan](plan.md), Risks). The full study did not run the held-out set.
14. **A wall-clock timer in a run's event loop holds virtual time.** Before the scheduler moves the virtual clock, it waits until no timer is pending in the run's event loop, so that a task sleeping in wall time, such as LangGraph's retry backoff, changes wall-clock metrics only (0008, 0009). Any other timer in that loop holds virtual time until it fires, such as the one `asyncio.wait_for` sets. Code that runs a run, a test included, must not wrap it in `asyncio.wait_for`: run it in its own thread, and wait on that thread instead.
15. **A reply with no token usage counts 0 tokens.** Token counts come from each reply's usage ([spec](spec.md), Runs and determinism). A reply that reports none adds 0 tokens to its worker's token budget, and 0 to the result's `tokens_in`, `tokens_out` and cost. Scripted replies report none by design.
16. **The spend cap is not a hard ceiling on a provider's invoice.** Before each call, the spend counted so far, from each reply's reported tokens at the price file's prices, is checked against the cap. Once it reaches the cap, further calls are refused ([spec](spec.md), Runs and determinism), but the call in flight can pass it ([spec](spec.md), Testing). For a model priced above 0, a reply that reports no usage makes the spend unknown, and further calls are refused as well. The meter applies one input rate to all input tokens; it does not account for different cache-read or cache-write prices, which can make the provider bill lower or higher than the counted spend ([API pilot](report/pilot-api.md)).
17. **No message holding a NUL character.** PostgreSQL's JSONB cannot store `\u0000`, so neither can the harness's log. An agent's MCP message to the gateway that holds one, in a value or a key, is refused with invalid params before anything logs it, in both topologies; the agent's framework retries the refused call as it retries any MCP error, so the worker then ends with an error. B0's tools refuse one as an invalid argument, since every request model, the final report's included, already does; the model sees the tool error. The split simulator refuses a body holding one with 422. A scripted model never sends one; a live model could.
18. **Ollama input usage and prompt cache.** The [local pilot](report/pilot-ollama.md) found that reported input can decrease between calls as the rendered prompt changes. The budget counts reported usage, but the records do not independently show whether every token read was counted when the prompt cache was used.
19. **MCP session recovery in the split topology.** In G1 and G2, the worker opens a new session when the gateway reports that the old one is gone. The failed request never reached a tool. In G1, recovery follows the same retry rules as a timeout. A write whose reply was lost can be placed twice. That is what G1 measures (0008). In G2, an identical proposal attaches to the same action (0001). The demos, the 235 expected outcomes, and the full study use the in-process topology. [Tracking issue #101](https://github.com/samrusani/FiveFold/issues/101)

## Out of scope

v0.1 is not an agent framework, a durable execution engine, a model router or a memory product. It has no receipts, signing, TEE or hosted service ([product](product.md), Scope). Later releases are listed in the [plan](plan.md).
