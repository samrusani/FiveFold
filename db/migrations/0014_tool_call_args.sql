-- The arguments of each logged call (decision record 0003, data model: `tool_call: seq,
-- tool, args, result, action_id, vt`; docs/spec.md, Data model, ToolCall).
--
-- `args` holds the arguments of the call a response answers, as the gateway received
-- them, decoded from JSON: a write's business arguments, `intent_id`, `based_on`, `note`
-- and any field outside its schema; `get_action_status`'s action id and wait; a final
-- report's claims. Both topologies refuse an agent's message holding a NUL character
-- before the gateway sees it (`fivefold.harness.log.holds_nul`), and the store keeps each
-- value as `fivefold.gateway.codec.storable` makes it.
--
-- NOT NULL, with no default. A run database is migrated before its run starts, so every
-- database the repository migrates has no logged call yet when this file runs. The
-- arguments of calls logged before this file were never recorded, and an empty object
-- would say there were none, so a database whose `gw.tool_call` already holds rows
-- cannot take this file: PostgreSQL refuses a NOT NULL column with no default on a table
-- with rows, the file rolls back and the runner stops. Run databases are disposable
-- (0007); make a new one.

ALTER TABLE gw.tool_call
    ADD COLUMN args jsonb NOT NULL CHECK (jsonb_typeof(args) = 'object');
