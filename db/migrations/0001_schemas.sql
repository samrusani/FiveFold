-- Schemas and privileges for the components (decision record 0008).
--
-- Tables arrive in later migrations. The default privileges below apply to every table and
-- sequence that the migrating role creates later in each schema, so each new table gets
-- the privileges in 0008's table without repeating them. "Writes" means SELECT, INSERT,
-- UPDATE and DELETE.
--
--   role         sim      gw       harness  agent    eval
--   ff_sim       writes   none     none     none     none
--   ff_gw        none     writes   none     none     none
--   ff_harness   select   none     writes   none     none
--   ff_agent     none     none     none     writes   none
--   ff_eval      select   select   select   none     writes
--   ff_viewer    select   select   select   none     select
--
-- The gateway's append-only action_event table revokes UPDATE and DELETE from ff_gw in the
-- migration that creates it (0003, I6).

CREATE SCHEMA sim;
CREATE SCHEMA gw;
CREATE SCHEMA harness;
CREATE SCHEMA agent;
CREATE SCHEMA eval;

-- sim: ground truth. The simulator writes; the harness, evaluator and viewer read.
GRANT USAGE ON SCHEMA sim TO ff_sim, ff_harness, ff_eval, ff_viewer;
ALTER DEFAULT PRIVILEGES IN SCHEMA sim
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ff_sim;
ALTER DEFAULT PRIVILEGES IN SCHEMA sim
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO ff_sim;
ALTER DEFAULT PRIVILEGES IN SCHEMA sim
    GRANT SELECT ON TABLES TO ff_harness, ff_eval, ff_viewer;

-- gw: the action store. The gateway writes; the evaluator and viewer read.
GRANT USAGE ON SCHEMA gw TO ff_gw, ff_eval, ff_viewer;
ALTER DEFAULT PRIVILEGES IN SCHEMA gw
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ff_gw;
ALTER DEFAULT PRIVILEGES IN SCHEMA gw
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO ff_gw;
ALTER DEFAULT PRIVILEGES IN SCHEMA gw
    GRANT SELECT ON TABLES TO ff_eval, ff_viewer;

-- harness: message, fault and actor logs, and final reports. The harness writes; the
-- evaluator and viewer read.
GRANT USAGE ON SCHEMA harness TO ff_harness, ff_eval, ff_viewer;
ALTER DEFAULT PRIVILEGES IN SCHEMA harness
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ff_harness;
ALTER DEFAULT PRIVILEGES IN SCHEMA harness
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO ff_harness;
ALTER DEFAULT PRIVILEGES IN SCHEMA harness
    GRANT SELECT ON TABLES TO ff_eval, ff_viewer;

-- agent: LangGraph checkpoints. Only the agent runner, which creates its own tables.
GRANT USAGE, CREATE ON SCHEMA agent TO ff_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA agent
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ff_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA agent
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO ff_agent;

-- eval: results. The evaluator writes; the viewer reads.
GRANT USAGE ON SCHEMA eval TO ff_eval, ff_viewer;
ALTER DEFAULT PRIVILEGES IN SCHEMA eval
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ff_eval;
ALTER DEFAULT PRIVILEGES IN SCHEMA eval
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO ff_eval;
ALTER DEFAULT PRIVILEGES IN SCHEMA eval
    GRANT SELECT ON TABLES TO ff_viewer;
