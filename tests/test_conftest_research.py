"""The domino-study tests need the optional `research` dependencies; without them they are skipped at collection
instead of breaking the production test run."""
from __future__ import annotations

from tests import conftest


def test_research_tests_are_ignored_when_a_research_dependency_is_missing():
    missing = lambda name: None if name == "scipy" else object()     # noqa: E731
    assert conftest.research_ignores(missing) == ["test_dominoes_*.py", "dominoes_synth.py"]


def test_research_tests_run_when_every_research_dependency_is_present():
    assert conftest.research_ignores(lambda name: object()) == []
