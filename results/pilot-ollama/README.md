# Local Ollama pilot data

The pilot ran on 17 Nov 2026, from main commit `56dc7c5` with the harness fix in commit `aaabb88`. Ollama was version `0.40.15`. The models were `llama3.1:8b` (ID `46e0c10c039e`) and `qwen3:8b` (ID `500a1f067a9f`). Ollama lists both as supporting tools: [Llama 3.1](https://ollama.com/library/llama3.1:8b) and [Qwen 3](https://ollama.com/library/qwen3:8b).

Each study used dev-001, C2, B0, G1 and G2, the no-fault and F2 cells, and seeds 7 through 9. The scenario's budget remained 60,000 tokens and 40 steps per worker. The only generation parameter was `num_ctx=32768`; all others used their defaults. Both local models were priced at 0 USD in `prices/example.yaml`. Each command had a 1 USD spend cap.

With `FIVEFOLD_ADMIN_DSN` set to a PostgreSQL 16 admin connection, the study commands were:

```sh
FIVEFOLD_LIVE=1 uv run python -m fivefold.harness.study \
    --out results/pilot-ollama/llama3.1-8b \
    --cells none F2 --contracts C2 --episodes 3 \
    --database-prefix pilot_llama31 --keep-databases \
    --live ollama:llama3.1:8b --spend-cap-usd 1 --prices prices/example.yaml \
    --param num_ctx=32768

FIVEFOLD_LIVE=1 uv run python -m fivefold.harness.study \
    --out results/pilot-ollama/qwen3-8b \
    --cells none F2 --contracts C2 --episodes 3 \
    --database-prefix pilot_qwen3 --keep-databases \
    --live ollama:qwen3:8b --spend-cap-usd 1 --prices prices/example.yaml \
    --param num_ctx=32768
```

The study processes were kept awake and their output was captured separately. The report commands were:

```sh
uv run python -m fivefold.evaluator report results/pilot-ollama/llama3.1-8b \
    --csv results/pilot-ollama/llama3.1-8b.csv \
    --markdown results/pilot-ollama/llama3.1-8b.md --prices prices/example.yaml

uv run python -m fivefold.evaluator report results/pilot-ollama/qwen3-8b \
    --csv results/pilot-ollama/qwen3-8b.csv \
    --markdown results/pilot-ollama/qwen3-8b.md --prices prices/example.yaml
```

For playback, each cassette was checked against the database named by its study's episode number. For example, the first Llama episode uses:

```sh
uv run python -m fivefold.recorder play \
    results/pilot-ollama/llama3.1-8b/cassettes/none-C2-B0-seed-7.json \
    --scenario scenarios/dev/dev-001-single-po.yaml \
    --original pilot_llama31_0001 --database pilot_llama31_play_0001
```

The same command form uses `pilot_qwen3_NNNN` for Qwen and the matching cassette and episode number for each run. All 36 cassettes matched. `playback.json` records the cassette, original and playback database for each check. Smoke cassettes are not part of this data set.
