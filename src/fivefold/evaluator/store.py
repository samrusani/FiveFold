"""Write a run's result to the `eval` schema, and score a run end to end (0004, 0008).

`eval.run_result` holds one row per run: the verdict, the properties evaluated, the
metrics and the whole result. `eval.result` holds one row per run and property: its
verdict, reason codes, metrics and failures (spec, Data model). Writing a run's result
again replaces the old rows, in one transaction.

`evaluate_run` connects as `ff_eval`, the only role that writes `eval` and reads the
other components' records (0008), reads the run, scores it and writes the rows.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from fivefold.domain.scenario import Scenario
from fivefold.evaluator.evaluate import evaluate
from fivefold.evaluator.load import load_run
from fivefold.evaluator.result import RunResult

Connection = psycopg.Connection[TupleRow]

EVALUATOR_ROLE = "ff_eval"
"""The database role the evaluator connects as (0008)."""


class WrongRole(Exception):
    """The connection's role is not `ff_eval`."""


def write_result(conn: Connection, result: RunResult) -> None:
    """Replace the run's rows in `eval.run_result` and `eval.result` with `result`.

    `conn` must be in autocommit mode: the writer opens its own transaction.
    """
    if not conn.autocommit:
        raise ValueError("write_result opens its own transaction: use an autocommit connection")
    run_id = result.run.run_id
    with conn.transaction():
        conn.execute("DELETE FROM eval.run_result WHERE run_id = %s", (run_id,))
        conn.execute(
            "INSERT INTO eval.run_result (run_id, verdict, evaluated, metrics, result)"
            " VALUES (%s, %s, %s, %s, %s)",
            (
                run_id,
                str(result.verdict),
                list(result.evaluated),
                Jsonb(result.metrics),
                Jsonb(result.model_dump(mode="json")),
            ),
        )
        for prop in result.properties:
            conn.execute(
                "INSERT INTO eval.result (run_id, property, name, verdict, reason_codes,"
                " metrics, failures) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    prop.property,
                    prop.name,
                    str(prop.verdict),
                    [str(code) for code in prop.reason_codes],
                    Jsonb(prop.metrics),
                    Jsonb([failure.model_dump(mode="json") for failure in prop.failures]),
                ),
            )


def evaluate_run(conninfo: str, scenario: Scenario, *, write: bool = True) -> RunResult:
    """Score the run in the database `conninfo` names, connecting as `ff_eval`.

    Writes the result's rows unless `write` is false, and returns the result. Raises
    `WrongRole` for any other role, and `RecordsError` for records it cannot score.
    """
    with psycopg.connect(conninfo, autocommit=True) as conn:
        row = conn.execute("SELECT current_user").fetchone()
        if row is None or row[0] != EVALUATOR_ROLE:
            raise WrongRole(f"the evaluator connects as {EVALUATOR_ROLE}, not {row}")
        result = evaluate(load_run(conn, scenario))
        if write:
            write_result(conn, result)
    return result
