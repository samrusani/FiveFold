-- The evaluator's results (decision records 0004 and 0008).
--
-- The evaluator connects as ff_eval and is the only writer here; ff_viewer reads. The
-- default privileges in 0001_schemas.sql give ff_eval SELECT, INSERT, UPDATE and DELETE
-- on every table here, and ff_viewer SELECT. No other role has any privilege.
--
-- Results are derived data: scoring a run again replaces its rows, in one transaction.

-- One row per run: the verdict over the properties evaluated, the metrics, and the whole
-- result as the evaluator's JSON (src/fivefold/evaluator/result.py).
CREATE TABLE eval.run_result (
    run_id text PRIMARY KEY,
    verdict text NOT NULL CHECK (verdict IN ('pass', 'pass_unresolved', 'fail')),
    evaluated smallint[] NOT NULL CHECK (
        cardinality(evaluated) BETWEEN 1 AND 5 AND evaluated <@ '{1, 2, 3, 4, 5}'
    ),
    metrics jsonb NOT NULL CHECK (jsonb_typeof(metrics) = 'object'),
    result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object')
);

-- One row per run and property (spec, Data model: eval.Result). A property that fails
-- names at least one reason code, and one that passes or is not yet evaluated names none.
CREATE TABLE eval.result (
    run_id text NOT NULL REFERENCES eval.run_result (run_id) ON DELETE CASCADE,
    property smallint NOT NULL CHECK (property BETWEEN 1 AND 5),
    name text NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('pass', 'fail', 'not_evaluated')),
    reason_codes text[] NOT NULL,
    metrics jsonb NOT NULL CHECK (jsonb_typeof(metrics) = 'object'),
    failures jsonb NOT NULL CHECK (jsonb_typeof(failures) = 'array'),
    PRIMARY KEY (run_id, property),
    CHECK ((verdict = 'fail') = (cardinality(reason_codes) > 0))
);
