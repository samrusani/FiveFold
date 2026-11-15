-- Component roles (decision record 0008), and ff_registry, which writes the run registry
-- (0012, db/registry/). It has no privilege on the schemas of a run database.
--
-- Roles belong to the whole PostgreSQL server, so this script runs once per server, as a
-- superuser, before any migration. It is safe to run again.
--
-- The roles can log in but have no password. Where the server uses password
-- authentication, set passwords outside this repository.

DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'ff_sim', 'ff_gw', 'ff_harness', 'ff_agent', 'ff_eval', 'ff_viewer', 'ff_registry'
    ]
    LOOP
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('CREATE ROLE %I LOGIN', role_name);
        END IF;
    END LOOP;
END
$$;

-- LangGraph's checkpointer keeps its tables in schema agent (0008).
ALTER ROLE ff_agent SET search_path = agent;
