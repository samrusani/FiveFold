-- Reads and write calls (decision record 0001, items 13 to 17).
--
-- gw.read holds every read the gateway served, with the fact's value, so a later check
-- can compare the cited version's fields with the current ones (0004, dependency
-- rules); a proposal cites reads by their ids.
-- gw.proposal holds every write call with its outcome, admitted or refused. The gateway
-- only ever adds rows to either table: ff_gw has no UPDATE or DELETE privilege on them,
-- and triggers reject both. The run's state row numbers both, so ids have no gaps.

ALTER TABLE gw.run_state
    ADD COLUMN last_read_no bigint NOT NULL DEFAULT 0,
    ADD COLUMN last_proposal_no bigint NOT NULL DEFAULT 0;

-- A closed run stays closed, and its counters never go back.
CREATE OR REPLACE FUNCTION gw.keep_run_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.run_closed AND NOT NEW.run_closed)
       OR NEW.last_action_no < OLD.last_action_no
       OR NEW.last_seq < OLD.last_seq
       OR NEW.last_call_no < OLD.last_call_no
       OR NEW.last_read_no < OLD.last_read_no
       OR NEW.last_proposal_no < OLD.last_proposal_no
    THEN
        RAISE EXCEPTION 'the run cannot reopen and its counters cannot go back';
    END IF;
    RETURN NEW;
END
$$;

CREATE TABLE gw.read (
    read_id text PRIMARY KEY CHECK (read_id ~ '^R-[0-9]{4,}$'),
    key text NOT NULL,
    version bigint NOT NULL,
    data jsonb NOT NULL CHECK (jsonb_typeof(data) = 'object'),
    vt bigint NOT NULL
);

CREATE INDEX read_by_fact ON gw.read (key, version);

-- `args` holds the business arguments in normal form, or, for a call refused with
-- invalid_args, the arguments as the agent sent them. `action_id` names the action the
-- call created or attached to, or the one an intent_busy or intent_done refusal names.
CREATE TABLE gw.proposal (
    proposal_id text PRIMARY KEY CHECK (proposal_id ~ '^P-[0-9]{4,}$'),
    operation text NOT NULL,
    intent_id text,
    args jsonb NOT NULL CHECK (jsonb_typeof(args) = 'object'),
    based_on jsonb NOT NULL CHECK (jsonb_typeof(based_on) = 'array'),
    admission text NOT NULL CHECK (admission IN ('admitted', 'attached', 'refused')),
    reason text CHECK (reason IN (
        'invalid_args', 'unknown_intent', 'wrong_operation', 'unknown_read', 'missing_basis',
        'intent_busy', 'intent_done', 'run_closed'
    )),
    action_id text REFERENCES gw.action (action_id),
    vt bigint NOT NULL,
    CHECK ((admission = 'refused') = (reason IS NOT NULL)),
    CHECK (admission = 'refused' OR action_id IS NOT NULL)
);

REVOKE UPDATE, DELETE ON gw.read, gw.proposal FROM ff_gw;

CREATE TRIGGER read_is_append_only BEFORE UPDATE OR DELETE ON gw.read
    FOR EACH ROW EXECUTE FUNCTION gw.reject_row_change();

CREATE TRIGGER proposal_is_append_only BEFORE UPDATE OR DELETE ON gw.proposal
    FOR EACH ROW EXECUTE FUNCTION gw.reject_row_change();
