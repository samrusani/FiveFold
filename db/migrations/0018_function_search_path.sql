-- Resolve built-in types and operators before names in any temporary schema.
SET LOCAL search_path = pg_catalog, pg_temp;

CREATE OR REPLACE FUNCTION sim.fact_value_ok(key text, value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT coalesce(jsonb_typeof(value) = 'object' AND CASE split_part(key, '/', 1)
        WHEN 'quote' THEN
            sim.is_integer(value -> 'unit_price_minor')
            AND jsonb_typeof(value -> 'currency') = 'string'
            AND sim.is_integer(value -> 'min_quantity')
            AND sim.is_integer(value -> 'lead_time_days')
            AND jsonb_typeof(value -> 'status') = 'string'
        WHEN 'memory' THEN
            jsonb_typeof(value -> 'order_channel') = 'string'
            AND jsonb_typeof(value -> 'notes') = 'string'
            AND value - 'order_channel' - 'notes' = '{}'::pg_catalog.jsonb
        WHEN 'budget' THEN
            sim.is_integer(value -> 'limit_minor') AND sim.is_integer(value -> 'committed_minor')
        WHEN 'inventory' THEN
            sim.is_integer(value -> 'available')
        WHEN 'suppliers' THEN
            jsonb_typeof(value -> 'suppliers') = 'array'
        ELSE false
    END, false)
$$;

ALTER FUNCTION gw.keep_action_identity() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION gw.keep_attempt_history() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION gw.keep_effect_id() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION gw.keep_grant_binding() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION gw.keep_intent() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION gw.keep_run_state() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION gw.reject_change() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION gw.reject_row_change() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION harness.check_copy() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION harness.keep_message_history() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION harness.reject_change() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.check_approval_event() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.check_effect() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.check_fact_version() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.check_read() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.consequence_key(text, jsonb) SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.consequence_value(text, jsonb, jsonb) SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.effect_is_complete() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.is_integer(jsonb) SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.keep_approval() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.keep_run() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.keep_write() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.next_event() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.reject_change() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION sim.require_event(bigint, text[], bigint, text) SET search_path = pg_catalog, pg_temp;
