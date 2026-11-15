-- The harness's log of model calls (decision records 0004 and 0009).
--
-- One row per model call of an agent worker, written once the call is answered: the
-- worker, the call's number in the run, the virtual times it was asked and answered, the
-- run's model, and the reply's input and output tokens. Record 0004's token and cost
-- metrics come from these rows. A reply that reports no usage, as every scripted reply
-- does, has no tokens. A call the live adapter refused at the spend cap names the reason
-- in `refused`, and has no tokens either: nothing was sent.
--
-- `call_no` counts the run's calls from 1 in the order they were answered, which is the
-- order of the run's cassette (0009). It is not a harness event number: model calls take
-- none of the counter that orders messages, faults, actor actions and reports. `wall_ms`,
-- the call's wall-clock duration, and `wall_at` are for metrics only (0009).
--
-- The rows are history: a trigger refuses every change, and ff_harness can only insert
-- and select. The default privileges in 0001_schemas.sql give ff_eval and ff_viewer
-- SELECT, so the evaluator can read them.

CREATE TABLE harness.model_call (
    call_no bigint PRIMARY KEY CHECK (call_no >= 1),
    worker text NOT NULL,
    asked_vt bigint NOT NULL CHECK (asked_vt >= 0),
    answered_vt bigint NOT NULL CHECK (answered_vt >= asked_vt),
    model text NOT NULL,
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0),
    refused text CHECK (refused IN ('spend_cap')),
    wall_ms double precision NOT NULL CHECK (wall_ms >= 0),
    wall_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((input_tokens IS NULL) = (output_tokens IS NULL)),
    CHECK (refused IS NULL OR input_tokens IS NULL)
);

CREATE TRIGGER model_call_is_append_only BEFORE UPDATE OR DELETE ON harness.model_call
    FOR EACH ROW EXECUTE FUNCTION harness.reject_change();

REVOKE UPDATE, DELETE ON harness.model_call FROM ff_harness;
