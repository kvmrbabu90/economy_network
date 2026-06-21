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
    # Ring scoring: one verdict per candidate id (first column of each "  id | ..."
    # line). Scope the scan to the CANDIDATES section so the seed lines embedded
    # higher in the prompt (same "  id | ..." shape) are NOT echoed back — that
    # would mask a regression in the engine's seed-skip logic.
    if "propagating a news shock" in prompt:
        cand_section = prompt.split("CANDIDATES at hop", 1)[-1]
        ids = re.findall(r"^\s{2}(\S+)\s*\|", cand_section, re.MULTILINE)
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


def test_midstream_failure_emits_terminal_error_then_done(conn, monkeypatch):
    # An unexpected failure AFTER the seeds event (here: _neighbors raising
    # during BFS) must still terminate the stream with error + done so the
    # client never hangs waiting for a `done` frame.
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(impact_mod, "_neighbors", boom)
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "seeds"          # seeds were emitted before the failure
    assert "error" in kinds
    assert kinds[-1] == "done"          # stream still closes cleanly
    err = next(e for e in events if e["event"] == "error")
    assert "kaboom" in err["message"]


def _seed_and_entity(prompt):
    """Shared first-stage routing for the coverage fakes."""
    if "Extract ONLY investable companies" in prompt:
        return "[]"
    if "Pick the ONE node" in prompt:
        m = re.search(r"^\s*(\S+)\s*\|", prompt, re.MULTILINE)
        return json.dumps({"node_id": m.group(1) if m else None,
                           "direction": "negative", "magnitude": 0.9, "reasoning": "t"})
    if "refining impact assessments" in prompt:
        ids = re.findall(r"^NODE:\s*(\S+)\s*\(", prompt, re.MULTILINE)
        return json.dumps([{"node_id": i, "direction": "negative",
                            "magnitude": 0.9, "reasoning": "t"} for i in ids])
    return None


def _ring_ids(prompt):
    cand = prompt.split("CANDIDATES at hop", 1)[-1]
    return re.findall(r"^\s{2}(\S+)\s*\|", cand, re.MULTILINE)


def _make_recovery_fake():
    """Omit the first candidate of each ring prompt on its FIRST sighting,
    then include it when re-asked (proves targeted retry recovers gaps)."""
    omitted_once: set[str] = set()

    def fake(prompt):
        pre = _seed_and_entity(prompt)
        if pre is not None:
            return pre
        if "propagating a news shock" in prompt:
            ids = _ring_ids(prompt)
            out = []
            for idx, i in enumerate(ids):
                if idx == 0 and i not in omitted_once:
                    omitted_once.add(i)
                    continue
                out.append({"node_id": i, "direction": "negative",
                            "magnitude": 0.5, "reasoning": "t"})
            return json.dumps(out)
        return ""
    return fake


def _always_omit_first_fake(prompt):
    """Always omit the first candidate of every ring/retry prompt → that id can
    never be recovered and must end up `unscored`."""
    pre = _seed_and_entity(prompt)
    if pre is not None:
        return pre
    if "propagating a news shock" in prompt:
        ids = _ring_ids(prompt)
        return json.dumps([{"node_id": i, "direction": "negative",
                            "magnitude": 0.5, "reasoning": "t"} for i in ids[1:]])
    return ""


def test_retry_recovers_missing_nodes(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _make_recovery_fake())
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 1)
    done = next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "done")
    scoring = done["result"]["scoring"]
    assert scoring["recovered"] >= 1
    # Nothing left unscored — everything was recovered on retry.
    assert scoring["unscored"] == 0
    assert all(v["direction"] != "unscored"
               for v in done["result"]["impacts"] if not v.get("is_seed"))


def test_unrecoverable_nodes_surface_as_unscored(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _always_omit_first_fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 1)
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    done = next(e for e in events if e["event"] == "done")
    scoring = done["result"]["scoring"]
    assert scoring["unscored"] >= 1
    unscored = [v for v in done["result"]["impacts"] if v["direction"] == "unscored"]
    assert len(unscored) == scoring["unscored"]
    assert {v["node_id"] for v in unscored} == set(scoring["unscored_node_ids"])
    # Surfaced, not dropped: each carries the standard fields.
    assert all(u["magnitude"] == 0.0 and u["hop"] >= 1 for u in unscored)


def test_every_ring_candidate_is_accounted_for(conn, monkeypatch):
    # Coverage invariant: with omissions forced, each hop still yields one
    # impact per ring candidate (scored or unscored) — nothing vanishes.
    monkeypatch.setattr(impact_mod, "_llm_call", _always_omit_first_fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 1)
    for ev in impact_mod.run_impact_stream("global crude oil supply shock", conn=conn):
        if ev["event"] == "hop":
            assert len(ev["new_impacts"]) == ev["ring_size"]


def test_unscored_nodes_do_not_propagate(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _always_omit_first_fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 1)
    done = next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "done")
    impacts = done["result"]["impacts"]
    unscored_ids = {v["node_id"] for v in impacts if v["direction"] == "unscored"}
    # No node was discovered via an unscored parent.
    assert all(v.get("via_parent") not in unscored_ids for v in impacts)


def test_scoring_summary_is_consistent(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _always_omit_first_fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 1)
    done = next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "done")
    r = done["result"]
    scoring = r["scoring"]
    assert set(scoring) == {"scored", "recovered", "unscored", "unscored_node_ids"}
    non_seed = [v for v in r["impacts"] if not v.get("is_seed")]
    assert scoring["scored"] + scoring["unscored"] == len(non_seed)
