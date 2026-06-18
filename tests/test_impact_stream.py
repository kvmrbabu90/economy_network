"""Tests for the streaming impact generator (run_impact_stream).

A deterministic fake LLM echoes verdicts derived from the prompt text, so the
BFS traverses the real econgraph.db graph reproducibly without calling Claude.
MAX_FRONTIER is patched high so the random frontier sampler never fires.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from api import impact as impact_mod
from schema.store import connect

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "econgraph.db"


@pytest.fixture
def conn():
    if not DB_PATH.exists():
        pytest.skip("econgraph.db missing; run `python -m pipeline.build_graph` first")
    c = connect(DB_PATH)
    yield c
    c.close()


def _fake_llm(prompt: str) -> str:
    """Route by prompt shape; echo verdicts for every id found in the prompt."""
    # Entity extraction: no named companies -> let the commodity seed anchor.
    if "Extract ONLY investable companies" in prompt:
        return "[]"
    # Seed selection: pick the FIRST candidate id from the "id | type | name" list.
    if "Pick the ONE node" in prompt:
        m = re.search(r"^\s*(\S+)\s*\|", prompt, re.MULTILINE)
        nid = m.group(1) if m else None
        return json.dumps({"node_id": nid, "direction": "negative", "magnitude": 0.9, "reasoning": "t"})
    # Ring scoring: one verdict per candidate id (first column of each "  id | ..." line).
    if "propagating a news shock" in prompt:
        ids = re.findall(r"^\s{2}(\S+)\s*\|", prompt, re.MULTILINE)
        return json.dumps([
            {"node_id": i, "direction": "negative", "magnitude": 0.5, "reasoning": "t"}
            for i in ids
        ])
    # Refinement: one verdict per "NODE: <id> (" header.
    if "refining impact assessments" in prompt:
        ids = re.findall(r"^NODE:\s*(\S+)\s*\(", prompt, re.MULTILINE)
        return json.dumps([
            {"node_id": i, "direction": "negative", "magnitude": 0.9, "reasoning": "t"}
            for i in ids
        ])
    return ""


@pytest.fixture(autouse=True)
def patch_llm_and_frontier(monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _fake_llm)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)  # disable random sampling


def test_stream_event_ordering(conn):
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "seeds"
    assert kinds[-1] == "done"
    assert "hop" in kinds
    # refinement appears (if any) before done; done is unique and last.
    assert kinds.count("done") == 1
    # No event after done.
    assert kinds.index("done") == len(kinds) - 1


def test_stream_reconciles_with_done(conn):
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    streamed_ids = set()
    done = None
    for e in events:
        if e["event"] == "seeds":
            streamed_ids.update(v["node_id"] for v in e["seeds"])
        elif e["event"] == "hop":
            streamed_ids.update(v["node_id"] for v in e["new_impacts"])
        elif e["event"] == "done":
            done = e
    assert done is not None
    final_ids = {v["node_id"] for v in done["result"]["impacts"]}
    # Every final node was revealed in a stream event; nothing streamed is absent.
    assert streamed_ids == final_ids


def test_wrapper_equals_done_payload(conn):
    # The wrapper must return exactly what the generator's `done` event carried.
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    done_result = next(e["result"] for e in events if e["event"] == "done")
    wrapped = impact_mod.run_impact("global crude oil supply shock", conn=conn)
    assert {v["node_id"] for v in wrapped["impacts"]} == {v["node_id"] for v in done_result["impacts"]}
    assert wrapped.get("max_hops") == done_result.get("max_hops")


def test_empty_text_emits_error_then_done(conn):
    events = list(impact_mod.run_impact_stream("   ", conn=conn))
    assert [e["event"] for e in events] == ["error", "done"]
    assert events[-1]["result"]["impacts"] == []
