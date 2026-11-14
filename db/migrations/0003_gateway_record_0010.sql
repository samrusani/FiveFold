-- Record 0010's changes to the gateway's records.
--
-- 1. A send's check keeps the approval's expiry. An approval that expires before a send
--    could commit counts as expired (0010, change 4; 0002, rule 17), so the check that
--    allowed a send must show when its approval expires. The expiry is kept exactly
--    when the check found the approval approved.
-- 2. An accepted `not_done` claim closes its intent, and the run closes in the
--    transaction that accepts the last report it expects (0010, item 6). `closed` marks
--    a closed intent; `gw.report` holds each agent worker's first accepted report.

ALTER TABLE gw.attempt ADD COLUMN check_approval_expires_at bigint;

ALTER TABLE gw.attempt ADD CONSTRAINT attempt_expiry_exactly_when_approved
    CHECK ((check_approval IS NOT DISTINCT FROM 'approved')
        = (check_approval_expires_at IS NOT NULL));

-- The expiry is part of the check, which is history (I3): only the answer may change.
CREATE OR REPLACE FUNCTION gw.keep_attempt_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.attempt_id, NEW.action_id, NEW.attempt_no, NEW.idempotency_key, NEW.grant_id,
        NEW.check_kind, NEW.check_approval, NEW.check_basis, NEW.check_seen_seq,
        NEW.check_vt, NEW.check_approval_expires_at, NEW.sent_vt)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.action_id, OLD.attempt_no, OLD.idempotency_key, OLD.grant_id,
        OLD.check_kind, OLD.check_approval, OLD.check_basis, OLD.check_seen_seq,
        OLD.check_vt, OLD.check_approval_expires_at, OLD.sent_vt)
    THEN
        RAISE EXCEPTION 'attempt % is history; only its answer can be recorded', OLD.attempt_id;
    END IF;
    RETURN NEW;
END
$$;

ALTER TABLE gw.intent ADD COLUMN closed boolean NOT NULL DEFAULT false;

-- An intent's id and operation never change, and a closed intent never reopens. An
-- intent is never deleted either, or deleting and re-adding it would reopen it.
CREATE FUNCTION gw.keep_intent() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.intent_id, NEW.operation) IS DISTINCT FROM (OLD.intent_id, OLD.operation)
       OR (OLD.closed AND NOT NEW.closed)
    THEN
        RAISE EXCEPTION 'intent % cannot change or reopen', OLD.intent_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER intent_keeps_identity BEFORE UPDATE ON gw.intent
    FOR EACH ROW EXECUTE FUNCTION gw.keep_intent();

CREATE FUNCTION gw.reject_row_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'gw.% rows are history; % refused', TG_TABLE_NAME, TG_OP;
END
$$;

CREATE TRIGGER intent_is_never_deleted BEFORE DELETE ON gw.intent
    FOR EACH ROW EXECUTE FUNCTION gw.reject_row_change();

-- A closed run stays closed, and its counters never go back.
CREATE FUNCTION gw.keep_run_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.run_closed AND NOT NEW.run_closed)
       OR NEW.last_action_no < OLD.last_action_no
       OR NEW.last_seq < OLD.last_seq
       OR NEW.last_call_no < OLD.last_call_no
    THEN
        RAISE EXCEPTION 'the run cannot reopen and its counters cannot go back';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER run_state_moves_forward BEFORE UPDATE ON gw.run_state
    FOR EACH ROW EXECUTE FUNCTION gw.keep_run_state();

CREATE TRIGGER run_state_is_never_deleted BEFORE DELETE ON gw.run_state
    FOR EACH ROW EXECUTE FUNCTION gw.reject_row_change();

-- One row per agent worker whose final report was accepted, naming the response that
-- first accepted it. The gateway only ever adds rows here.
CREATE TABLE gw.report (
    worker text PRIMARY KEY,
    call_no bigint NOT NULL UNIQUE REFERENCES gw.tool_call (call_no)
);

REVOKE UPDATE, DELETE ON gw.report FROM ff_gw;

CREATE TRIGGER report_is_append_only BEFORE UPDATE OR DELETE ON gw.report
    FOR EACH ROW EXECUTE FUNCTION gw.reject_row_change();
