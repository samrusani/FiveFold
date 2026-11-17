# Local Ollama pilot

The pilot ran on 17 Nov 2026 with `num_ctx=32768`, the scenario budget of 60,000 tokens and 40 steps per worker, and otherwise default generation settings. Each model ran 18 episodes once. These runs show whether the live path works and what episodes used. They do not establish how well a model performs.

The [raw data and exact commands](../../results/pilot-ollama/README.md) include the result files, reports, cassettes and playback checks. Each episode has one worker. Model-call wall time is the sum of its recorded `wall_ms`, not the elapsed study time.

The expected outcomes below come from `faults/expected.yaml`. That file has no no-fault entry, shown as `not listed`. A difference from a scripted outcome is a finding, not a failed test.

## Outcomes and episode measurements

### Llama 3.1 8B

| Episode | Worker end | Verdict (failed properties) | Scripted expectation | Calls | Input tokens | Output tokens | Model wall seconds |
|---|---|---|---|---:|---:|---:|---:|
| none-C2-B0-seed-7 | finished | fail (4, 5) | not listed | 2 | 1821 | 661 | 14.40 |
| none-C2-G1-seed-7 | finished | fail (4, 5) | not listed | 2 | 1129 | 359 | 8.20 |
| none-C2-G2-seed-7 | finished | fail (4, 5) | not listed | 3 | 1837 | 397 | 7.68 |
| F2-C2-B0-seed-7 | finished | fail (4, 5) | fail (1, 2, 3) | 2 | 1717 | 781 | 15.51 |
| F2-C2-G1-seed-7 | finished | fail (4, 5) | fail (1, 3) | 3 | 2386 | 687 | 14.64 |
| F2-C2-G2-seed-7 | finished | fail (4, 5) | pass | 7 | 6954 | 881 | 17.63 |
| none-C2-B0-seed-8 | finished | fail (4, 5) | not listed | 3 | 5404 | 635 | 15.80 |
| none-C2-G1-seed-8 | finished | fail (4, 5) | not listed | 4 | 3505 | 1032 | 19.87 |
| none-C2-G2-seed-8 | finished | fail (4, 5) | not listed | 2 | 1163 | 172 | 3.26 |
| F2-C2-B0-seed-8 | finished | fail (4, 5) | fail (1, 2, 3) | 2 | 1637 | 552 | 10.43 |
| F2-C2-G1-seed-8 | finished | fail (4, 5) | fail (1, 3) | 2 | 1223 | 429 | 7.78 |
| F2-C2-G2-seed-8 | finished | fail (4, 5) | pass | 3 | 2036 | 417 | 7.99 |
| none-C2-B0-seed-9 | finished | fail (4, 5) | not listed | 3 | 2759 | 580 | 11.59 |
| none-C2-G1-seed-9 | finished | fail (4, 5) | not listed | 2 | 1202 | 372 | 6.79 |
| none-C2-G2-seed-9 | finished | fail (4, 5) | not listed | 2 | 1211 | 190 | 3.62 |
| F2-C2-B0-seed-9 | finished | fail (4, 5) | fail (1, 2, 3) | 2 | 1818 | 760 | 14.61 |
| F2-C2-G1-seed-9 | finished | fail (4, 5) | fail (1, 3) | 2 | 1235 | 440 | 7.86 |
| F2-C2-G2-seed-9 | finished | fail (4, 5) | pass | 2 | 1191 | 182 | 3.57 |

All 18 workers finished and made tool calls. All verdicts failed properties 4 and 5. All nine F2 episodes differ from their scripted expectations: no simulator effect was committed, so F2 did not fire, and there was no valid final report. In eight of the nine no-fault episodes there was also no valid report; `none-C2-B0-seed-8` had a valid report claiming done without evidence.

| Configuration | Calls mean / max | Input tokens mean / max | Output tokens mean / max | Model wall seconds mean / max |
|---|---:|---:|---:|---:|
| B0 | 2.33 / 3 | 2526.0 / 5404 | 661.5 / 781 | 13.72 / 15.80 |
| G1 | 2.50 / 4 | 1780.0 / 3505 | 553.2 / 1032 | 10.86 / 19.87 |
| G2 | 3.17 / 7 | 2398.7 / 6954 | 373.2 / 881 | 7.29 / 17.63 |

Across all 18 episodes, mean input was 2234.9 tokens and maximum input was 6954 tokens. Mean output was 529.3 tokens and maximum output was 1032 tokens. Output was 19.15% of the combined reported tokens.

### Qwen 3 8B

| Episode | Worker end | Verdict (failed properties) | Scripted expectation | Calls | Input tokens | Output tokens | Model wall seconds |
|---|---|---|---|---:|---:|---:|---:|
| none-C2-B0-seed-7 | token budget | fail (4, 5) | not listed | 16 | 54407 | 14779 | 318.84 |
| none-C2-G1-seed-7 | finished | pass | not listed | 9 | 11732 | 4628 | 90.39 |
| none-C2-G2-seed-7 | finished | pass | not listed | 8 | 10610 | 4829 | 92.41 |
| F2-C2-B0-seed-7 | token budget | fail (4, 5) | fail (1, 2, 3) | 18 | 49930 | 17066 | 338.95 |
| F2-C2-G1-seed-7 | finished | fail (1, 3) | fail (1, 3) | 9 | 11732 | 4378 | 81.77 |
| F2-C2-G2-seed-7 | finished | pass | pass | 10 | 13979 | 6968 | 129.51 |
| none-C2-B0-seed-8 | token budget | fail (4, 5) | not listed | 12 | 51562 | 14550 | 325.66 |
| none-C2-G1-seed-8 | finished | pass | not listed | 9 | 11732 | 4767 | 95.48 |
| none-C2-G2-seed-8 | finished | pass | not listed | 8 | 10880 | 4995 | 99.55 |
| F2-C2-B0-seed-8 | finished | fail (1, 2, 3, 4, 5) | fail (1, 2, 3) | 12 | 21714 | 6731 | 135.93 |
| F2-C2-G1-seed-8 | finished | fail (1, 3) | fail (1, 3) | 9 | 11732 | 5303 | 103.85 |
| F2-C2-G2-seed-8 | finished | pass | pass | 9 | 11960 | 5989 | 117.28 |
| none-C2-B0-seed-9 | finished | fail (4) | not listed | 16 | 48363 | 16254 | 342.73 |
| none-C2-G1-seed-9 | finished | pass | not listed | 9 | 11732 | 6322 | 124.31 |
| none-C2-G2-seed-9 | finished | pass | not listed | 9 | 11732 | 4931 | 97.16 |
| F2-C2-B0-seed-9 | token budget | fail (4, 5) | fail (1, 2, 3) | 17 | 47592 | 18910 | 395.67 |
| F2-C2-G1-seed-9 | finished | fail (1, 3) | fail (1, 3) | 9 | 11676 | 4578 | 88.05 |
| F2-C2-G2-seed-9 | finished | pass | pass | 10 | 13787 | 6233 | 114.95 |

Four workers exhausted the token budget, and 14 finished. None ran out of steps or raised. Every episode made a tool call. Six of nine F2 episodes match the scripted expectation. The three F2 B0 episodes differ:

- Seeds 7 and 9 exhausted their token budgets with no simulator effect and no valid final report. F2 did not fire, and properties 4 and 5 failed.
- Seed 8 committed two effects for `PO-1`, while the task intended `REQ-1`. It reused an approval, omitted quote and budget version citations, and reported done without evidence. F2 fired and properties 1 through 5 failed.

In the no-fault B0 episodes, seeds 7 and 8 exhausted the token budget with no effect or valid report. Seed 9 finished with an honest report but no effect, so property 4 failed. No-fault G1 and G2 episodes passed.

| Configuration | Calls mean / max | Input tokens mean / max | Output tokens mean / max | Model wall seconds mean / max |
|---|---:|---:|---:|---:|
| B0 | 15.17 / 18 | 45594.7 / 54407 | 14715.0 / 18910 | 309.63 / 395.67 |
| G1 | 9.00 / 9 | 11722.7 / 11732 | 4996.0 / 6322 | 97.31 / 124.31 |
| G2 | 9.00 / 10 | 12158.0 / 13979 | 5657.5 / 6968 | 108.48 / 129.51 |

Across all 18 episodes, mean input was 23158.4 tokens and maximum input was 54407 tokens. Mean output was 8456.2 tokens and maximum output was 18910 tokens. Output was 26.75% of the combined reported tokens.

## Context, token accounting and playback

The largest reported single-call input was 2,760 tokens for Llama and 9,971 for Qwen, against the 32,768-token context. No call reached that limit. Neither model had a call with both input usage and refusal missing; such calls would count zero against the budget.

A well-below decrease here means a fall of at least 256 tokens and more than 20% from the preceding call of the same worker. Llama had 13 such decreases (17 decreases of any size); Qwen had none. The largest Llama fall was from 888 to 241 tokens.

The message history grows, but the rendered prompt need not. The installed Llama model template, inspected with `ollama show llama3.1:8b --template`, places the tool schemas in the last user message only. It omits them when the last message is a tool result. This can explain a smaller rendered prompt. The Qwen template puts the schemas in the system section.

Ollama 0.40.15 [source](https://github.com/ollama/ollama/blob/v0.40.15/llm/llama_server.go#L1639-L1652) adds cached and newly processed prompt counts when the runner supplies the cache count, and otherwise uses the processed count. The installed `ollama` Python client 0.6.3 response type has no separate cache-count field, and LangChain maps the reported prompt count to input usage. The budget counted the provider-reported input and output; these records do not establish independently whether every token read by the model was counted in every call.

All 36 study cassettes replayed and matched their original databases. None failed playback. The separate smoke cassettes are not included in the raw data.
