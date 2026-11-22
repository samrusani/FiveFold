ALTER TABLE registry.run
    DROP CONSTRAINT run_config_check,
    ADD CONSTRAINT run_config_check CHECK (config IN ('B0', 'B1', 'B2', 'G1', 'G2'));
