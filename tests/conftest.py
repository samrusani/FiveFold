"""Shared test configuration: Hypothesis profiles (0007).

Select a profile with HYPOTHESIS_PROFILE. CI uses `ci`, which derandomizes Hypothesis so
results are reproducible. The nightly job uses `nightly`, which explores far more examples.
"""

import os

from hypothesis import settings

settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
settings.register_profile("nightly", max_examples=2_000, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
