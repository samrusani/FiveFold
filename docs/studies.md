# Studies and reports

`python -m fivefold.harness.study` runs the episodes of milestone M4. An episode is an expected-outcome cell or a cell with no fault, run with one seed. By default the study runs the dev-001 cells in `faults/expected.yaml` for B0, G1 and G2 at C0 to C3, once each, with the scripted model. Each episode runs in a fresh database, dropped once it ends.

To use the 80 F8 expectations in `faults/expected-f8.yaml`, select the memory
scenario and all five configurations. B1 needs `pip install "fivefold[temporal]"`.
Run `make temporal-setup` in the checkout or set `FIVEFOLD_TEMPORAL_CLI` to
the pinned Temporal CLI binary. B2 needs `pip install "fivefold[dbos]"` and
runs on Linux and macOS. Both use the scripted model.
[Fault catalog](faults.md#f8-in-the-memory-scenario)

```sh
uv run python -m fivefold.harness.study \
  --scenario scenarios/dev/dev-001-memory-po.yaml \
  --configs B0 B1 B2 G1 G2 --cells F8 --out f8-study
```

`--cells` takes `all`, `none` (no fault), a fault (`F2`), a fault and variant (`F11-after_tool_call`) or a cell (`F2-C2-G2`). `--configs` and `--contracts` narrow the matrix. `--episodes N` runs each cell with the seeds S to S+N-1, where `--seed S` defaults to 7. Episodes run in a fixed order, by seed and then by cell, and each result is saved as `CELL-seed-SEED.json`, such as `F2-C0-G2-seed-7.json`. The same inputs with the scripted model give the same bytes, apart from the wall-clock latencies of recovery (see below). The output directory must be new or empty. The fault plans apply to the scenario named in their expected-outcome file. A study refuses to start if any of its episode databases already exists, such as one another study kept, rather than replace it. It never drops a database it did not create: if another study creates an episode's database after that check, the study stops at that episode with one line naming the database, and keeps the results it has.

The evaluator writes each run's result as JSON, with the input and output tokens its model calls used (`tokens_in` and `tokens_out`; a scripted run uses none). `uv run python -m fivefold.harness.demo --out results` saves one file per demo run. A rerun replaces its fixed-name demo databases if they have FiveFold's marker or its complete matching migration history. The demos refuse unrelated databases. `uv run python -m fivefold.evaluator report DIR --csv runs.csv --markdown report.md` turns the result files in `DIR` into a CSV with one row per run and the Markdown report. The report needs no database, and the same results always give the same bytes.

The CSV adds a leading apostrophe to text cells whose first character after any spaces is `=`, `+`, `-`, `@`, a tab, a carriage return or a line feed. This marks them as text for spreadsheet readers. Numeric columns keep their values, including negative seeds. See [OWASP's CSV injection guidance](https://owasp.org/www-community/attacks/CSV_Injection).

Each result also holds recovery latency (record 0014) for every task intent a fault touched: from the first fault that touched it until the stack's evidence settles the outcome found at the end, in virtual and in wall-clock milliseconds. If an effect committed, recovery ends at the first write answer, status answer or attestation that names one. If none did, it ends when the last of the intent's requests is first shown never to commit, or at the fault when the stack sent none. Evidence the stack held before the fault gives 0 ms. An intent whose outcome the evidence never settles, such as F2 at C0 with no resolver, is unresolved: it has no latency and counts apart. On seed 7 with F2 at C2, recovery takes 2,013 ms in B0, which places a second order and fails properties 1, 2 and 3, and 2,257 ms in G2, which retries with the same key and passes: latency alone does not rank configurations.

The metrics count `affected_intents` and `unresolved_intents`. The CSV adds `recovery_ms_max` and `recovery_wall_ms_max`, the largest latencies among a run's resolved intents. The report adds a table by fault plan, configuration and contract level: affected, resolved and unresolved intents, then the median and largest virtual latency and the median wall-clock latency over the resolved ones, with the lower middle value for an even count. Latencies are never summed. Wall-clock latency depends on the machine, so playback, the determinism check and the report's check for a result given twice all leave it out.

Add `--prices prices.yaml`, a price file in the format of `prices/example.yaml`, to price the tokens. The CSV then gains `cost_usd`, each run's cost in USD as an exact decimal. The report gains a cost table by model, configuration and contract level: runs, passing runs, tokens in and out, total cost and cost per passing run, at the price file's date. A passing run evaluated all five properties and all five hold, so its verdict is pass or pass_unresolved. Cost per passing run is the cost of all the row's runs, failed ones too, over its passing runs. The scripted model costs nothing, and a run of a model the price file does not price is refused. Without a price file, the report says that cost is not computed.

## Clean up DBOS state after a hard kill

B2 normally removes its per-run state database. If the run is hard-killed,
find its `fivefold_dbos_*/owner.json` in Python's temporary directory
(`python -c "import tempfile; print(tempfile.gettempdir())"`). The record
contains the database name and PostgreSQL OID. Stop the run's workers,
use the same `FIVEFOLD_ADMIN_DSN` as the run, and pass that run's
`owner.json` to this command:

```sh
uv run python - /path/to/fivefold_dbos_RUN/owner.json <<'PYTHON'
import json
import os
import sys
from pathlib import Path

from fivefold.harness.migrations import DEFAULT_ADMIN_DSN, database_lock, drop_database_locked

owner = Path(sys.argv[1])
state = json.loads(owner.read_text())
name, oid = state["name"], state["oid"]
if (
    not isinstance(name, str)
    or not name.startswith("fivefold_dbos_")
    or type(oid) is not int
    or oid <= 0
):
    raise SystemExit("The ownership record needs a DBOS database name and its OID.")
dsn = os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_ADMIN_DSN)
with database_lock(dsn) as connection:
    if not drop_database_locked(connection, name, oid=oid):
        raise SystemExit("The database is absent or its OID differs. Kept the ownership record.")
owner.unlink()
owner.parent.rmdir()
PYTHON
```

The cleanup holds FiveFold's database lock and drops the database only
when its OID still matches the ownership record (0020).
