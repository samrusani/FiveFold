ALTER TABLE harness.run
    DROP CONSTRAINT run_config_check,
    ADD CONSTRAINT run_config_check CHECK (config IN ('B0', 'B1', 'G1', 'G2'));
