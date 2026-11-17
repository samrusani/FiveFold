# API pilot data

The pilot ran on 17 Nov 2026 from main commit `2d8578f`. The models were `claude-sonnet-5-5` and `gpt-6.1-sol`. OpenAI's model documentation listed no dated snapshot for the latter. The locked package versions were `anthropic` 2.8.0, `openai` 3.51.0, `langchain-anthropic` 1.7.10 and `langchain-openai` 1.8.0.

Each study used dev-001, C2, B0, G1 and G2, no fault and F2, and seeds 7 through 9. The scenario's budget remained 60,000 tokens and 40 steps per worker. The only generation parameter was `max_tokens=16000`. No sampling, thinking or reasoning setting was supplied. The dated price file is `prices/api-2026-11-17.yaml`; its prices are USD 2 per million input tokens and USD 10 per million output tokens.

With `FIVEFOLD_ADMIN_DSN` set to a PostgreSQL 16 admin connection and the provider keys set in their environment variables, the study commands were:

```sh
FIVEFOLD_LIVE=1 uv run python -m fivefold.harness.study \
    --out results/pilot-api/claude-sonnet-5-5 \
    --cells none F2 --contracts C2 --episodes 3 \
    --database-prefix pilot_sonnet55 --keep-databases \
    --live anthropic:claude-sonnet-5-5 --spend-cap-usd 12.60 \
    --prices prices/api-2026-11-17.yaml --param max_tokens=16000

FIVEFOLD_LIVE=1 uv run python -m fivefold.harness.study \
    --out results/pilot-api/gpt-6.1-sol \
    --cells none F2 --contracts C2 --episodes 3 \
    --database-prefix pilot_gpt61sol --keep-databases \
    --live openai:gpt-6.1-sol --spend-cap-usd 12.60 \
    --prices prices/api-2026-11-17.yaml --param max_tokens=16000
```

Final spend lines from the study commands:

- Model anthropic:claude-sonnet-5-5; 0.951520 USD spent of the 12.60 USD cap on 104 model calls, at prices dated 2026-11-17.
- Model openai:gpt-6.1-sol; 0.406928 USD spent of the 12.60 USD cap on 99 model calls, at prices dated 2026-11-17.

The spend meter applies the price file's flat input and output rates to reported usage. Cache reads and writes can be billed at different rates. The accompanying pilot report explains the difference.

The report commands were:

```sh
uv run python -m fivefold.evaluator report results/pilot-api/claude-sonnet-5-5 \
    --csv results/pilot-api/claude-sonnet-5-5.csv \
    --markdown results/pilot-api/claude-sonnet-5-5.md --prices prices/api-2026-11-17.yaml
uv run python -m fivefold.evaluator report results/pilot-api/gpt-6.1-sol \
    --csv results/pilot-api/gpt-6.1-sol.csv \
    --markdown results/pilot-api/gpt-6.1-sol.md --prices prices/api-2026-11-17.yaml
```

For playback, each cassette was checked against the database named by its study episode number. For example:

```sh
uv run python -m fivefold.recorder play \
    results/pilot-api/claude-sonnet-5-5/cassettes/none-C2-B0-seed-7.json \
    --scenario scenarios/dev/dev-001-single-po.yaml \
    --original pilot_sonnet55_0001 --database pilot_sonnet55_play_0001
```

The same command form uses `pilot_gpt61sol_NNNN` for GPT and the matching cassette and episode number for each run. All 36 cassettes matched; `playback.json` records each check. Smoke cassettes are not included.
