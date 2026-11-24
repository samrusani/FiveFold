-- The versioned memory facts of record 0023 use the simulator's existing ledger.
ALTER TABLE sim.fact_version DROP CONSTRAINT fact_version_key_check;
ALTER TABLE sim.fact_version ADD CONSTRAINT fact_version_key_check CHECK (key ~ (
    '^(quote/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
    '|memory/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
    '|budget/[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
    '|inventory/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
    '|suppliers)$'
));

CREATE OR REPLACE FUNCTION sim.fact_value_ok(key text, value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
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
            AND value - 'order_channel' - 'notes' = '{}'::jsonb
        WHEN 'budget' THEN
            sim.is_integer(value -> 'limit_minor') AND sim.is_integer(value -> 'committed_minor')
        WHEN 'inventory' THEN
            sim.is_integer(value -> 'available')
        WHEN 'suppliers' THEN
            jsonb_typeof(value -> 'suppliers') = 'array'
        ELSE false
    END, false)
$$;
