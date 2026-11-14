-- The harness's records of a run (decision records 0004, 0006 and 0009).
--
-- The evaluator scores from these records and the simulator's (0004). One counter, kept by
-- the harness, numbers every harness event in order: a message's send, its delivery, a
-- fault firing, an action of a scripted human and a submitted report. A message's `seq`
-- is the number of its send, and `delivered_seq` the number of its delivery, so "this
-- message was delivered before that report" compares two numbers.
--
-- The records are history. A message's fate is set once; faults, actor actions and
-- reports are never changed. ff_harness has no DELETE on any of these tables and no
-- UPDATE except on the message log, and triggers hold every role to the same rules.
--
-- The default privileges in 0001_schemas.sql give ff_harness SELECT, INSERT, UPDATE and
-- DELETE on every table here, and ff_eval and ff_viewer SELECT.

-- One row per run database: what the run is (0009).
CREATE TABLE harness.run (
    run_id text PRIMARY KEY,
    scenario_id text NOT NULL,
    scenario_digest text NOT NULL,
    config text NOT NULL CHECK (config IN ('B0', 'G1', 'G2')),
    contract text NOT NULL CHECK (contract IN ('C0', 'C1', 'C2', 'C3')),
    seed bigint NOT NULL,
    topology text NOT NULL CHECK (topology IN ('inproc', 'split')),
    model_adapter text NOT NULL CHECK (model_adapter IN ('scripted', 'live', 'replay')),
    model text NOT NULL,
    cassette_digest text,
    parent_run_id text,
    branch_point bigint,
    params jsonb NOT NULL,
    started_wall_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((parent_run_id IS NULL) = (branch_point IS NULL))
);

CREATE UNIQUE INDEX run_is_one_row ON harness.run ((true));

-- Every message on every hop and its fate (0004, 0006).
CREATE TABLE harness.message_log (
    seq bigint PRIMARY KEY CHECK (seq >= 1),
    hop text NOT NULL CHECK (hop IN ('agent_gw', 'gw_sim', 'agent_sim')),
    kind text NOT NULL CHECK (kind IN ('request', 'answer', 'notification')),
    sender text NOT NULL,
    receiver text NOT NULL,
    api text,
    correlation bigint REFERENCES harness.message_log (seq),
    intent_id text,
    idempotency_key text,
    answer_class text,
    body jsonb NOT NULL,
    sent_vt bigint NOT NULL CHECK (sent_vt >= 0),
    fate text CHECK (fate IN ('delivered', 'dropped')),
    drop_reason text CHECK (drop_reason IN (
        'fault', 'receiver_down', 'caller_timed_out', 'caller_crashed'
    )),
    delivered_seq bigint UNIQUE CHECK (delivered_seq > seq),
    delivered_vt bigint CHECK (delivered_vt >= sent_vt),
    wall_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind = 'answer') = (correlation IS NOT NULL)),
    CHECK ((fate = 'delivered') = (delivered_seq IS NOT NULL AND delivered_vt IS NOT NULL)),
    CHECK ((fate = 'dropped') = (drop_reason IS NOT NULL))
);

-- A message is written once when sent; its fate is set once, later.
CREATE FUNCTION harness.keep_message_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.fate IS NOT NULL
       OR (NEW.seq, NEW.hop, NEW.kind, NEW.sender, NEW.receiver, NEW.api, NEW.correlation,
           NEW.intent_id, NEW.idempotency_key, NEW.answer_class, NEW.body, NEW.sent_vt)
          IS DISTINCT FROM
          (OLD.seq, OLD.hop, OLD.kind, OLD.sender, OLD.receiver, OLD.api, OLD.correlation,
           OLD.intent_id, OLD.idempotency_key, OLD.answer_class, OLD.body, OLD.sent_vt)
    THEN
        RAISE EXCEPTION 'message % is history; only its fate can be set, once', OLD.seq;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER message_log_keeps_history BEFORE UPDATE ON harness.message_log
    FOR EACH ROW EXECUTE FUNCTION harness.keep_message_history();

-- Every fault fired, with the deliveries it voided (0006).
CREATE TABLE harness.fault_log (
    seq bigint PRIMARY KEY CHECK (seq >= 1),
    fault text NOT NULL CHECK (fault ~ '^F([1-9]|1[0-2])$'),
    variant text,
    point text NOT NULL,
    occurrence integer NOT NULL CHECK (occurrence >= 1),
    action text NOT NULL,
    target_seq bigint REFERENCES harness.message_log (seq),
    component text,
    voided bigint[] NOT NULL DEFAULT '{}',
    params jsonb NOT NULL,
    vt bigint NOT NULL CHECK (vt >= 0),
    wall_at timestamptz NOT NULL DEFAULT now()
);

-- Actions of the scripted approver and resolver (0002, 0009).
CREATE TABLE harness.actor_log (
    seq bigint PRIMARY KEY CHECK (seq >= 1),
    actor text NOT NULL CHECK (actor IN ('approver', 'resolver')),
    action text NOT NULL,
    subject text NOT NULL,
    details jsonb NOT NULL,
    vt bigint NOT NULL CHECK (vt >= 0),
    wall_at timestamptz NOT NULL DEFAULT now()
);

-- Every final report the stack submitted, with its validation (0004, 0008). `message_seq`
-- is the message that carried the report to the component that accepted it: the gateway
-- in G modes, the report sink in baselines.
CREATE TABLE harness.final_report (
    seq bigint PRIMARY KEY CHECK (seq >= 1),
    worker text NOT NULL,
    report jsonb NOT NULL,
    valid boolean NOT NULL,
    errors jsonb NOT NULL,
    accepted_by text NOT NULL CHECK (accepted_by IN ('gateway', 'sink')),
    message_seq bigint REFERENCES harness.message_log (seq),
    vt bigint NOT NULL CHECK (vt >= 0),
    wall_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION harness.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '%.% is history; % refused', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP;
END
$$;

CREATE TRIGGER message_log_is_never_deleted BEFORE DELETE ON harness.message_log
    FOR EACH ROW EXECUTE FUNCTION harness.reject_change();
CREATE TRIGGER fault_log_is_append_only BEFORE UPDATE OR DELETE ON harness.fault_log
    FOR EACH ROW EXECUTE FUNCTION harness.reject_change();
CREATE TRIGGER actor_log_is_append_only BEFORE UPDATE OR DELETE ON harness.actor_log
    FOR EACH ROW EXECUTE FUNCTION harness.reject_change();
CREATE TRIGGER final_report_is_append_only BEFORE UPDATE OR DELETE ON harness.final_report
    FOR EACH ROW EXECUTE FUNCTION harness.reject_change();

REVOKE DELETE ON harness.message_log FROM ff_harness;
REVOKE UPDATE, DELETE ON harness.fault_log, harness.actor_log, harness.final_report
    FROM ff_harness;
