-- When the harness stopped the run (decision records 0004 and 0009).
--
-- The run stops at the end of the drain window. The evaluator judges "at the end" at this
-- time: for example, whether an approval is still valid and unused (0004, property 4).
-- Time passes in a quiet drain window without any record, so the latest record is not
-- the end. One row, written once when the run stops, and never changed.
CREATE TABLE harness.run_stop (
    run_id text PRIMARY KEY REFERENCES harness.run (run_id),
    stopped_vt bigint NOT NULL CHECK (stopped_vt >= 0),
    stopped_wall_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX run_stops_once ON harness.run_stop ((true));

CREATE TRIGGER run_stop_is_append_only BEFORE UPDATE OR DELETE ON harness.run_stop
    FOR EACH ROW EXECUTE FUNCTION harness.reject_change();

REVOKE UPDATE, DELETE ON harness.run_stop FROM ff_harness;
