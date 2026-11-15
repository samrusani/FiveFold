-- Copies of requests, and the answers they get (decision record 0006, fault F4).
--
-- F4 delivers a request twice. The second delivery is a message of its own, which the
-- transport made: its sender sent the request once. `copy_of` names the request it
-- copies, so a reader can tell a copy from a request a component sent. A copy carries
-- the request exactly as sent, on the same hop, at the same virtual time; the trigger
-- below holds every insert to that. The transport never delivers the answer to a copy:
-- it drops it with reason `answer_to_copy` (0006, transport rules). An MCP notification
-- passes the same fault point as a request, so it can be copied too; it has no answer.
--
-- `copy_of` is history like the rest of the message: set when the copy is logged, never
-- changed afterwards.

ALTER TABLE harness.message_log
    ADD COLUMN copy_of bigint REFERENCES harness.message_log (seq),
    ADD CONSTRAINT message_log_copy_check CHECK (
        copy_of IS NULL OR (kind <> 'answer' AND copy_of < seq)
    );

ALTER TABLE harness.message_log
    DROP CONSTRAINT message_log_drop_reason_check,
    ADD CONSTRAINT message_log_drop_reason_check CHECK (drop_reason IN (
        'fault', 'receiver_down', 'caller_timed_out', 'caller_crashed', 'answer_to_copy'
    ));

-- A copy repeats a request (or notification) that a component sent, field for field.
CREATE FUNCTION harness.check_copy() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    original harness.message_log%ROWTYPE;
BEGIN
    IF NEW.copy_of IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO original FROM harness.message_log WHERE seq = NEW.copy_of;
    IF NOT FOUND
       OR original.kind = 'answer'
       OR original.copy_of IS NOT NULL
       OR (NEW.hop, NEW.kind, NEW.sender, NEW.receiver, NEW.api, NEW.intent_id,
           NEW.idempotency_key, NEW.body, NEW.sent_vt)
          IS DISTINCT FROM
          (original.hop, original.kind, original.sender, original.receiver, original.api,
           original.intent_id, original.idempotency_key, original.body, original.sent_vt)
    THEN
        RAISE EXCEPTION 'message % is not a copy of request %', NEW.seq, NEW.copy_of;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER message_log_copies_repeat_a_request BEFORE INSERT ON harness.message_log
    FOR EACH ROW EXECUTE FUNCTION harness.check_copy();

-- The history rule of 0004_harness.sql, now covering `copy_of` too.
CREATE OR REPLACE FUNCTION harness.keep_message_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.fate IS NOT NULL
       OR (NEW.seq, NEW.hop, NEW.kind, NEW.sender, NEW.receiver, NEW.api, NEW.correlation,
           NEW.intent_id, NEW.idempotency_key, NEW.answer_class, NEW.body, NEW.sent_vt,
           NEW.copy_of)
          IS DISTINCT FROM
          (OLD.seq, OLD.hop, OLD.kind, OLD.sender, OLD.receiver, OLD.api, OLD.correlation,
           OLD.intent_id, OLD.idempotency_key, OLD.answer_class, OLD.body, OLD.sent_vt,
           OLD.copy_of)
    THEN
        RAISE EXCEPTION 'message % is history; only its fate can be set, once', OLD.seq;
    END IF;
    RETURN NEW;
END
$$;
