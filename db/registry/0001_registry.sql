-- The run registry (decision records 0009 and 0012).
--
-- The registry is a small database of its own, `fivefold_registry` unless the environment
-- variable FIVEFOLD_REGISTRY_DB names another, on the same server as the run databases.
-- It has one row per run database, so the viewer can list runs without opening each one.
-- The harness writes a run's row as the run starts, as role ff_registry; the viewer reads
-- the rows as role ff_viewer, which can only select. No other role has any privilege here.
--
-- A row is keyed by its database's name. A run id names a run's choices, and two
-- databases can hold runs with the same id: a recording and its replay, for example.
-- When a database is created again under its old name, as each demo's is, the harness
-- replaces the row; when a run database is dropped, the harness removes it.
-- `created_wall_at` is wall-clock time, for display only (0009).

CREATE SCHEMA registry;

GRANT USAGE ON SCHEMA registry TO ff_registry, ff_viewer;
ALTER DEFAULT PRIVILEGES IN SCHEMA registry
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ff_registry;
ALTER DEFAULT PRIVILEGES IN SCHEMA registry
    GRANT SELECT ON TABLES TO ff_viewer;

CREATE TABLE registry.run (
    database_name text PRIMARY KEY CHECK (database_name ~ '^[a-z_][a-z0-9_]{0,62}$'),
    run_id text NOT NULL,
    scenario_id text NOT NULL,
    config text NOT NULL CHECK (config IN ('B0', 'G1', 'G2')),
    contract text NOT NULL CHECK (contract IN ('C0', 'C1', 'C2', 'C3')),
    seed bigint NOT NULL,
    created_wall_at timestamptz NOT NULL DEFAULT now()
);
