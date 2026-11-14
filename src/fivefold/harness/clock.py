"""The run's virtual clock (0009). Implemented in milestone M1.

Every component reads time only from this clock. This module and `rng.py` are the only
places allowed to use wall-clock time or system randomness; the lint configuration in
pyproject.toml enforces it (0007). Wall-clock time is recorded for metrics only.
"""
