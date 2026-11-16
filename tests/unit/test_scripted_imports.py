"""Scripted entry points do not load live provider integrations."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "fivefold.harness.demo",
        "fivefold.harness.study",
        "fivefold.recorder",
        "fivefold.recorder.__main__",
    ],
)
def test_scripted_import_does_not_load_provider_sdks(module: str) -> None:
    script = """
import importlib
import sys
importlib.import_module(sys.argv[1])
providers = {
    'langchain_anthropic', 'langchain_google_genai', 'langchain_ollama', 'langchain_openai',
    'anthropic', 'openai', 'google.genai', 'ollama',
}
loaded = providers.intersection(sys.modules)
assert not loaded, f'provider modules loaded: {sorted(loaded)}'
"""
    subprocess.run(
        [sys.executable, "-c", script, module], check=True, capture_output=True, text=True
    )
