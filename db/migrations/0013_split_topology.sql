-- The split topology (decision record 0009): the simulator and the gateway run as separate
-- services, for people who bring their own agent, and each writes the harness's records of
-- the traffic it sees.
--
-- One counter numbers every harness event in order (0004_harness.sql). The inproc harness
-- keeps it in memory. In the split topology two processes write the records, so they draw
-- their numbers from this sequence. The default privileges in 0001_schemas.sql give
-- ff_harness USAGE, SELECT and UPDATE on it.
CREATE SEQUENCE harness.event_seq AS bigint MINVALUE 1;

-- The split run's clock origin: the host's monotonic milliseconds at virtual time 0 (0009:
-- "In the split topology, the virtual clock follows the host's monotonic clock"). The first
-- service of the run to start sets it, and every process of the run reads it, so on one host
-- they all read the same virtual time. One row, written once and never changed.
CREATE TABLE harness.clock_origin (
    origin_ms bigint NOT NULL,
    set_wall_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX clock_origin_is_one_row ON harness.clock_origin ((true));

CREATE TRIGGER clock_origin_is_append_only BEFORE UPDATE OR DELETE ON harness.clock_origin
    FOR EACH ROW EXECUTE FUNCTION harness.reject_change();

REVOKE UPDATE, DELETE ON harness.clock_origin FROM ff_harness;

-- A split run's agent is the user's own. The harness runs no model for it, so the run
-- records its model adapter as `external`, with the model the user names.
ALTER TABLE harness.run
    DROP CONSTRAINT run_model_adapter_check,
    ADD CONSTRAINT run_model_adapter_check CHECK (
        model_adapter IN ('scripted', 'live', 'replay', 'external')
    );
