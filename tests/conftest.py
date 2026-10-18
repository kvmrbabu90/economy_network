"""Test-collection guard: the domino-study tests need the optional `research` dependencies
(`pip install -e .[research]`). Without them those tests are skipped at collection, so the production test run
still works on a plain install."""
from __future__ import annotations

import importlib.util

RESEARCH_MODULES = ("pandas", "numpy", "scipy", "pyarrow", "matplotlib")


def research_ignores(find_spec=importlib.util.find_spec) -> list[str]:
    if all(find_spec(m) is not None for m in RESEARCH_MODULES):
        return []
    return ["test_dominoes_*.py", "dominoes_synth.py"]


collect_ignore_glob = research_ignores()
