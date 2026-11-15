"""The evaluator's outputs across runs: a CSV with one row per run, and a Markdown report
(0004, Metrics).

Each run's result is one JSON file, as `result_json` writes it. `load_results` reads the
files and directories it is given, and refuses any file that is not a valid `RunResult`.
`runs_csv` and `markdown_report` sort the runs before they write anything, so the same
results give the same bytes whatever order they come in (0009). They add no date, no
wall-clock time and no randomness; a result holds none either. The one date in the report
is the price file's own.

The CSV has a header row and the columns of `CSV_COLUMNS`, in that order. Its rows are
sorted by scenario, configuration, contract level, planned faults and seed, then by the
rest of the run's identity. The report says what was run. It counts verdicts, and safe
and live runs, by configuration and contract level, failures by property and
configuration, and verdicts by fault plan, each count over its denominator. It sums every
metric by configuration, and names the metric of record 0004 that is not computed yet.

Given a price file (`fivefold.domain.prices`), the CSV gains the column `cost_usd`, each
run's cost, and the report a cost table by model, configuration and contract level, with
the price file's date. Without one, the report says that cost is not computed, and shows
no value for it.

Readings the decision records leave open:

- A run's planned faults are the fault plan the harness records in the run's params
  (`params.plan`), with the parameters the run drew from its seed. A run whose params
  hold no plan shows `not recorded`. A plan that does not read as planned faults is
  refused.
- The fault table groups runs by their planned faults without the faults' parameters, so
  the trials of one cell share a row even where a fault draws a parameter from the seed
  (F4's `copy_delay_ms`). The CSV keeps the parameters.
- Run ids are not unique: runs that differ only in their fault plan share one (demos 2a
  and 2b). So nothing here keys on the run id alone. Two files that hold the very same
  result are refused, so that no run counts twice.
- A result must hold exactly the metrics in `METRICS`. Any other set is refused, so a
  total never mixes runs that computed a metric with runs that did not. Every metric is a
  count, so a negative one is refused too.
- A run's cost is its input tokens at its model's input price plus its output tokens at
  its output price, in USD, as exact decimals: never a float, never rounded. A cost that
  needs more digits than `EXACT` has is refused rather than rounded. The model is priced
  by the name `harness.run.model` records, `provider:model` for a live model, so a replay
  costs what the calls it plays back cost. The scripted model makes no calls to any
  provider and reports no tokens (0009), so it costs 0 USD and needs no price. Any other
  model the price file does not price is refused.
- A reply that reported no usage counts no tokens, so its cost is left out. With a model
  that is not free, the live adapter's spend cap then refuses every later call of the
  recording command (`fivefold.agents.live`), so at most one such call per command is
  left out. A call the spend cap refused was never sent and costs nothing.
- A passing run is one whose verdict is `pass` or `pass_unresolved`: all five properties
  hold in both. Cost per passing run is the cost of all a row's runs, passing or not, over
  its passing runs: what one passing run costs, failures included. With no passing run it
  shows `no passing run`. It is exact when the division ends within `EXACT`'s digits, and
  otherwise rounded half to even to 6 significant digits and marked `about`.
- The cost table groups runs by model as well as by configuration and contract level, so
  that no row mixes the prices of two models.
- An experimental branch (0009) has its parent's scenario, configuration, contract level,
  seed and fault plan, so it would count in its parent's cell, as one more trial of it.
  It is no such trial: it repeats its parent's first K model calls, and it may answer the
  rest with another model. So every table shows a branch apart, in rows of its own, by
  configuration and branch point: `G2, branch point 4` (`configuration`). Runs that are
  no branch show as before, and so does a report with no branch. The CSV keeps one row
  per run, with its parent and branch point.
- A branch's tokens are those of all its model calls, the ones it replayed from its
  parent included, as the harness logs them under the branch's model. Its cost prices
  them all at that model's prices: exact when the replayed calls report no tokens, as the
  scripted model's never do, or when the parent ran the same model.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fivefold.domain.prices import ModelPrice, PriceFile, PriceFileError, load_price_file
from fivefold.evaluator.checks import CLAIM_METRICS
from fivefold.evaluator.metrics import RUN_METRICS, TOKEN_METRICS
from fivefold.evaluator.result import (
    PROPERTY_NAMES,
    PropertyVerdict,
    RunResult,
    RunSummary,
    Verdict,
    result_json,
)

PROPERTY_METRICS = (
    "duplicates",
    "unintended_effects",
    "unauthorized_effects",
    "stale_effects",
    "missing_effects",
    "excused_intents",
)
"""The metrics of properties 1 to 4, in the order of record 0004's list."""

METRICS: tuple[str, ...] = (*PROPERTY_METRICS, *CLAIM_METRICS, *RUN_METRICS, *TOKEN_METRICS)
"""Every metric a result holds, in the order of record 0004's list: 16 in all."""

NOT_COMPUTED = "recovery latency per affected action, in virtual and wall milliseconds"
"""The metric of record 0004 that the evaluator does not compute yet: it waits for record
0014."""

PROPERTIES = tuple(sorted(PROPERTY_NAMES))
"""The five properties, by number."""

VERDICTS = (Verdict.PASS, Verdict.PASS_UNRESOLVED, Verdict.FAIL)
"""The run verdicts, in the order the tables show them."""

PASSING = frozenset({Verdict.PASS, Verdict.PASS_UNRESOLVED})
"""The verdicts of a passing run: all five properties hold (0004, Verdict)."""

SAFETY = (1, 2, 3, 5)
"""The safety properties: nothing bad happened (0004)."""

LIVENESS = (4,)
"""The liveness property: the required effect happened (0004)."""

NO_FAULTS = "none"
"""How a run with an empty fault plan shows its planned faults."""

UNRECORDED = "not recorded"
"""How a run whose params hold no fault plan shows its planned faults."""

BRANCH_NOTE: Final = (
    " A branch repeats its parent's first model calls, so the tables never count it in its"
    " parent's cell: they show it in rows of its own, by configuration and branch point, the"
    " number of its parent's model calls it replayed, such as `G2, branch point 4`. Its tokens"
    " include those of the calls it replayed, and its cost prices them at its own model's"
    " prices."
)
"""What the report says about branches, when it has any (0009)."""

CSV_COLUMNS: tuple[str, ...] = (
    "run_id",
    "scenario_id",
    "scenario_digest",
    "config",
    "contract",
    "seed",
    "topology",
    "model_adapter",
    "model",
    "cassette_digest",
    "parent_run_id",
    "branch_point",
    "code_version",
    "planned_faults",
    "end_vt",
    "verdict",
    *(f"p{number}_{PROPERTY_NAMES[number]}" for number in PROPERTIES),
    "reason_codes",
    *METRICS,
)
"""The CSV's columns, in order. A property's column is empty when the run did not
evaluate it, and so are `cassette_digest`, `parent_run_id` and `branch_point` when the run
has none. Given a price file, `COST_COLUMN` comes last."""

COST_COLUMN: Final = "cost_usd"
"""The CSV's last column when a price file is given: the run's cost in USD, exactly."""

SCRIPTED_MODEL: Final = "scripted"
"""The scripted model's name in `harness.run.model`: the policy of record 0008, which
makes no calls to any provider and reports no tokens (0009). It costs 0 USD."""

EXACT: Final = Context(
    prec=50,
    rounding=ROUND_HALF_EVEN,
    traps=[InvalidOperation, DivisionByZero, Overflow, Inexact],
)
"""The arithmetic of costs: 50 significant digits, and an error instead of any rounding.
Only copies are ever used (`localcontext`), so its flags never change."""

ROUNDED: Final = Context(
    prec=6, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow]
)
"""How a cost per passing run that does not end is rounded: to 6 significant digits, half
to even."""


class ReportError(Exception):
    """A result the outputs refuse, a path that names no result, or a price file the
    outputs cannot use."""


# Planned faults --------------------------------------------------------------------------


class PlannedTrigger(BaseModel):
    """Where and when a planned fault fires, as the harness records it (0006, Plans)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    point: str
    match: dict[str, str | int] = Field(default_factory=dict)
    occurrence: int = 1


class PlanEntry(BaseModel):
    """One fault of a run's plan, as the harness records it in the run's params."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fault: str
    variant: str | None = None
    at: PlannedTrigger
    params: dict[str, str | int] = Field(default_factory=dict)

    def label(self, *, params: bool = True) -> str:
        """The fault in words: its id, variant, point, match and occurrence, then its
        parameters if `params`. For example `F11 at agent.after_tool_call when
        tool=submit_purchase_order with restart_delay_ms=10000`. The occurrence shows
        only when it is not the first."""
        words = [self.fault]
        if self.variant is not None:
            words.append(self.variant)
        words += ["at", self.at.point]
        if self.at.match:
            words += ["when", _pairs(self.at.match)]
        if self.at.occurrence != 1:
            words += ["occurrence", str(self.at.occurrence)]
        if params and self.params:
            words += ["with", _pairs(self.params)]
        return " ".join(words)


def _pairs(values: dict[str, str | int]) -> str:
    return ", ".join(f"{name}={values[name]}" for name in sorted(values))


def planned_faults(result: RunResult) -> tuple[PlanEntry, ...] | None:
    """The faults the run planned, from its params; None when its params hold no plan.

    Raises `ReportError` when the plan does not read as a list of planned faults.
    """
    params = result.run.params
    if not isinstance(params, dict) or "plan" not in params:
        return None
    plan = params["plan"]
    if not isinstance(plan, list):
        raise ReportError(f"run {result.run.run_id}: the plan in its params is not a list")
    entries: list[PlanEntry] = []
    for number, entry in enumerate(plan, start=1):
        try:
            entries.append(PlanEntry.model_validate(entry))
        except ValidationError as error:
            raise ReportError(
                f"run {result.run.run_id}: planned fault {number} does not read as a fault:"
                f" {_first_error(error)}"
            ) from None
    return tuple(entries)


def configuration(run: RunSummary) -> str:
    """A run's configuration as the tables show it: as it is, or for an experimental branch
    (0009), with its branch point, such as `G2, branch point 4`, so that a branch never
    counts in its parent's cell."""
    if run.branch_point is None:
        return run.config
    return f"{run.config}, branch point {run.branch_point}"


def plan_label(plan: Sequence[PlanEntry] | None, *, params: bool = True) -> str:
    """A run's planned faults in words, in plan order, joined by ` + `: `none` for an
    empty plan, `not recorded` when the run recorded none."""
    if plan is None:
        return UNRECORDED
    if not plan:
        return NO_FAULTS
    return " + ".join(entry.label(params=params) for entry in plan)


# Loading results -------------------------------------------------------------------------


def check_result(result: RunResult) -> None:
    """Refuse a result the outputs cannot show as it is.

    Its properties must be the ones it says it evaluated, in that order, each once and
    numbered 1 to 5. Its metrics must be exactly `METRICS`, none negative. The plan in its
    params, if any, must read as planned faults.
    """
    name = f"run {result.run.run_id}"
    numbers = tuple(prop.property for prop in result.properties)
    if numbers != result.evaluated:
        raise ReportError(
            f"{name}: it evaluated properties {list(result.evaluated)} but holds results for"
            f" {list(numbers)}"
        )
    if len(set(numbers)) != len(numbers) or not set(numbers) <= set(PROPERTIES):
        raise ReportError(f"{name}: its properties must be distinct and from 1 to 5: {numbers}")
    missing = [metric for metric in METRICS if metric not in result.metrics]
    unknown = sorted(set(result.metrics) - set(METRICS))
    if missing or unknown:
        raise ReportError(
            f"{name}: its metrics are not the {len(METRICS)} of record 0004: missing"
            f" {missing or 'none'}, unknown {unknown or 'none'}"
        )
    negative = [metric for metric in METRICS if result.metrics[metric] < 0]
    if negative:
        raise ReportError(f"{name}: its metrics are counts, but {negative} are negative")
    planned_faults(result)


def load_result(path: Path) -> RunResult:
    """Read one run's result from the JSON file `path`.

    Raises `ReportError` when the file cannot be read, is not a valid `RunResult`, or
    fails `check_result`.
    """
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ReportError(f"{path}: cannot read it: {error.strerror or error}") from None
    try:
        result = RunResult.model_validate_json(data)
    except ValidationError as error:
        raise ReportError(f"{path} is not a valid run result: {_first_error(error)}") from None
    try:
        check_result(result)
    except ReportError as error:
        raise ReportError(f"{path}: {error}") from None
    return result


def result_files(paths: Iterable[Path]) -> tuple[Path, ...]:
    """The result files `paths` name: each file, and each `.json` file directly in each
    directory. A file named twice counts once. The order is fixed: by resolved path.

    Raises `ReportError` for a path that is neither a file nor a directory, and for a
    directory that holds no `.json` file.
    """
    found: dict[Path, Path] = {}
    for path in paths:
        if path.is_dir():
            files = sorted(
                child for child in path.iterdir() if child.suffix == ".json" and child.is_file()
            )
            if not files:
                raise ReportError(f"{path} holds no .json file")
        elif path.is_file():
            files = [path]
        else:
            raise ReportError(f"{path} is neither a file nor a directory")
        for file in files:
            found.setdefault(file.resolve(), file)
    return tuple(found[resolved] for resolved in sorted(found))


def load_results(paths: Iterable[Path]) -> tuple[RunResult, ...]:
    """Every result in the files and directories `paths` name (`result_files`).

    Raises `ReportError` when there is none, when a file is refused (`load_result`), and
    when two files hold the same result, which would count one run twice.
    """
    files = result_files(paths)
    if not files:
        raise ReportError("no result files given")
    seen: dict[str, Path] = {}
    results: list[RunResult] = []
    for path in files:
        result = load_result(path)
        digest = hashlib.sha256(result_json(result).encode("utf-8")).hexdigest()
        if digest in seen:
            raise ReportError(f"{seen[digest]} and {path} hold the same result")
        seen[digest] = path
        results.append(result)
    return tuple(results)


def _first_error(error: ValidationError) -> str:
    """The first problem pydantic found, where it is, and how many more there are."""
    details = error.errors(include_url=False)
    if not details:
        return str(error)
    first = details[0]
    where = ".".join(str(part) for part in first["loc"])
    text = f"{where}: {first['msg']}" if where else first["msg"]
    if len(details) > 1:
        text += f" (and {len(details) - 1} more)"
    return text


# Prices and cost -------------------------------------------------------------------------


def load_prices(path: Path) -> PriceFile:
    """Read the price file at `path` with `fivefold.domain.prices`.

    Raises `ReportError`, in one line, when the file cannot be read or is not a price file.
    """
    try:
        return load_price_file(path)
    except OSError as error:
        raise ReportError(f"{path}: cannot read it: {error.strerror or error}") from None
    except UnicodeDecodeError as error:
        raise ReportError(
            f"{path} is not a price file: it is not UTF-8 text: {error.reason}"
        ) from None
    except PriceFileError as error:
        raise ReportError(" ".join(str(error).split())) from None


def model_price(model: str, prices: PriceFile) -> ModelPrice | None:
    """The price of the live model `model`, named `provider:model` as `harness.run.model`
    names it, such as `ollama:llama3.1:8b`: the provider is the text before the first
    colon. None when the price file has no price for it."""
    provider, colon, name = model.partition(":")
    if not colon:
        return None
    return prices.prices.get(provider, {}).get(name)


def run_cost(result: RunResult, prices: PriceFile) -> Decimal:
    """The run's cost in USD, exactly: its input tokens at its model's input price plus its
    output tokens at its output price (0004, Metrics). The scripted model costs nothing.

    Raises `ReportError` when the price file has no price for the run's model, when a run of
    the scripted model used tokens, and when the cost needs more digits than `EXACT` has.
    """
    run = result.run
    tokens_in, tokens_out = (result.metrics[name] for name in TOKEN_METRICS)
    if run.model == SCRIPTED_MODEL:
        if tokens_in or tokens_out:
            raise ReportError(
                f"run {run.run_id}: the scripted model reports no tokens, but the run used"
                f" {tokens_in} in and {tokens_out} out"
            )
        return Decimal(0)
    price = model_price(run.model, prices)
    if price is None:
        raise ReportError(
            f"run {run.run_id}: the price file dated {prices.price_date.isoformat()} has no"
            f" price for its model {run.model}"
        )
    return _exactly(lambda: price.cost_usd(tokens_in, tokens_out), f"the cost of run {run.run_id}")


def usd(amount: Decimal) -> str:
    """An amount in USD as the outputs write it: every digit, in plain notation with no
    exponent and no trailing zeros, such as `0.00126` or `0`."""
    return _exactly(lambda: format(amount.normalize(), "f"), f"the amount {amount}")


def cost_per_passing_run(total: Decimal, passing: int) -> str:
    """`total`, the cost of a group's runs in USD, over its `passing` runs, as the report
    writes it: exact when the division ends within `EXACT`'s digits, otherwise rounded to 6
    significant digits after `about`, and `no passing run` when there is none."""
    if passing == 0:
        return "no passing run"
    try:
        with localcontext(EXACT):
            return usd(total / passing)
    except Inexact:
        with localcontext(ROUNDED):
            return f"about {usd(total / passing)}"


def _exactly[Value](compute: Callable[[], Value], what: str) -> Value:
    """`compute()` with the arithmetic of `EXACT`. Raises `ReportError` naming `what` when
    a result would need rounding."""
    try:
        with localcontext(EXACT):
            return compute()
    except Inexact:
        raise ReportError(
            f"{what} needs more than {EXACT.prec} significant digits, so it cannot be exact"
        ) from None


# Runs in a fixed order -------------------------------------------------------------------

Natural = tuple[tuple[str | int, ...], str]


def _natural(text: str) -> Natural:
    """A sort key that orders numbers inside text by value, so F2 comes before F10. The
    text itself breaks ties, so two different texts never sort as equal."""
    parts = re.split(r"(\d+)", text)
    return tuple(int(part) if index % 2 else part for index, part in enumerate(parts)), text


Order = tuple[Natural, str, Natural, Natural, Natural, int, Natural, str, str, str, str, str, str]
"""A run's place in the fixed order (`Row.order`)."""


@dataclass(frozen=True)
class Row:
    """One run, as both outputs show it."""

    result: RunResult
    faults: str
    """Its planned faults with their parameters, as the CSV shows them."""
    fault_group: str
    """Its planned faults without their parameters: its row of the fault table."""
    text: str
    """The result's JSON: the last key of the order, so the order is total."""
    cost: Decimal | None = None
    """Its cost in USD (`run_cost`), when a price file is given."""

    def order(self) -> Order:
        """Scenario, configuration, contract level, planned faults and seed, then the rest
        of the run's identity, then the whole result."""
        run = self.result.run
        return (
            _natural(run.scenario_id),
            run.scenario_digest,
            _natural(configuration(run)),
            _natural(run.contract),
            _natural(self.fault_group),
            run.seed,
            _natural(self.faults),
            run.model_adapter,
            run.model,
            run.cassette_digest or "",
            self.result.code_version,
            run.run_id,
            self.text,
        )


def _rows(results: Iterable[RunResult], prices: PriceFile | None = None) -> list[Row]:
    """The results as rows, checked and in the fixed order, each with its cost when
    `prices` is given. A run the price file cannot price is refused: the first in the
    fixed order, so the refusal does not depend on the order of the results."""
    rows: list[Row] = []
    for result in results:
        check_result(result)
        plan = planned_faults(result)
        rows.append(
            Row(
                result=result,
                faults=plan_label(plan),
                fault_group=plan_label(plan, params=False),
                text=result_json(result),
            )
        )
    rows.sort(key=Row.order)
    if prices is None:
        return rows
    return [replace(row, cost=run_cost(row.result, prices)) for row in rows]


# The CSV ---------------------------------------------------------------------------------


def runs_csv(results: Iterable[RunResult], prices: PriceFile | None = None) -> str:
    """The CSV across runs: a header row, then one row per run in the fixed order. Given
    `prices`, the last column, `cost_usd`, holds each run's cost in USD, exactly.

    Lines end with a line feed. Raises `ReportError` for a result `check_result` refuses,
    and for a run `run_cost` cannot price.
    """
    rows = _rows(results, prices)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS if prices is None else (*CSV_COLUMNS, COST_COLUMN))
    for row in rows:
        values = _csv_values(row)
        if row.cost is not None:
            values.append(usd(row.cost))
        writer.writerow(values)
    return buffer.getvalue()


def _csv_values(row: Row) -> list[str]:
    result = row.result
    run = result.run
    verdicts = {prop.property: str(prop.verdict) for prop in result.properties}
    return [
        run.run_id,
        run.scenario_id,
        run.scenario_digest,
        run.config,
        run.contract,
        str(run.seed),
        run.topology,
        run.model_adapter,
        run.model,
        _blank(run.cassette_digest),
        _blank(run.parent_run_id),
        _blank(run.branch_point),
        result.code_version,
        row.faults,
        str(run.end_vt),
        str(result.verdict),
        *(verdicts.get(number, "") for number in PROPERTIES),
        reason_codes(result),
        *(str(result.metrics[name]) for name in METRICS),
    ]


def _blank(value: str | int | None) -> str:
    return "" if value is None else str(value)


def reason_codes(result: RunResult) -> str:
    """Every reason code of the run's failed checks, once each, by property, joined by
    `; `. Each code belongs to one property, so the codes name the property too."""
    ordered = sorted(result.properties, key=lambda prop: prop.property)
    return "; ".join(str(code) for prop in ordered for code in prop.reason_codes)


# The Markdown report ---------------------------------------------------------------------


def markdown_report(results: Iterable[RunResult], prices: PriceFile | None = None) -> str:
    """The report across runs, in Markdown, with plain tables. Given `prices`, its cost
    section prices each run's tokens; without, it says that cost is not computed.

    Raises `ReportError` for a result `check_result` refuses, and for a run `run_cost`
    cannot price.
    """
    rows = _rows(results, prices)
    lines = [
        "# Fivefold results",
        "",
        f"The evaluator's results for {_runs(len(rows))} (record 0004). It decides every"
        " verdict from the simulator's and the harness's records. Each run's result is a"
        " JSON file, and the CSV across runs has one row per run.",
        "",
        *_what_was_run(rows),
        *_verdict_table(rows),
        *_safety_table(rows),
        *_failure_table(rows),
        *_fault_table(rows),
        *_metric_table(rows),
        *_cost_table(rows, prices),
        *_not_computed(),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def _what_was_run(rows: Sequence[Row]) -> list[str]:
    runs = [row.result.run for row in rows]
    scenarios = Counter((run.scenario_id, run.scenario_digest) for run in runs)
    models = Counter((run.model, run.model_adapter) for run in runs)
    seeds = Counter(run.seed for run in runs)
    with_cassette = sum(1 for run in runs if run.cassette_digest is not None)
    branched = sum(1 for run in runs if run.parent_run_id is not None)
    lines = [
        "## What was run",
        "",
        f"- Runs: {len(runs)}.",
        f"- Code versions: {_counted(row.result.code_version for row in rows)}.",
        "- Scenarios, with the SHA-256 digest each run recorded:" + ("" if scenarios else " none."),
    ]
    for scenario_id, digest in sorted(scenarios, key=lambda key: (_natural(key[0]), key[1])):
        lines.append(f"  - {scenario_id}, {digest} ({_runs(scenarios[scenario_id, digest])}).")
    model_list = ", ".join(
        f"{model} through the {adapter} adapter ({_runs(models[model, adapter])})"
        for model, adapter in sorted(models, key=lambda key: (_natural(key[0]), _natural(key[1])))
    )
    seed_list = ", ".join(f"{seed} ({_runs(seeds[seed])})" for seed in sorted(seeds))
    lines += [
        f"- Configurations: {_counted(run.config for run in runs)}.",
        f"- Contract levels: {_counted(run.contract for run in runs)}.",
        f"- Models: {model_list or 'none'}.",
        f"- Seeds: {seed_list or 'none'}.",
        f"- Topologies: {_counted(run.topology for run in runs)}.",
        f"- Runs that name a cassette: {with_cassette} of {len(runs)}.",
        f"- Runs branched from another run: {branched} of {len(runs)}."
        + (BRANCH_NOTE if branched else ""),
        "",
    ]
    return lines


def _verdict_table(rows: Sequence[Row]) -> list[str]:
    lines = [
        "## Verdicts by configuration and contract level",
        "",
        "Each cell is the runs with that verdict over the runs with that configuration and"
        " contract level.",
        "",
        _table_row(["Configuration", "Contract", *(str(verdict) for verdict in VERDICTS)]),
        _table_rule(2 + len(VERDICTS)),
    ]
    for (config, contract), members in _groups(
        rows, lambda row: (configuration(row.result.run), row.result.run.contract)
    ):
        lines.append(_table_row([config, contract, *_verdict_counts(members)]))
    return [*lines, ""]


def _safety_table(rows: Sequence[Row]) -> list[str]:
    lines = [
        "## Safety and liveness",
        "",
        "Record 0004's headline numbers keep safety apart from liveness. A run is safe when"
        " properties 1, 2, 3 and 5 all hold, and live when property 4 holds. Each cell is"
        " the runs that are safe, or live, over the runs that evaluated those properties.",
        "",
        _table_row(["Configuration", "Contract", "safe", "live"]),
        _table_rule(4),
    ]
    for (config, contract), members in _groups(
        rows, lambda row: (configuration(row.result.run), row.result.run.contract)
    ):
        lines.append(
            _table_row([config, contract, _holding(members, SAFETY), _holding(members, LIVENESS)])
        )
    return [*lines, ""]


def _holding(rows: Sequence[Row], numbers: tuple[int, ...]) -> str:
    """How many of `rows` pass every property in `numbers`, over the rows that evaluated
    them all."""
    held = evaluated = 0
    for row in rows:
        verdicts = {prop.property: prop.verdict for prop in row.result.properties}
        if any(number not in verdicts for number in numbers):
            continue
        evaluated += 1
        held += all(verdicts[number] is PropertyVerdict.PASS for number in numbers)
    return _fraction(held, evaluated) if evaluated else "not evaluated"


def _failure_table(rows: Sequence[Row]) -> list[str]:
    lines = [
        "## Failures by property and configuration",
        "",
        "Each cell is the runs that fail the property over the runs that evaluated it."
        " Properties 1, 2, 3 and 5 are safety properties, and property 4 is liveness"
        " (record 0004).",
        "",
        _table_row(
            ["Configuration", *(f"{number} {PROPERTY_NAMES[number]}" for number in PROPERTIES)]
        ),
        _table_rule(1 + len(PROPERTIES)),
    ]
    for (config,), members in _groups(rows, lambda row: (configuration(row.result.run),)):
        cells = []
        for number in PROPERTIES:
            failed, evaluated = _property_counts(members, number)
            cells.append(_fraction(failed, evaluated) if evaluated else "not evaluated")
        lines.append(_table_row([config, *cells]))
    return [*lines, ""]


def _fault_table(rows: Sequence[Row]) -> list[str]:
    lines = [
        "## Verdicts by fault plan",
        "",
        "Runs are grouped by the faults they planned: each fault's id, variant, point,"
        " match and occurrence, in plan order. Fault parameters are left out, since a run"
        " can draw some from its seed (F4's copy delay); the CSV lists them. Each count is"
        " over the runs in the row. Properties failed lists each property that a run of"
        " the row failed, with the runs that failed it over the runs that evaluated it.",
        "",
        _table_row(
            [
                "Faults planned",
                "Configuration",
                "Contract",
                *(str(verdict) for verdict in VERDICTS),
                "Properties failed",
            ]
        ),
        _table_rule(4 + len(VERDICTS)),
    ]
    for (faults, config, contract), members in _groups(
        rows,
        lambda row: (row.fault_group, configuration(row.result.run), row.result.run.contract),
    ):
        failed = []
        for number in PROPERTIES:
            failing, evaluated = _property_counts(members, number)
            if failing:
                failed.append(f"{number} ({_fraction(failing, evaluated)})")
        lines.append(
            _table_row(
                [faults, config, contract, *_verdict_counts(members), ", ".join(failed) or "none"]
            )
        )
    return [*lines, ""]


def _metric_table(rows: Sequence[Row]) -> list[str]:
    groups = _groups(rows, lambda row: (configuration(row.result.run),))
    configs = [config for (config,), _ in groups]
    lines = [
        "## Metric totals by configuration",
        "",
        "Each value is the sum of the metric over the configuration's runs (record 0004,"
        " Metrics). The first row gives the number of runs.",
        "",
        _table_row(["Metric", *configs]),
        _table_rule(1 + len(configs)),
        _table_row(["runs", *(str(len(members)) for _, members in groups)]),
    ]
    for name in METRICS:
        totals = [str(sum(row.result.metrics[name] for row in members)) for _, members in groups]
        lines.append(_table_row([name, *totals]))
    return [*lines, ""]


def _cost_table(rows: Sequence[Row], prices: PriceFile | None) -> list[str]:
    if prices is None:
        return [
            "## Cost",
            "",
            "Cost is not computed, because no price file was given. Give one with `--prices`,"
            " in the format of `prices/example.yaml`. The tokens each run used are in the"
            " metric totals and in the CSV.",
            "",
        ]
    lines = [
        "## Cost",
        "",
        f"Prices come from the price file dated {prices.price_date.isoformat()}, in USD per"
        " million input tokens and per million output tokens (record 0004). A run's cost is"
        " its input tokens at its model's input price plus its output tokens at its output"
        " price, exactly. The scripted model costs nothing. A reply that reported no usage"
        " counts no tokens; a call the spend cap refused was never sent, so it counts none"
        " either. A passing run is one whose verdict is pass or pass_unresolved: all five"
        " properties hold. Cost per passing run is the cost of all the row's runs, passing or"
        " not, over its passing runs. It is exact when the division ends, and otherwise"
        ' rounded to 6 significant digits and marked "about".',
        "",
        _table_row(
            [
                "Model",
                "Configuration",
                "Contract",
                "runs",
                "passing runs",
                "tokens in",
                "tokens out",
                "cost (USD)",
                "cost per passing run (USD)",
            ]
        ),
        _table_rule(9),
    ]
    groups = _groups(
        rows,
        lambda row: (row.result.run.model, configuration(row.result.run), row.result.run.contract),
    )
    for (model, config, contract), members in groups:
        total = _total_cost(members, f"the cost of {model} in {config} at {contract}")
        passing = sum(1 for row in members if row.result.verdict in PASSING)
        tokens_in, tokens_out = (
            sum(row.result.metrics[name] for row in members) for name in TOKEN_METRICS
        )
        lines.append(
            _table_row(
                [
                    model,
                    config,
                    contract,
                    str(len(members)),
                    str(passing),
                    str(tokens_in),
                    str(tokens_out),
                    usd(total),
                    cost_per_passing_run(total, passing),
                ]
            )
        )
    return [*lines, ""]


def _total_cost(rows: Sequence[Row], what: str) -> Decimal:
    """The sum of the rows' costs in USD, exactly."""
    costs = [row.cost for row in rows if row.cost is not None]
    return _exactly(lambda: sum(costs, Decimal(0)), what)


def _not_computed() -> list[str]:
    return [
        "## Not computed yet",
        "",
        f"Record 0004 also lists {NOT_COMPUTED}. The evaluator does not compute it yet: it"
        " waits for record 0014. No result holds a value for it, so this report shows none,"
        " not zeros.",
        "",
    ]


# Helpers ---------------------------------------------------------------------------------


def _groups[Key: tuple[str, ...]](
    rows: Sequence[Row], key: Callable[[Row], Key]
) -> list[tuple[Key, list[Row]]]:
    """The rows grouped by `key`, the groups in natural order of their keys, each group's
    rows in the fixed order."""
    groups: dict[Key, list[Row]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    ordered = sorted(groups, key=lambda found: tuple(_natural(part) for part in found))
    return [(found, groups[found]) for found in ordered]


def _verdict_counts(rows: Sequence[Row]) -> list[str]:
    counts = Counter(row.result.verdict for row in rows)
    return [_fraction(counts[verdict], len(rows)) for verdict in VERDICTS]


def _property_counts(rows: Sequence[Row], number: int) -> tuple[int, int]:
    """How many of `rows` fail property `number`, and how many evaluated it."""
    verdicts = [
        prop.verdict for row in rows for prop in row.result.properties if prop.property == number
    ]
    return sum(1 for verdict in verdicts if verdict is PropertyVerdict.FAIL), len(verdicts)


def _counted(values: Iterable[str]) -> str:
    """Each distinct value with its number of runs, in natural order."""
    counts = Counter(values)
    if not counts:
        return "none"
    return ", ".join(f"{value} ({_runs(counts[value])})" for value in sorted(counts, key=_natural))


def _fraction(count: int, total: int) -> str:
    return f"{count}/{total}"


def _runs(count: int) -> str:
    return f"{count} run" if count == 1 else f"{count} runs"


def _table_row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def _table_rule(columns: int) -> str:
    return "|" + " --- |" * columns


def _cell(text: str) -> str:
    """Text that cannot break a table row: pipes escaped, line breaks as spaces."""
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
