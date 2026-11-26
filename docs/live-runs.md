# Live runs

Live runs are manual and never run in CI. A live run is refused unless the environment sets `FIVEFOLD_LIVE=1` and the command gets a spend cap in USD and a price file that prices the model (records 0004 and 0009). `--live PROVIDER:MODEL` names the model. The providers:

- `ollama:MODEL`, a model on an Ollama server: no key. The server is your own at `http://127.0.0.1:11434` unless `OLLAMA_HOST` names another.
- `anthropic:MODEL`, Anthropic's API: your key in `ANTHROPIC_API_KEY`.
- `openai:MODEL`, OpenAI's API: your key in `OPENAI_API_KEY`.
- `google_genai:MODEL`, Google's Gemini API: your key in `GOOGLE_API_KEY` or `GEMINI_API_KEY`. The run refuses `GOOGLE_GENAI_USE_VERTEXAI` and `GOOGLE_GENAI_USE_ENTERPRISE`, which can switch to Vertex AI with Google's default credentials. It refuses `GOOGLE_GENAI_CLIENT_MODE`, which can switch on the SDK's recording and replay modes.

A live run with an API provider needs its key variable set. `--param` takes generation parameters only, so a key, a base URL or a header can never reach a cassette or the run's records through those parameters. A live run refuses `LANGSMITH_GATEWAY` and enabled LangSmith tracing.

The price file must price the model under its provider, in USD per million input tokens and per million output tokens. Use prices you have checked on the provider's price list, and set `price_date` to the day you read them: the spend cap and the report's cost are only as right as those prices. `prices/example.yaml` prices local Ollama models at 0 USD and no API model.

To run demo 1 in G2 with a local model:

1. Install [Ollama](https://ollama.com) and pull a model that can call tools: `ollama pull MODEL`.
2. Copy `prices/example.yaml` to `prices.yaml`, and add `MODEL` under `ollama:` at 0 USD, quoted, as the examples are.
3. Record the episode. `CAP` is the spend cap in USD; a local model costs nothing, so any cap above 0 lets it run. If the Ollama server is not at `http://127.0.0.1:11434`, set `OLLAMA_HOST`.

   ```
   FIVEFOLD_LIVE=1 uv run python -m fivefold.recorder record --demo 1 --config G2 \
       --cassette live-1-g2.json --live ollama:MODEL --spend-cap-usd CAP --prices prices.yaml
   ```

4. Play it back, with no network and no flag: `uv run python -m fivefold.recorder play live-1-g2.json --scenario scenarios/dev/dev-001-single-po.yaml`.

Use `--database NAME` for an unused destination when recording or playing back another run. `--replace` works on destinations with FiveFold's marker or its complete matching migration history. The recorder refuses to replace unrelated databases and verifies playback's original run before creating its replay database.

The record command prints the verdict, the cassette's digest and what the run spent. Each model call takes the scenario's `think_ms` of virtual time, however long the model really takes, and `harness.model_call` logs its tokens. `--param NAME=VALUE`, such as `--param temperature=0`, sets a model parameter; the cassette records it. Before each call, the spend so far is checked against the cap, and a refused call ends that agent worker as `budget`. A call the provider has not answered after `--call-limit-s` wall-clock seconds (default 600) fails, and its worker ends with an error, so a stalled server cannot hold the run.

A live study takes the same options and is refused without them, before it touches any database. It also saves each episode's cassette in `cassettes/`; `python -m fivefold.recorder play` compares a replay with the original run's database, so give `--keep-databases` to play one back. One spend cap holds across the study, and the study stops once the spend reaches it, since every later model call would be refused. An episode the cap cut short keeps its cassette, and its result goes to `spend-cap/`, which a report over the directory leaves out.

The [local Ollama pilot](report/pilot-ollama.md) and [API pilot](report/pilot-api.md) each describe 18 episodes per model. Their raw data contains the commands, results and cassettes: [local](../results/pilot-ollama/README.md) and [API](../results/pilot-api/README.md). The [full study](report.md) covers 741 episodes per model, with its [raw data and commands](../results/study/README.md). The example below uses placeholders for a new study.

```
FIVEFOLD_LIVE=1 uv run python -m fivefold.harness.study --out pilot \
    --cells none F2 --contracts C2 --episodes 3 \
    --live ollama:MODEL --spend-cap-usd CAP --prices prices.yaml
uv run python -m fivefold.evaluator report pilot --csv pilot.csv --markdown pilot.md
```

The study prints each episode's verdict next to the one `faults/expected.yaml` expects for its cell, how each agent worker ended, and in a live study the episode's model calls and the spend so far.
