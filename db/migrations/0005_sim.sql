-- The simulator's ground truth (decision records 0002, 0004, 0005, 0009).
--
-- The simulator connects as ff_sim and is the only writer here; ff_harness, ff_eval and
-- ff_viewer read; ff_gw and ff_agent have no access (0008). The service in
-- src/fivefold/sim/ decides every change and writes it in one transaction. This schema
-- also enforces, on its own, the rules a database can enforce:
--
--   - One gap-free sequence orders every simulator event. Each event takes the next
--     number from the run's counter row, and virtual time never goes back.
--   - Ground truth is never deleted: no role can DELETE or TRUNCATE a sim table, and the
--     logs (events, fact versions, reads, effects, approval events) are append-only.
--   - Fact versions run 1, 2, 3 per key, each from a later event. A version caused by an
--     effect is exactly that effect's consequence, and every effect has its consequence.
--   - An effect repeats the write it commits, commits by the write's deadline unless a
--     fault waived it, and stores the idempotency key exactly when the destination
--     honours keys. A key the destination honours commits at most once and is in flight
--     at most once.
--   - At C3, an effect cites an approval that is approved, unrevoked, unexpired and not
--     yet backing another effect. Scope and basis are checked by the service only.
--   - Writes and approvals only move forward, and what identifies them never changes.
--
-- The default privileges in 0001_schemas.sql give ff_sim SELECT, INSERT, UPDATE and
-- DELETE on every table here, and ff_harness, ff_eval and ff_viewer SELECT, as record
-- 0008's table says. Triggers, not privileges, keep ground truth from being deleted or
-- rewritten, and they apply to every role.

-- Helpers ---------------------------------------------------------------------------------

-- Whether a jsonb value is an integer.
CREATE FUNCTION sim.is_integer(value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(jsonb_typeof(value) = 'number' AND (value #>> '{}') ~ '^-?[0-9]+$', false)
$$;

-- Whether a fact value has the fields of its key's kind (src/fivefold/domain/rules.py).
CREATE FUNCTION sim.fact_value_ok(key text, value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(jsonb_typeof(value) = 'object' AND CASE split_part(key, '/', 1)
        WHEN 'quote' THEN
            sim.is_integer(value -> 'unit_price_minor')
            AND jsonb_typeof(value -> 'currency') = 'string'
            AND sim.is_integer(value -> 'min_quantity')
            AND sim.is_integer(value -> 'lead_time_days')
            AND jsonb_typeof(value -> 'status') = 'string'
        WHEN 'budget' THEN
            sim.is_integer(value -> 'limit_minor') AND sim.is_integer(value -> 'committed_minor')
        WHEN 'inventory' THEN
            sim.is_integer(value -> 'available')
        WHEN 'suppliers' THEN
            jsonb_typeof(value -> 'suppliers') = 'array'
        ELSE false
    END, false)
$$;

-- The fact a committed write changes (rules.consequence_key).
CREATE FUNCTION sim.consequence_key(operation text, args jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE operation
        WHEN 'submit_purchase_order' THEN 'budget/' || (args ->> 'cost_center')
        WHEN 'reserve_inventory' THEN
            'inventory/' || (args ->> 'warehouse_id') || '/' || (args ->> 'sku')
    END
$$;

-- The new value of that fact (rules.consequence): an order adds its amount to the
-- budget's committed amount; a reservation takes its quantity from available stock.
CREATE FUNCTION sim.consequence_value(operation text, args jsonb, current jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE operation
        WHEN 'submit_purchase_order' THEN current || jsonb_build_object('committed_minor',
            (current ->> 'committed_minor')::numeric
            + (args ->> 'quantity')::numeric * (args ->> 'unit_price_minor')::numeric)
        WHEN 'reserve_inventory' THEN current || jsonb_build_object('available',
            (current ->> 'available')::numeric - (args ->> 'quantity')::numeric)
    END
$$;

CREATE FUNCTION sim.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'sim.% is ground truth: % refused', TG_TABLE_NAME, TG_OP;
END
$$;

-- The run ---------------------------------------------------------------------------------

-- One row per run database: the destination's configuration, set once at setup, and the
-- counters that number events and ids. Every simulator transaction locks this row
-- first, so the simulator handles one request at a time (0004).
CREATE TABLE sim.run (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    scenario_id text NOT NULL,
    task_id text NOT NULL,
    capabilities text[] NOT NULL
        CHECK (capabilities <@ ARRAY['status', 'idempotency_key', 'conditional_write']),
    max_hop_latency_ms bigint NOT NULL CHECK (max_hop_latency_ms > 0),
    commit_deadline_ms bigint NOT NULL CHECK (commit_deadline_ms > 0),
    status_lag_ms bigint NOT NULL CHECK (status_lag_ms >= 0),
    credentials text[] NOT NULL CHECK (cardinality(credentials) >= 1),
    authority jsonb NOT NULL CHECK (sim.is_integer(authority -> 'approval_ttl_ms')),
    last_seq bigint NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    last_vt bigint NOT NULL DEFAULT 0 CHECK (last_vt >= 0),
    last_po_no bigint NOT NULL DEFAULT 0 CHECK (last_po_no >= 0),
    last_rs_no bigint NOT NULL DEFAULT 0 CHECK (last_rs_no >= 0),
    last_apr_no bigint NOT NULL DEFAULT 0 CHECK (last_apr_no >= 0)
);

-- The configuration never changes, counters never go back, and only a new event (whose
-- trigger runs one level down) moves the sequence and the clock.
CREATE FUNCTION sim.keep_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.singleton, NEW.scenario_id, NEW.task_id, NEW.capabilities,
        NEW.max_hop_latency_ms, NEW.commit_deadline_ms, NEW.status_lag_ms, NEW.credentials,
        NEW.authority)
       IS DISTINCT FROM
       (OLD.singleton, OLD.scenario_id, OLD.task_id, OLD.capabilities,
        OLD.max_hop_latency_ms, OLD.commit_deadline_ms, OLD.status_lag_ms, OLD.credentials,
        OLD.authority)
    THEN
        RAISE EXCEPTION 'the run''s configuration never changes';
    END IF;
    IF NEW.last_po_no < OLD.last_po_no OR NEW.last_rs_no < OLD.last_rs_no
       OR NEW.last_apr_no < OLD.last_apr_no
    THEN
        RAISE EXCEPTION 'the run''s id counters never go back';
    END IF;
    IF (NEW.last_seq, NEW.last_vt) IS DISTINCT FROM (OLD.last_seq, OLD.last_vt)
       AND pg_trigger_depth() < 2
    THEN
        RAISE EXCEPTION 'only a new event moves the sequence and the clock';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER run_keeps_its_configuration BEFORE UPDATE ON sim.run
    FOR EACH ROW EXECUTE FUNCTION sim.keep_run();

-- Events ----------------------------------------------------------------------------------

-- Every simulator event, in the order the gap-free sequence gives (0004). A row's seq is
-- assigned here: the run's last seq plus one. Other tables refer to these rows.
CREATE TABLE sim.event (
    seq bigint PRIMARY KEY CHECK (seq >= 1),
    vt bigint NOT NULL CHECK (vt >= 0),
    kind text NOT NULL CHECK (kind IN (
        'setup', 'read', 'fact_changed',
        'write_received', 'write_committed', 'write_replayed', 'write_in_progress',
        'write_rejected', 'write_aborted',
        'approval_requested', 'approval_repeated', 'approval_approved', 'approval_denied',
        'approval_revoked', 'approval_status', 'status'
    )),
    actor text NOT NULL,
    ref text,
    -- Wall-clock time, for metrics only; never used for decisions (0009).
    wall_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION sim.next_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    run sim.run;
BEGIN
    SELECT * INTO run FROM sim.run FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'the run is not set up';
    END IF;
    IF NEW.seq IS NOT NULL AND NEW.seq <> run.last_seq + 1 THEN
        RAISE EXCEPTION 'event % would leave a gap after %', NEW.seq, run.last_seq;
    END IF;
    IF NEW.vt < run.last_vt THEN
        RAISE EXCEPTION 'virtual time went back from % to %', run.last_vt, NEW.vt;
    END IF;
    NEW.seq := run.last_seq + 1;
    UPDATE sim.run SET last_seq = NEW.seq, last_vt = NEW.vt;
    RETURN NEW;
END
$$;

CREATE TRIGGER event_takes_the_next_seq BEFORE INSERT ON sim.event
    FOR EACH ROW EXECUTE FUNCTION sim.next_event();

-- Raise unless event `seq` exists with this kind and virtual time (and actor, if given).
CREATE FUNCTION sim.require_event(seq bigint, kinds text[], vt bigint, actor text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    hit sim.event;
BEGIN
    SELECT * INTO hit FROM sim.event WHERE event.seq = require_event.seq;
    IF NOT FOUND OR NOT hit.kind = ANY(require_event.kinds)
       OR hit.vt IS DISTINCT FROM require_event.vt
       OR (require_event.actor IS NOT NULL AND hit.actor IS DISTINCT FROM require_event.actor)
    THEN
        RAISE EXCEPTION 'event % is not a % event at % by %', require_event.seq,
            require_event.kinds, require_event.vt, require_event.actor;
    END IF;
END
$$;

-- Facts -----------------------------------------------------------------------------------

-- The full history of quotes, budgets, stock and the supplier directory (0004). Every
-- fact starts at version 1 from the scenario; every change appends a version with its
-- cause: a committed effect, or a change through the control interface (faults F7).
CREATE TABLE sim.fact_version (
    key text NOT NULL CHECK (key ~ (
        '^(quote/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
        '|budget/[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
        '|inventory/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
        '|suppliers)$'
    )),
    version bigint NOT NULL CHECK (version >= 1),
    value jsonb NOT NULL,
    valid_from_seq bigint NOT NULL REFERENCES sim.event (seq),
    valid_from_vt bigint NOT NULL,
    cause text NOT NULL CHECK (cause IN ('initial', 'effect', 'control')),
    cause_effect_id text,
    cause_actor text,
    PRIMARY KEY (key, version),
    CONSTRAINT fact_value_fits_its_kind CHECK (sim.fact_value_ok(key, value)),
    CONSTRAINT fact_versions_start_from_the_scenario CHECK ((cause = 'initial') = (version = 1)),
    CONSTRAINT fact_cause_is_named CHECK (
        (cause = 'effect') = (cause_effect_id IS NOT NULL)
        AND (cause = 'control') = (cause_actor IS NOT NULL)
    )
);

-- Every read served: the fact's version at that moment, and who asked (0004).
CREATE TABLE sim.read_log (
    seq bigint PRIMARY KEY REFERENCES sim.event (seq),
    key text NOT NULL,
    version bigint NOT NULL,
    caller text NOT NULL,
    vt bigint NOT NULL,
    FOREIGN KEY (key, version) REFERENCES sim.fact_version (key, version)
);

-- A read serves the latest version.
CREATE FUNCTION sim.check_read() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sim.require_event(NEW.seq, ARRAY['read'], NEW.vt, NEW.caller);
    IF NEW.version IS DISTINCT FROM
       (SELECT max(version) FROM sim.fact_version WHERE key = NEW.key)
    THEN
        RAISE EXCEPTION 'a read of % must serve its latest version', NEW.key;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER read_serves_the_latest_version BEFORE INSERT ON sim.read_log
    FOR EACH ROW EXECUTE FUNCTION sim.check_read();

-- Writes ----------------------------------------------------------------------------------

-- Every write request that reached the destination, and its outcome. A write in state
-- `received` is in flight: it commits only by its deadline, its arrival plus D, unless a
-- fault waived the deadline (F5 beyond the deadline, a contract violation). Otherwise it
-- is aborted and never commits (0005). `key_honoured` says whether the destination stores
-- and enforces the write's idempotency key.
CREATE TABLE sim.write (
    received_seq bigint PRIMARY KEY REFERENCES sim.event (seq),
    received_vt bigint NOT NULL,
    caller text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('submit_purchase_order', 'reserve_inventory')),
    intent_id text NOT NULL,
    approval_id text NOT NULL,
    args jsonb NOT NULL CHECK (jsonb_typeof(args) = 'object'),
    basis jsonb NOT NULL CHECK (jsonb_typeof(basis) = 'object'),
    idempotency_key text,
    key_honoured boolean NOT NULL,
    client_ref text,
    deadline_vt bigint NOT NULL,
    deadline_waived boolean NOT NULL,
    state text NOT NULL CHECK (state IN (
        'received', 'committed', 'replayed', 'in_progress', 'rejected', 'aborted'
    )),
    outcome_seq bigint UNIQUE REFERENCES sim.event (seq),
    outcome_vt bigint,
    effect_id text,
    rejection jsonb,
    CONSTRAINT write_outcome_recorded CHECK (
        (state = 'received') = (outcome_seq IS NULL)
        AND (outcome_seq IS NULL) = (outcome_vt IS NULL)
    ),
    CONSTRAINT write_outcome_after_receipt CHECK (outcome_seq > received_seq),
    CONSTRAINT write_effect_when_committed
        CHECK ((state IN ('committed', 'replayed')) = (effect_id IS NOT NULL)),
    CONSTRAINT write_rejection_when_rejected CHECK ((state = 'rejected') = (rejection IS NOT NULL)),
    CONSTRAINT write_key_honoured_needs_a_key
        CHECK (NOT key_honoured OR idempotency_key IS NOT NULL)
);

-- A key the destination honours is in flight at most once (0005).
CREATE UNIQUE INDEX write_key_in_flight_once ON sim.write (idempotency_key)
    WHERE key_honoured AND state = 'received';

CREATE INDEX write_in_flight_by_deadline ON sim.write (deadline_vt) WHERE state = 'received';

-- Every committed write: the effects ledger (0004). One committed write is one effect.
CREATE TABLE sim.effect (
    effect_id text PRIMARY KEY,
    intent_id text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('submit_purchase_order', 'reserve_inventory')),
    args jsonb NOT NULL,
    approval_id text NOT NULL,
    basis jsonb NOT NULL,
    idempotency_key text UNIQUE,
    client_ref text,
    received_seq bigint NOT NULL UNIQUE REFERENCES sim.write (received_seq),
    commit_seq bigint NOT NULL UNIQUE REFERENCES sim.event (seq),
    committed_vt bigint NOT NULL,
    CONSTRAINT effect_id_names_its_operation CHECK (
        (operation = 'submit_purchase_order' AND effect_id ~ '^PO-[0-9]{4,}$')
        OR (operation = 'reserve_inventory' AND effect_id ~ '^RS-[0-9]{4,}$')
    ),
    CONSTRAINT effect_commits_after_receipt CHECK (commit_seq > received_seq)
);

CREATE INDEX effect_by_intent ON sim.effect (intent_id, operation, commit_seq);
CREATE INDEX effect_by_approval ON sim.effect (approval_id);

ALTER TABLE sim.write ADD CONSTRAINT write_effect_exists
    FOREIGN KEY (effect_id) REFERENCES sim.effect (effect_id);
ALTER TABLE sim.fact_version ADD CONSTRAINT fact_version_cause_exists
    FOREIGN KEY (cause_effect_id) REFERENCES sim.effect (effect_id);
CREATE INDEX fact_version_by_effect ON sim.fact_version (cause_effect_id)
    WHERE cause_effect_id IS NOT NULL;

-- Approvals -------------------------------------------------------------------------------

-- The authority record (0002). `status` is stored as pending, approved, denied or
-- revoked; an approved approval is expired from `expires_at` on, which needs no row
-- change. `effect_id` is the effect the approval backs: the first to cite it (rule 10).
CREATE TABLE sim.approval (
    approval_id text PRIMARY KEY CHECK (approval_id ~ '^APR-[0-9]{4,}$'),
    intent_id text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('submit_purchase_order', 'reserve_inventory')),
    scope jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'object'),
    client_request_key text UNIQUE,
    requester text NOT NULL,
    requested_seq bigint NOT NULL UNIQUE REFERENCES sim.event (seq),
    requested_vt bigint NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'revoked')),
    decided_seq bigint UNIQUE REFERENCES sim.event (seq),
    decided_vt bigint,
    decided_by text,
    expires_at bigint,
    revoked_seq bigint UNIQUE REFERENCES sim.event (seq),
    revoked_vt bigint,
    revoked_by text,
    effect_id text UNIQUE REFERENCES sim.effect (effect_id),
    CONSTRAINT approval_decision_recorded CHECK (
        (status = 'pending') = (decided_seq IS NULL)
        AND (decided_seq IS NULL) = (decided_vt IS NULL)
        AND (decided_seq IS NULL) = (decided_by IS NULL)
    ),
    CONSTRAINT approval_expiry_when_approved
        CHECK ((status IN ('approved', 'revoked')) = (expires_at IS NOT NULL)),
    CONSTRAINT approval_revocation_recorded CHECK (
        (status = 'revoked') = (revoked_seq IS NOT NULL)
        AND (revoked_seq IS NULL) = (revoked_vt IS NULL)
        AND (revoked_seq IS NULL) = (revoked_by IS NULL)
    )
);

-- Every change to an approval: requested, approved, denied, revoked (0002, rule 9).
CREATE TABLE sim.approval_event (
    seq bigint PRIMARY KEY REFERENCES sim.event (seq),
    approval_id text NOT NULL REFERENCES sim.approval (approval_id),
    kind text NOT NULL CHECK (kind IN ('requested', 'approved', 'denied', 'revoked')),
    actor text NOT NULL,
    vt bigint NOT NULL
);

CREATE FUNCTION sim.check_approval_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sim.require_event(NEW.seq, ARRAY['approval_' || NEW.kind], NEW.vt, NEW.actor);
    RETURN NEW;
END
$$;

CREATE TRIGGER approval_event_matches_its_event BEFORE INSERT ON sim.approval_event
    FOR EACH ROW EXECUTE FUNCTION sim.check_approval_event();

-- An approval is filed pending. It then moves only from pending to approved or denied,
-- and from approved to revoked, each move recorded by its approval event; an approval
-- expires `approval_ttl_ms` after its decision. Its request never changes, and it backs
-- at most one effect, which must cite it.
CREATE FUNCTION sim.keep_approval() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ttl bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.effect_id IS NOT NULL THEN
            RAISE EXCEPTION 'approval % must be filed pending', NEW.approval_id;
        END IF;
        PERFORM sim.require_event(NEW.requested_seq, ARRAY['approval_requested'],
            NEW.requested_vt, NEW.requester);
        RETURN NEW;
    END IF;
    IF (NEW.approval_id, NEW.intent_id, NEW.operation, NEW.scope, NEW.client_request_key,
        NEW.requester, NEW.requested_seq, NEW.requested_vt)
       IS DISTINCT FROM
       (OLD.approval_id, OLD.intent_id, OLD.operation, OLD.scope, OLD.client_request_key,
        OLD.requester, OLD.requested_seq, OLD.requested_vt)
    THEN
        RAISE EXCEPTION 'the request of approval % never changes', OLD.approval_id;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT (OLD.status = 'pending' AND NEW.status IN ('approved', 'denied'))
       AND NOT (OLD.status = 'approved' AND NEW.status = 'revoked')
    THEN
        RAISE EXCEPTION 'approval % cannot move from % to %',
            OLD.approval_id, OLD.status, NEW.status;
    END IF;
    IF OLD.decided_seq IS NOT NULL
       AND (NEW.decided_seq, NEW.decided_vt, NEW.decided_by, NEW.expires_at)
           IS DISTINCT FROM (OLD.decided_seq, OLD.decided_vt, OLD.decided_by, OLD.expires_at)
    THEN
        RAISE EXCEPTION 'the decision on approval % never changes', OLD.approval_id;
    END IF;
    IF OLD.decided_seq IS NULL AND NEW.decided_seq IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM sim.approval_event
            WHERE seq = NEW.decided_seq AND approval_id = NEW.approval_id
              AND kind = CASE NEW.status WHEN 'denied' THEN 'denied' ELSE 'approved' END
              AND vt = NEW.decided_vt AND actor = NEW.decided_by
        ) THEN
            RAISE EXCEPTION 'the decision on approval % has no matching event', NEW.approval_id;
        END IF;
        SELECT (authority ->> 'approval_ttl_ms')::bigint INTO ttl FROM sim.run;
        IF NEW.status = 'approved' AND NEW.expires_at IS DISTINCT FROM NEW.decided_vt + ttl THEN
            RAISE EXCEPTION 'approval % must expire % ms after its decision',
                NEW.approval_id, ttl;
        END IF;
    END IF;
    IF OLD.revoked_seq IS NOT NULL
       AND (NEW.revoked_seq, NEW.revoked_vt, NEW.revoked_by)
           IS DISTINCT FROM (OLD.revoked_seq, OLD.revoked_vt, OLD.revoked_by)
    THEN
        RAISE EXCEPTION 'the revocation of approval % never changes', OLD.approval_id;
    END IF;
    IF OLD.revoked_seq IS NULL AND NEW.revoked_seq IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM sim.approval_event
        WHERE seq = NEW.revoked_seq AND approval_id = NEW.approval_id AND kind = 'revoked'
          AND vt = NEW.revoked_vt AND actor = NEW.revoked_by
    ) THEN
        RAISE EXCEPTION 'the revocation of approval % has no matching event', NEW.approval_id;
    END IF;
    IF NEW.effect_id IS DISTINCT FROM OLD.effect_id THEN
        IF OLD.effect_id IS NOT NULL OR NOT EXISTS (
            SELECT 1 FROM sim.effect
            WHERE effect_id = NEW.effect_id AND approval_id = NEW.approval_id
        ) THEN
            RAISE EXCEPTION 'approval % backs its first citing effect, once', NEW.approval_id;
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER approval_moves_forward BEFORE INSERT OR UPDATE ON sim.approval
    FOR EACH ROW EXECUTE FUNCTION sim.keep_approval();

-- Fact version rules ----------------------------------------------------------------------

CREATE FUNCTION sim.check_fact_version() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    previous sim.fact_version;
    source sim.effect;
BEGIN
    PERFORM sim.require_event(NEW.valid_from_seq,
        CASE NEW.cause
            WHEN 'initial' THEN ARRAY['setup']
            WHEN 'effect' THEN ARRAY['write_committed']
            ELSE ARRAY['fact_changed']
        END,
        NEW.valid_from_vt, NEW.cause_actor);
    SELECT * INTO previous FROM sim.fact_version
        WHERE key = NEW.key ORDER BY version DESC LIMIT 1;
    IF FOUND THEN
        IF NEW.version <> previous.version + 1 OR NEW.valid_from_seq <= previous.valid_from_seq
        THEN
            RAISE EXCEPTION 'version % of % must follow version % from a later event',
                NEW.version, NEW.key, previous.version;
        END IF;
    ELSIF NEW.version <> 1 THEN
        RAISE EXCEPTION 'the first version of % must be 1', NEW.key;
    END IF;
    IF NEW.cause = 'effect' THEN
        SELECT * INTO source FROM sim.effect WHERE effect_id = NEW.cause_effect_id;
        IF NOT FOUND OR previous IS NULL
           OR NEW.valid_from_seq <> source.commit_seq
           OR NEW.key IS DISTINCT FROM sim.consequence_key(source.operation, source.args)
           OR NEW.value IS DISTINCT FROM
              sim.consequence_value(source.operation, source.args, previous.value)
        THEN
            RAISE EXCEPTION 'version % of % is not the consequence of effect %',
                NEW.version, NEW.key, NEW.cause_effect_id;
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER fact_version_follows_history BEFORE INSERT ON sim.fact_version
    FOR EACH ROW EXECUTE FUNCTION sim.check_fact_version();

-- Write and effect rules ------------------------------------------------------------------

-- A write is received in flight, or decided at once (replayed, in progress, rejected).
-- It then moves only from `received` to an outcome, recorded by an event of that kind,
-- and what it asked for never changes. Its deadline is its arrival plus D, and it keeps
-- its idempotency key exactly when the destination has the idempotency_key capability.
CREATE FUNCTION sim.keep_write() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    run sim.run;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT * INTO run FROM sim.run;
        PERFORM sim.require_event(NEW.received_seq, ARRAY['write_received'],
            NEW.received_vt, NEW.caller);
        IF NEW.deadline_vt <> NEW.received_vt + run.commit_deadline_ms
           OR NEW.key_honoured IS DISTINCT FROM
              ('idempotency_key' = ANY(run.capabilities) AND NEW.idempotency_key IS NOT NULL)
        THEN
            RAISE EXCEPTION 'write % breaks the destination''s deadline or key rules',
                NEW.received_seq;
        END IF;
        IF NEW.state = 'committed' OR NEW.state = 'aborted' THEN
            RAISE EXCEPTION 'write % must be received before it commits or aborts',
                NEW.received_seq;
        END IF;
    ELSE
        IF (NEW.received_seq, NEW.received_vt, NEW.caller, NEW.operation, NEW.intent_id,
            NEW.approval_id, NEW.args, NEW.basis, NEW.idempotency_key, NEW.key_honoured,
            NEW.client_ref, NEW.deadline_vt, NEW.deadline_waived)
           IS DISTINCT FROM
           (OLD.received_seq, OLD.received_vt, OLD.caller, OLD.operation, OLD.intent_id,
            OLD.approval_id, OLD.args, OLD.basis, OLD.idempotency_key, OLD.key_honoured,
            OLD.client_ref, OLD.deadline_vt, OLD.deadline_waived)
        THEN
            RAISE EXCEPTION 'write % never changes what it asked for', OLD.received_seq;
        END IF;
        IF OLD.state <> 'received' OR NEW.state = 'received' THEN
            RAISE EXCEPTION 'write % has its outcome already', OLD.received_seq;
        END IF;
    END IF;
    IF NEW.state = 'received' THEN
        RETURN NEW;
    END IF;
    PERFORM sim.require_event(NEW.outcome_seq, ARRAY['write_' || NEW.state], NEW.outcome_vt,
        NULL);
    IF NEW.state = 'committed' AND NOT EXISTS (
        SELECT 1 FROM sim.effect
        WHERE effect_id = NEW.effect_id AND received_seq = NEW.received_seq
          AND commit_seq = NEW.outcome_seq
    ) THEN
        RAISE EXCEPTION 'write % commits as its own effect', NEW.received_seq;
    END IF;
    IF NEW.state = 'replayed' AND NOT (NEW.key_honoured AND EXISTS (
        SELECT 1 FROM sim.effect
        WHERE effect_id = NEW.effect_id AND idempotency_key = NEW.idempotency_key
    )) THEN
        RAISE EXCEPTION 'write % replays only the effect of its own key', NEW.received_seq;
    END IF;
    IF NEW.state = 'in_progress' AND NOT (NEW.key_honoured AND EXISTS (
        SELECT 1 FROM sim.write
        WHERE idempotency_key = NEW.idempotency_key AND key_honoured AND state = 'received'
          AND received_seq <> NEW.received_seq
    )) THEN
        RAISE EXCEPTION 'write % is in progress only while its key is in flight',
            NEW.received_seq;
    END IF;
    IF NEW.state = 'aborted' AND (NEW.deadline_waived OR NEW.outcome_vt <= NEW.deadline_vt)
    THEN
        RAISE EXCEPTION 'write % aborts only after its deadline', NEW.received_seq;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER write_moves_forward BEFORE INSERT OR UPDATE ON sim.write
    FOR EACH ROW EXECUTE FUNCTION sim.keep_write();

-- An effect repeats its write, which is in flight, and commits by the write's deadline
-- unless a fault waived it. It keeps the write's key exactly when the destination honours
-- it. At C3 its approval is approved, unrevoked, unexpired and backs no effect yet.
CREATE FUNCTION sim.check_effect() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    request sim.write;
    run sim.run;
    cited sim.approval;
BEGIN
    SELECT * INTO run FROM sim.run;
    SELECT * INTO request FROM sim.write WHERE received_seq = NEW.received_seq;
    IF NOT FOUND OR request.state <> 'received'
       OR (NEW.intent_id, NEW.operation, NEW.args, NEW.approval_id, NEW.basis, NEW.client_ref)
          IS DISTINCT FROM
          (request.intent_id, request.operation, request.args, request.approval_id,
           request.basis, request.client_ref)
       OR NEW.idempotency_key IS DISTINCT FROM
          (CASE WHEN request.key_honoured THEN request.idempotency_key END)
    THEN
        RAISE EXCEPTION 'effect % does not repeat the write it commits', NEW.effect_id;
    END IF;
    PERFORM sim.require_event(NEW.commit_seq, ARRAY['write_committed'], NEW.committed_vt, NULL);
    IF NEW.committed_vt > request.deadline_vt AND NOT request.deadline_waived THEN
        RAISE EXCEPTION 'effect % commits after its deadline', NEW.effect_id;
    END IF;
    IF 'conditional_write' = ANY(run.capabilities) THEN
        SELECT * INTO cited FROM sim.approval WHERE approval_id = NEW.approval_id;
        IF NOT FOUND OR cited.status <> 'approved' OR cited.expires_at <= NEW.committed_vt
           OR cited.effect_id IS NOT NULL
        THEN
            RAISE EXCEPTION 'at C3, effect % needs a valid unused approval', NEW.effect_id;
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER effect_repeats_its_write BEFORE INSERT ON sim.effect
    FOR EACH ROW EXECUTE FUNCTION sim.check_effect();

-- By the end of its transaction, an effect has its consequence and its write is committed.
CREATE FUNCTION sim.effect_is_complete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM sim.fact_version WHERE cause_effect_id = NEW.effect_id)
       OR NOT EXISTS (
           SELECT 1 FROM sim.write
           WHERE received_seq = NEW.received_seq AND state = 'committed'
             AND effect_id = NEW.effect_id
       )
    THEN
        RAISE EXCEPTION 'effect % needs its consequence and its committed write', NEW.effect_id;
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER effect_is_complete AFTER INSERT ON sim.effect
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sim.effect_is_complete();

-- Ground truth is never deleted -----------------------------------------------------------

CREATE TRIGGER event_is_append_only BEFORE UPDATE OR DELETE ON sim.event
    FOR EACH ROW EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER fact_version_is_append_only BEFORE UPDATE OR DELETE ON sim.fact_version
    FOR EACH ROW EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER read_log_is_append_only BEFORE UPDATE OR DELETE ON sim.read_log
    FOR EACH ROW EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER effect_is_append_only BEFORE UPDATE OR DELETE ON sim.effect
    FOR EACH ROW EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER approval_event_is_append_only BEFORE UPDATE OR DELETE ON sim.approval_event
    FOR EACH ROW EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER run_is_never_deleted BEFORE DELETE ON sim.run
    FOR EACH ROW EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER write_is_never_deleted BEFORE DELETE ON sim.write
    FOR EACH ROW EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER approval_is_never_deleted BEFORE DELETE ON sim.approval
    FOR EACH ROW EXECUTE FUNCTION sim.reject_change();

CREATE TRIGGER run_is_never_truncated BEFORE TRUNCATE ON sim.run
    FOR EACH STATEMENT EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER event_is_never_truncated BEFORE TRUNCATE ON sim.event
    FOR EACH STATEMENT EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER fact_version_is_never_truncated BEFORE TRUNCATE ON sim.fact_version
    FOR EACH STATEMENT EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER read_log_is_never_truncated BEFORE TRUNCATE ON sim.read_log
    FOR EACH STATEMENT EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER write_is_never_truncated BEFORE TRUNCATE ON sim.write
    FOR EACH STATEMENT EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER effect_is_never_truncated BEFORE TRUNCATE ON sim.effect
    FOR EACH STATEMENT EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER approval_is_never_truncated BEFORE TRUNCATE ON sim.approval
    FOR EACH STATEMENT EXECUTE FUNCTION sim.reject_change();
CREATE TRIGGER approval_event_is_never_truncated BEFORE TRUNCATE ON sim.approval_event
    FOR EACH STATEMENT EXECUTE FUNCTION sim.reject_change();
