# Full study data

The studies started on 17 Nov 2026 and finished on 17 Nov 2026. They ran from main commit `ef2f404` with uv 0.13.8, Python 3.12 and PostgreSQL 16. The locked provider packages were `anthropic` 2.8.0, `langchain-anthropic` 1.7.10, `langchain-openai` 1.8.0, `openai` 3.51.0.

Each model ran dev-001 with all 235 expected fault cells and 12 no-fault cells, at seeds 7, 8 and 9: 741 episodes per model. The scenario budget remained 60,000 tokens and 40 steps per worker. The only generation parameter was `max_tokens=16000`. No sampling, thinking or reasoning setting was supplied.

With `FIVEFOLD_ADMIN_DSN` set to a PostgreSQL 16 admin connection and provider keys available through the ignored `.env`, the study commands were:

```sh
FIVEFOLD_LIVE=1 uv run --env-file .env python -m fivefold.harness.study \
    --out results/study/claude-sonnet-5-5 \
    --cells all none --episodes 3 \
    --database-prefix study_sonnet55 --keep-databases \
    --live anthropic:claude-sonnet-5-5 --spend-cap-usd 120 \
    --prices prices/api-2026-11-17.yaml --param max_tokens=16000

FIVEFOLD_LIVE=1 uv run --env-file .env python -m fivefold.harness.study \
    --out results/study/gpt-6.1-sol \
    --cells all none --episodes 3 \
    --database-prefix study_gpt61sol --keep-databases \
    --live openai:gpt-6.1-sol --spend-cap-usd 50 \
    --prices prices/api-2026-11-17.yaml --param max_tokens=16000

```

The study processes were kept awake. A temporary local gate checked each first line and the stop conditions before the next episode. The gate did not alter the model, scenario, tools or replay check.

Final spend lines:

- Model anthropic:claude-sonnet-5-5; 44.281882 USD spent of the 120 USD cap on 4764 model calls, at prices dated 2026-11-17.
- Model openai:gpt-6.1-sol; 18.937724 USD spent of the 50 USD cap on 4537 model calls, at prices dated 2026-11-17.

The spend meter applies the price file's flat input and output rates to reported usage. Cache reads and writes can be billed at different rates, so the meter is not an invoice or a hard billing ceiling. See the [API pilot](../../docs/report/pilot-api.md).

The evaluator commands were:

```sh
uv run python -m fivefold.evaluator report results/study/claude-sonnet-5-5 \
    --csv results/study/claude-sonnet-5-5.csv \
    --markdown results/study/claude-sonnet-5-5.md --prices prices/api-2026-11-17.yaml

uv run python -m fivefold.evaluator report results/study/gpt-6.1-sol \
    --csv results/study/gpt-6.1-sol.csv \
    --markdown results/study/gpt-6.1-sol.md --prices prices/api-2026-11-17.yaml

```

`episodes.csv` maps each episode to its result file, scripted expectation, counts and outcome. The two `*-records.json` files retain the selected original fault, effect and model-call records used in the summary. The study logs contain worker endings and final spend lines. `fault-patterns.csv` groups the differences, and `playback.json` records every replay against its original database.

All cassettes are committed as plain files because their combined size, 24,365,520 bytes, was under 25 MB.

One initial playback command for `F10-stale_within_lag-C1-G2-seed-7` was refused before the replay database was created: it supplied the base scenario, whose status lag is 0 ms, while that cell was recorded with a 3,000 ms lag. The rejected attempt is in `playback-attempts.json`; the scenario derived with `cell_scenario` has the cassette digest, and the resumed replay and remaining results are in `playback.json`.

For any F10 `stale_within_lag` cassette, derive the scenario that the study used before replay:

```sh
uv run python - <<'PY'
from pathlib import Path
import yaml
from fivefold.domain.scenario import load_scenario_file
from fivefold.harness.cells import cell_scenario, load_expected

base = load_scenario_file("scenarios/dev/dev-001-single-po.yaml")
cell = next(c for c in load_expected().cells if c.name == "F10-stale_within_lag-C1-G2")
scenario = cell_scenario(cell, base)
Path("f10-stale-within-lag.yaml").write_text(yaml.safe_dump(scenario.model_dump(mode="json"), sort_keys=False))
PY
uv run python -m fivefold.recorder play \
    results/study/claude-sonnet-5-5/cassettes/F10-stale_within_lag-C1-G2-seed-7.json \
    --scenario f10-stale-within-lag.yaml \
    --original study_sonnet55_0203 --database study_sonnet55_example_0203
```

Choose a fresh playback database name and remove the generated scenario file after use. The other cassettes use the unchanged base scenario.

Each cassette can be played against the kept original database using its episode number. For example:

```sh
uv run python -m fivefold.recorder play \
    results/study/claude-sonnet-5-5/cassettes/none-C0-B0-seed-7.json \
    --scenario scenarios/dev/dev-001-single-po.yaml \
    --original study_sonnet55_0001 --database study_sonnet55_example_0001
```

Use a fresh `--database` name if that playback database already exists. GPT originals use `study_gpt61sol_NNNN`. The playback results give the cassette and original database for every episode.
