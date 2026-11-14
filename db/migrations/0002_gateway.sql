-- The gateway's action store (decision records 0001, 0002, 0003).
--
-- The step functions in src/fivefold/gateway/steps.py decide every change, and the store
-- in src/fivefold/gateway/pg_store.py writes it in one transaction. This schema holds the
-- records and enforces, on its own, the invariants a database can enforce:
--
--   I1  a trigger rejects any change to an effect id once it is set, and an effect id
--       exists exactly when the action is Confirmed;
--   I2  one grant per action and one per approval; a grant never moves to another action
--       or approval; every attempt uses its action's grant;
--   I4  every attempt carries its action's idempotency key;
--   I6  action_event is append-only: ff_gw has no UPDATE, DELETE or TRUNCATE privilege on
--       it, and triggers reject all three for every role;
--   I7  a partial unique index allows one live or Confirmed action per intent, for rows
--       with identity on (G1's rows have it off and are exempt); identity never changes.
--
-- What identifies an action, a grant or an attempt never changes after it is written.
--
-- The default privileges in 0001_schemas.sql give ff_gw SELECT, INSERT, UPDATE and DELETE
-- on every table here, and ff_eval and ff_viewer SELECT.

-- One row per run database: whether the run is closed (0003), and the counters that give
-- actions, events and tool calls their gap-free numbers. A transaction takes this row's
-- lock last: after the intent's row and the action rows it changes.
CREATE TABLE gw.run_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    run_closed boolean NOT NULL DEFAULT false,
    last_action_no bigint NOT NULL DEFAULT 0,
    last_seq bigint NOT NULL DEFAULT 0,
    last_call_no bigint NOT NULL DEFAULT 0
);
INSERT INTO gw.run_state DEFAULT VALUES;

-- One row per task intent, created at run start; the lock anchor for the matching rule
-- (0001, item 10).
CREATE TABLE gw.intent (
    intent_id text PRIMARY KEY,
    operation text NOT NULL
);

CREATE TABLE gw.action (
    action_id text PRIMARY KEY,
    task_id text NOT NULL,
    intent_id text NOT NULL REFERENCES gw.intent (intent_id),
    operation text NOT NULL,
    target text NOT NULL,
    args jsonb NOT NULL,
    action_key text NOT NULL,
    identity boolean NOT NULL,
    contract text NOT NULL CHECK (contract IN ('C0', 'C1', 'C2', 'C3')),
    basis jsonb NOT NULL,
    state text NOT NULL CHECK (state IN (
        'proposed', 'pending_approval', 'authorized', 'dispatched', 'outcome_unknown',
        'confirmed', 'rejected', 'failed'
    )),
    grant_id text UNIQUE,
    idempotency_key text NOT NULL UNIQUE,
    effect_id text,
    created_vt bigint NOT NULL,
    updated_vt bigint NOT NULL,
    UNIQUE (action_id, idempotency_key),
    UNIQUE (action_id, grant_id),
    CONSTRAINT action_effect_id_only_when_confirmed
        CHECK ((state = 'confirmed') = (effect_id IS NOT NULL))
);

CREATE UNIQUE INDEX action_one_holder_per_intent ON gw.action (intent_id)
    WHERE identity AND state IN (
        'proposed', 'pending_approval', 'authorized', 'dispatched', 'outcome_unknown',
        'confirmed'
    );

CREATE FUNCTION gw.keep_effect_id() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.effect_id IS NOT NULL AND NEW.effect_id IS DISTINCT FROM OLD.effect_id THEN
        RAISE EXCEPTION 'I1: the effect id of % is set and cannot change', OLD.action_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER action_keeps_effect_id BEFORE UPDATE OF effect_id ON gw.action
    FOR EACH ROW EXECUTE FUNCTION gw.keep_effect_id();

-- Only basis (before the first send, 0001 item 16), state, grant_id, effect_id and
-- updated_vt may change.
CREATE FUNCTION gw.keep_action_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.action_id, NEW.task_id, NEW.intent_id, NEW.operation, NEW.target, NEW.args,
        NEW.action_key, NEW.identity, NEW.contract, NEW.idempotency_key, NEW.created_vt)
       IS DISTINCT FROM
       (OLD.action_id, OLD.task_id, OLD.intent_id, OLD.operation, OLD.target, OLD.args,
        OLD.action_key, OLD.identity, OLD.contract, OLD.idempotency_key, OLD.created_vt)
    THEN
        RAISE EXCEPTION 'what identifies action % cannot change', OLD.action_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER action_keeps_identity BEFORE UPDATE ON gw.action
    FOR EACH ROW EXECUTE FUNCTION gw.keep_action_identity();

-- "grant" is a reserved word in SQL, so the table name is always quoted.
CREATE TABLE gw."grant" (
    grant_id text PRIMARY KEY,
    action_id text NOT NULL UNIQUE REFERENCES gw.action (action_id),
    approval_id text NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN (
        'pending', 'active', 'consumed', 'denied', 'timed_out', 'released', 'revoked',
        'expired'
    )),
    requested_vt bigint NOT NULL,
    consumed_vt bigint,
    UNIQUE (action_id, grant_id)
);

-- Only state and consumed_vt may change: a grant never moves (I2).
CREATE FUNCTION gw.keep_grant_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.grant_id, NEW.action_id, NEW.approval_id, NEW.requested_vt)
       IS DISTINCT FROM (OLD.grant_id, OLD.action_id, OLD.approval_id, OLD.requested_vt)
    THEN
        RAISE EXCEPTION 'I2: grant % cannot move to another action or approval', OLD.grant_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER grant_keeps_binding BEFORE UPDATE ON gw."grant"
    FOR EACH ROW EXECUTE FUNCTION gw.keep_grant_binding();

-- An action's grant must be a grant of that action. Deferred, because the action row and
-- its grant are written in the same transaction.
ALTER TABLE gw.action ADD CONSTRAINT action_grant_is_its_own
    FOREIGN KEY (action_id, grant_id) REFERENCES gw."grant" (action_id, grant_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE gw.attempt (
    attempt_id text PRIMARY KEY,
    action_id text NOT NULL REFERENCES gw.action (action_id),
    attempt_no integer NOT NULL CHECK (attempt_no >= 1),
    idempotency_key text NOT NULL,
    grant_id text NOT NULL,
    check_kind text NOT NULL CHECK (check_kind IN ('full', 'exact_only', 'exempt')),
    check_approval text,
    check_basis jsonb,
    check_seen_seq bigint NOT NULL,
    check_vt bigint NOT NULL,
    sent_vt bigint NOT NULL,
    answer_class text CHECK (answer_class IN ('committed', 'rejected', 'in_progress', 'unknown')),
    answer_ref text,
    answered_vt bigint,
    UNIQUE (action_id, attempt_no),
    FOREIGN KEY (action_id, grant_id) REFERENCES gw."grant" (action_id, grant_id),
    FOREIGN KEY (action_id, grant_id) REFERENCES gw.action (action_id, grant_id),
    FOREIGN KEY (action_id, idempotency_key) REFERENCES gw.action (action_id, idempotency_key)
);

-- Only the answer may change: the check and the send are history (I3).
CREATE FUNCTION gw.keep_attempt_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.attempt_id, NEW.action_id, NEW.attempt_no, NEW.idempotency_key, NEW.grant_id,
        NEW.check_kind, NEW.check_approval, NEW.check_basis, NEW.check_seen_seq,
        NEW.check_vt, NEW.sent_vt)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.action_id, OLD.attempt_no, OLD.idempotency_key, OLD.grant_id,
        OLD.check_kind, OLD.check_approval, OLD.check_basis, OLD.check_seen_seq,
        OLD.check_vt, OLD.sent_vt)
    THEN
        RAISE EXCEPTION 'attempt % is history; only its answer can be recorded', OLD.attempt_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER attempt_keeps_history BEFORE UPDATE ON gw.attempt
    FOR EACH ROW EXECUTE FUNCTION gw.keep_attempt_history();

CREATE TABLE gw.action_event (
    seq bigint PRIMARY KEY CHECK (seq >= 1),
    action_id text NOT NULL REFERENCES gw.action (action_id),
    kind text NOT NULL CHECK (kind IN ('transition', 'note')),
    from_state text,
    to_state text NOT NULL,
    reason text NOT NULL,
    details jsonb NOT NULL,
    vt bigint NOT NULL,
    wall_at timestamptz NOT NULL DEFAULT now()
);

REVOKE UPDATE, DELETE ON gw.action_event FROM ff_gw;

CREATE FUNCTION gw.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'I6: % is append-only; % refused', TG_TABLE_NAME, TG_OP;
END
$$;

CREATE TRIGGER action_event_is_append_only BEFORE UPDATE OR DELETE ON gw.action_event
    FOR EACH ROW EXECUTE FUNCTION gw.reject_change();

CREATE TRIGGER action_event_is_never_truncated BEFORE TRUNCATE ON gw.action_event
    FOR EACH STATEMENT EXECUTE FUNCTION gw.reject_change();

CREATE TABLE gw.escalation (
    escalation_id text PRIMARY KEY,
    action_id text NOT NULL UNIQUE REFERENCES gw.action (action_id),
    opened_vt bigint NOT NULL,
    resolved_vt bigint,
    resolution text CHECK (resolution IN ('committed', 'not_committed')),
    effect_id text,
    actor text
);

-- Every agent-facing response, for I5. `as_of_seq` is the event seq it reflects.
CREATE TABLE gw.tool_call (
    call_no bigint PRIMARY KEY CHECK (call_no >= 1),
    tool text NOT NULL,
    action_id text REFERENCES gw.action (action_id),
    result jsonb NOT NULL,
    as_of_seq bigint NOT NULL,
    vt bigint NOT NULL
);
