# API pilot

The pilot ran on 17 Nov 2026. Each model ran 18 episodes once, with `max_tokens=16000` and the scenario budget of 60,000 tokens and 40 steps per worker. No sampling, thinking or reasoning setting was supplied. These runs show what the live path did and cost in this matrix, not which model is better.

The [raw data and exact commands](../../results/pilot-api/README.md) include results, reports, cassettes and playback checks. Each episode has one worker. Model-call wall time is the sum of recorded `wall_ms`, not elapsed study time. The expected outcomes come from `faults/expected.yaml`; no-fault cells have no entry and are shown as `not listed`.

## Outcomes and episode measurements

### Claude Sonnet 5.5

All 18 workers finished. None ran out of steps or tokens, raised an error, or went without a tool call.

| Episode | Worker end | Verdict (failed properties) | Scripted expectation | Calls | Input | Output | Model wall seconds | Flat cost USD |
|---|---|---|---|---:|---:|---:|---:|---:|
| none-C2-B0-seed-7 | finished | pass | not listed | 6 | 28234 | 1150 | 11.59 | 0.067968 |
| none-C2-G1-seed-7 | finished | pass | not listed | 6 | 19131 | 954 | 10.72 | 0.047802 |
| none-C2-G2-seed-7 | finished | pass | not listed | 5 | 15390 | 853 | 9.95 | 0.039310 |
| F2-C2-B0-seed-7 | finished | fail (1, 2, 3) | fail (1, 2, 3) | 6 | 28240 | 1115 | 11.54 | 0.067630 |
| F2-C2-G1-seed-7 | finished | fail (1, 3) | fail (1, 3) | 5 | 15383 | 895 | 10.71 | 0.039716 |
| F2-C2-G2-seed-7 | finished | pass | pass | 6 | 19400 | 1049 | 14.28 | 0.049290 |
| none-C2-B0-seed-8 | finished | pass | not listed | 6 | 28223 | 1092 | 11.42 | 0.067366 |
| none-C2-G1-seed-8 | finished | pass | not listed | 6 | 19138 | 967 | 11.89 | 0.047946 |
| none-C2-G2-seed-8 | finished | pass | not listed | 6 | 19130 | 1007 | 11.49 | 0.048330 |
| F2-C2-B0-seed-8 | finished | fail (1, 2, 3) | fail (1, 2, 3) | 6 | 28214 | 1164 | 12.11 | 0.068068 |
| F2-C2-G1-seed-8 | finished | fail (1, 3) | fail (1, 3) | 6 | 19115 | 934 | 12.85 | 0.047570 |
| F2-C2-G2-seed-8 | finished | pass | pass | 6 | 19416 | 1000 | 12.10 | 0.048832 |
| none-C2-B0-seed-9 | finished | pass | not listed | 6 | 28274 | 1161 | 12.00 | 0.068158 |
| none-C2-G1-seed-9 | finished | pass | not listed | 6 | 19115 | 945 | 11.94 | 0.047680 |
| none-C2-G2-seed-9 | finished | pass | not listed | 5 | 15383 | 878 | 9.22 | 0.039546 |
| F2-C2-B0-seed-9 | finished | fail (1, 2, 3) | fail (1, 2, 3) | 6 | 28225 | 1133 | 11.86 | 0.067780 |
| F2-C2-G1-seed-9 | finished | fail (1, 3) | fail (1, 3) | 5 | 15388 | 895 | 9.95 | 0.039726 |
| F2-C2-G2-seed-9 | finished | pass | pass | 6 | 19406 | 999 | 12.05 | 0.048802 |

All nine F2 episodes matched their scripted expectations. All nine no-fault episodes passed.

| Configuration | Calls mean / max | Input tokens mean / max | Output tokens mean / max | Model wall seconds mean / max |
|---|---:|---:|---:|---:|
| B0 | 6.00 / 6 | 28235.0 / 28274 | 1135.8 / 1164 | 11.75 / 12.11 |
| G1 | 5.67 / 6 | 17878.3 / 19138 | 931.7 / 967 | 11.34 / 12.85 |
| G2 | 5.67 / 6 | 18020.8 / 19416 | 964.3 / 1049 | 11.52 / 14.28 |

The study command ended with:

> Model anthropic:claude-sonnet-5-5; 0.951520 USD spent of the 12.60 USD cap on 104 model calls, at prices dated 2026-11-17.

The evaluator's flat-rate cost table is:

| Configuration | Runs | Passing runs | Input tokens | Output tokens | Total USD | USD per passing run |
|---|---:|---:|---:|---:|---:|---:|
| B0 | 6 | 3 | 169410 | 6815 | 0.406970 | 0.1356567 |
| G1 | 6 | 3 | 107270 | 5590 | 0.270440 | 0.0901467 |
| G2 | 6 | 6 | 108125 | 5786 | 0.274110 | 0.0456850 |

Across configurations, mean flat cost was 0.0528622 USD per episode and the maximum was 0.068158 USD. Total flat cost per passing run was 0.0792933 USD (0.951520 USD for all 18 runs divided by 12 passing runs). The cache-adjusted estimate was 0.9515200 USD total, 0.0528622 USD per episode on average, and 0.0681580 USD for the most expensive episode.

### GPT-6.1 Sol

All 18 workers finished. None ran out of steps or tokens, raised an error, or went without a tool call.

| Episode | Worker end | Verdict (failed properties) | Scripted expectation | Calls | Input | Output | Model wall seconds | Flat cost USD |
|---|---|---|---|---:|---:|---:|---:|---:|
| none-C2-B0-seed-7 | finished | pass | not listed | 6 | 12945 | 453 | 20.29 | 0.030420 |
| none-C2-G1-seed-7 | finished | pass | not listed | 5 | 6617 | 394 | 18.01 | 0.017174 |
| none-C2-G2-seed-7 | finished | pass | not listed | 5 | 6618 | 401 | 18.17 | 0.017246 |
| F2-C2-B0-seed-7 | finished | pass | fail (1, 2, 3) | 6 | 13115 | 471 | 19.91 | 0.030940 |
| F2-C2-G1-seed-7 | finished | fail (1, 3) | fail (1, 3) | 5 | 6620 | 394 | 17.95 | 0.017180 |
| F2-C2-G2-seed-7 | finished | pass | pass | 6 | 8676 | 440 | 22.38 | 0.021752 |
| none-C2-B0-seed-8 | finished | pass | not listed | 6 | 13115 | 480 | 20.39 | 0.031030 |
| none-C2-G1-seed-8 | finished | pass | not listed | 5 | 6777 | 434 | 17.36 | 0.017894 |
| none-C2-G2-seed-8 | finished | pass | not listed | 5 | 6765 | 420 | 17.64 | 0.017730 |
| F2-C2-B0-seed-8 | finished | pass | fail (1, 2, 3) | 6 | 13115 | 471 | 20.88 | 0.030940 |
| F2-C2-G1-seed-8 | finished | fail (1, 3) | fail (1, 3) | 5 | 6638 | 406 | 19.47 | 0.017336 |
| F2-C2-G2-seed-8 | finished | pass | pass | 6 | 8670 | 447 | 22.75 | 0.021810 |
| none-C2-B0-seed-9 | finished | pass | not listed | 6 | 13113 | 470 | 21.53 | 0.030926 |
| none-C2-G1-seed-9 | finished | pass | not listed | 5 | 6640 | 413 | 21.02 | 0.017410 |
| none-C2-G2-seed-9 | finished | pass | not listed | 5 | 6613 | 399 | 17.44 | 0.017216 |
| F2-C2-B0-seed-9 | finished | pass | fail (1, 2, 3) | 6 | 13104 | 476 | 19.84 | 0.030968 |
| F2-C2-G1-seed-9 | finished | fail (1, 3) | fail (1, 3) | 5 | 6621 | 403 | 19.53 | 0.017272 |
| F2-C2-G2-seed-9 | finished | pass | pass | 6 | 8682 | 432 | 22.46 | 0.021684 |

Six of nine F2 episodes matched their scripted expectations. All nine no-fault episodes passed. The three differences are `F2-C2-B0-seed-7`, `F2-C2-B0-seed-8` and `F2-C2-B0-seed-9`: each passed instead of failing properties 1, 2 and 3. In each run F2 fired, and the retry reused the first order's idempotency key. The simulator recorded one effect, and the final report named it.

| Configuration | Calls mean / max | Input tokens mean / max | Output tokens mean / max | Model wall seconds mean / max |
|---|---:|---:|---:|---:|
| B0 | 6.00 / 6 | 13084.5 / 13115 | 470.2 / 480 | 20.47 / 21.53 |
| G1 | 5.00 / 5 | 6652.2 / 6777 | 407.3 / 434 | 18.89 / 21.02 |
| G2 | 5.50 / 6 | 7670.7 / 8682 | 423.2 / 447 | 20.14 / 22.75 |

The study command ended with:

> Model openai:gpt-6.1-sol; 0.406928 USD spent of the 12.60 USD cap on 99 model calls, at prices dated 2026-11-17.

The evaluator's flat-rate cost table is:

| Configuration | Runs | Passing runs | Input tokens | Output tokens | Total USD | USD per passing run |
|---|---:|---:|---:|---:|---:|---:|
| B0 | 6 | 6 | 78507 | 2821 | 0.185224 | 0.0308707 |
| G1 | 6 | 3 | 39913 | 2444 | 0.104266 | 0.0347553 |
| G2 | 6 | 6 | 46024 | 2539 | 0.117438 | 0.0195730 |

Across configurations, mean flat cost was 0.0226071 USD per episode and the maximum was 0.031030 USD. Total flat cost per passing run was 0.0271285 USD (0.406928 USD for all 18 runs divided by 15 passing runs). The cache-adjusted estimate was 0.1443603 USD total, 0.0080200 USD per episode on average, and 0.0121323 USD for the most expensive episode.

## Tokens, context and replies

| Model | Episode input mean / max | Episode output mean / max | Output share | Largest single-call input |
|---|---:|---:|---:|---:|
| Claude Sonnet 5.5 | 21378.1 / 28274 | 1010.6 / 1164 | 4.51% | 5542 |
| GPT-6.1 Sol | 9135.8 / 13115 | 433.6 / 480 | 4.53% | 2637 |
| Qwen 3 8B, local pilot | 23158.4 / 54407 | 8456.2 / 18910 | 26.75% | 9971 |

Claude's largest reported call input was 5,542 tokens, below its 1,000,000-token model profile. GPT's was 2,637, below its [1,050,000-token context](https://developers.openai.com/api/docs/models/gpt-6.1-sol). No call reached its model's context window. No call reported both input usage and refusal as null. No call's reported input fell below the previous call in its worker. The worker budget counted the provider-reported input and output tokens, including reported cache tokens. These records do not independently prove that every token read by a provider was reported.

No reply ended on the output limit, no provider refusal was recorded, and no worker raised a provider error. Claude reported 68 reasoning tokens and two thinking content blocks across 104 replies. Its stop reasons were 86 `tool_use` and 18 `end_turn`. GPT reported zero reasoning tokens across 99 replies; all 99 response statuses were `completed`.

## Cost and cache accounting

The reviewer expected about 2.40 USD per model and set a 12.60 USD cap per study. FiveFold counted 0.951520 USD for Claude and 0.406928 USD for GPT, both below those figures. These totals exclude the separately capped smoke tests.

The meter and evaluator apply the price file's 2 USD input and 10 USD output rates to every reported token. Claude reported no cache reads or writes, so its cache-adjusted estimate is also 0.951520 USD. GPT reported 141,153 cached-read tokens and 11,246 cache-write tokens. At the [published GPT rates](https://developers.openai.com/api/docs/pricing), 0.10 USD per million reads and 2.50 USD per million default 30-minute writes, the estimate is 0.1443603 USD. Its responses reported the default service tier; no cache setting was supplied. The [provider formula and default lifetime](https://developers.openai.com/api/docs/guides/prompt-caching) support this calculation. This is an estimate from response usage, not a provider invoice.

Flat-rate accounting can overcount cache reads and undercount cache writes. Consequently the meter's cap is not an exact ceiling on the provider bill, even before the in-flight-call overshoot described in `docs/limitations.md`. The cap checked in these studies did not reach 12.60 USD.

## Playback

All 36 study cassettes replayed and matched their original databases. None failed playback. The separately recorded smoke cassettes are not included in the raw data.
