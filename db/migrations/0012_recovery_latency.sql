-- What recovery latency reads from the harness (decision record 0014).
--
-- `fault_log.intent_id` names the task intent a fault touched: the intent of the message,
-- action or tool call it acted on (0014, decision 2). It is null for F9, which touches
-- every task intent, and for a fault on a message without an intent, such as a read. F10
-- acts on the next `count` status answers after it fires, which can concern several
-- intents. So besides the row written when F10 fires, the harness writes one row for
-- each status answer F10 changes, with that answer as `target_seq` and its intent. A
-- fault row is history: the intent is set when the row is written, never later.
--
-- `message_log.delivered_wall_at` is the wall-clock time of a delivery, set with its fate.
-- LangGraph's retry backoff holds a run in wall time, so a message's send (`wall_at`) and
-- its delivery can be far apart. Like every wall-clock column, it is for metrics only and
-- never decides anything (0009). A dropped message has none. The history rule of
-- 0009_message_copies.sql lets it be set once, with the fate, and never changed.

ALTER TABLE harness.fault_log ADD COLUMN intent_id text;

ALTER TABLE harness.message_log
    ADD COLUMN delivered_wall_at timestamptz,
    ADD CONSTRAINT message_log_delivered_wall_at_check CHECK (
        delivered_wall_at IS NULL OR fate IS NOT DISTINCT FROM 'delivered'
    );
