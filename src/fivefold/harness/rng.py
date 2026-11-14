"""The run's seeded generators (0009). Implemented in milestone M1.

The run seed feeds independent named generators (latency, fault plans, data generation,
scripted variation), so adding a new consumer does not shift another's numbers. No other
module may use the global `random` module, `secrets`, `uuid4` or `os.urandom` (0007).
"""
