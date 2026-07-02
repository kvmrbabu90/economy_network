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
from schema.store import connect, default_db_path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = default_db_path()   # honors ECONGRAPH_DB so this tracks the relocated DB


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


def test_zero_retries_degrades_cleanly(conn, monkeypatch):
    # With retries disabled, missing ids are NOT recovered — they go straight to
    # `unscored`. Coverage must still hold (every candidate accounted for).
    monkeypatch.setattr(impact_mod, "_llm_call", _always_omit_first_fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "RING_SCORE_RETRIES", 0)
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    done = next(e for e in events if e["event"] == "done")
    scoring = done["result"]["scoring"]
    assert scoring["recovered"] == 0          # no retry rounds ran
    assert scoring["unscored"] >= 1           # omitted ids surface as unscored
    # Coverage invariant still holds per hop.
    for ev in events:
        if ev["event"] == "hop":
            assert len(ev["new_impacts"]) == ev["ring_size"]


def _verify_fake(decision: str):
    """Wrap _fake_llm: answer the verify prompt with `decision` for the first
    NODE and 'upheld' for the rest; delegate seeds/ring/refine to _fake_llm."""
    def fake(prompt: str) -> str:
        if "TRY TO REFUTE" in prompt:
            ids = re.findall(r"^NODE:\s*(\S+)\s*\(", prompt, re.MULTILINE)
            out = [{"node_id": i, "verdict": (decision if idx == 0 else "upheld"),
                    "confidence": 0.8, "reasoning": "t"} for idx, i in enumerate(ids)]
            return json.dumps(out)
        return _fake_llm(prompt)
    return fake


def _run_done(conn):
    return next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "done")["result"]


def test_refute_downgrades_to_no_effect(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _verify_fake("refuted"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.45)
    r = _run_done(conn)
    refuted = [v for v in r["impacts"] if v.get("verification", {}).get("verdict") == "refuted"]
    assert refuted, "expected at least one refuted node"
    for v in refuted:
        assert v["direction"] == "no_effect" and v["magnitude"] == 0.0
        assert v["verified"] is True
    assert r["verification"]["refuted"] >= 1


def test_weaken_halves_magnitude(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _verify_fake("weakened"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.45)
    r = _run_done(conn)
    weakened = [v for v in r["impacts"] if v.get("verification", {}).get("verdict") == "weakened"]
    assert weakened
    # Ring fake scores candidates at magnitude 0.5 → weakened halves to 0.25.
    for v in weakened:
        assert abs(v["magnitude"] - 0.25) < 1e-9
    assert r["verification"]["weakened"] >= 1


def test_upheld_unchanged_but_annotated(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _verify_fake("upheld"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.45)
    r = _run_done(conn)
    upheld = [v for v in r["impacts"] if v.get("verification", {}).get("verdict") == "upheld"]
    assert upheld
    for v in upheld:
        assert v["direction"] in ("positive", "negative")  # unchanged
        assert v["verified"] is True and 0.0 <= v["confidence"] <= 1.0


def test_only_strong_verdicts_checked(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _verify_fake("refuted"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.99)  # nothing qualifies
    r = _run_done(conn)
    assert r["verification"] == {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0}
    assert not any(v.get("verified") for v in r["impacts"])


def test_verify_fail_open_leaves_verdicts_unchanged(conn, monkeypatch):
    # autouse _fake_llm returns "" for the verify prompt → no adjudication.
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.45)
    r = _run_done(conn)
    assert r["verification"]["checked"] == 0
    assert not any(v.get("verified") for v in r["impacts"])
    # Strong verdicts retained (not downgraded by an unparseable verifier).
    assert any(v["direction"] in ("positive", "negative") and v["magnitude"] >= 0.45
               for v in r["impacts"] if not v.get("is_seed"))


def test_verification_disabled(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _verify_fake("refuted"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_ENABLED", False)
    events = list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    vevent = next(e for e in events if e["event"] == "verification")
    assert vevent["summary"] == {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0}
    done = next(e for e in events if e["event"] == "done")["result"]
    assert not any(v.get("verified") for v in done["impacts"])


def test_event_ordering_includes_verification(conn):
    kinds = [e["event"] for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn)]
    assert kinds[0] == "seeds" and kinds[-1] == "done"
    assert "verification" in kinds
    # verification comes after the (single) refinement event, before done.
    assert kinds.index("verification") > kinds.index("refinement")
    assert kinds.index("verification") < kinds.index("done")


def test_heuristic_confidence_values():
    assert impact_mod._heuristic_confidence(1, True) == 0.70
    assert impact_mod._heuristic_confidence(1, False) == 0.85
    assert impact_mod._heuristic_confidence(3, True) == 0.45
    assert impact_mod._heuristic_confidence(3, False) == 0.60
    # never reads as near-certain for an unverified heuristic
    assert impact_mod._heuristic_confidence(1, False) <= 0.90


def test_every_verdict_has_confidence(conn):
    r = _run_done(conn)  # autouse echo fake; no verifier adjudication
    for v in r["impacts"]:
        if v.get("is_seed"):
            assert v["confidence"] == 1.0
        elif v["direction"] in ("positive", "negative"):
            assert isinstance(v["confidence"], (int, float)) and 0.0 < v["confidence"] <= 1.0
        elif v["direction"] == "unscored":
            assert v["confidence"] == 0.0
        else:  # no_effect
            assert v.get("confidence") is None


def test_verifier_confidence_overwrites_heuristic(conn, monkeypatch):
    def fake(prompt):
        if "TRY TO REFUTE" in prompt:
            ids = re.findall(r"^NODE:\s*(\S+)\s*\(", prompt, re.MULTILINE)
            return json.dumps([{"node_id": i, "verdict": "upheld",
                                "confidence": 0.33, "reasoning": "t"} for i in ids])
        return _fake_llm(prompt)
    monkeypatch.setattr(impact_mod, "_llm_call", fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.45)
    r = _run_done(conn)
    verified = [v for v in r["impacts"] if v.get("verified")]
    assert verified and all(abs(v["confidence"] - 0.33) < 1e-9 for v in verified)


# --- Seed directness audit (A+B): reject speculative commodity seeds ----------

def _seed_audit_fake(decision: str):
    """Route the seed-directness audit prompt to `decision`; delegate the rest
    (entity extraction, seed pick, ring, refine) to _fake_llm."""
    def fake(prompt: str) -> str:
        if "auditing a seed choice" in prompt:
            return json.dumps({"verdict": decision, "reasoning": "t"})
        return _fake_llm(prompt)
    return fake


def test_indirect_seed_is_dropped(conn, monkeypatch):
    # _fake_llm picks the first commodity candidate as the seed and names no
    # company. If the audit calls that seed "indirect", it must be dropped — and
    # with no other seed, the run reports "no seed" rather than propagating from
    # a speculative anchor.
    monkeypatch.setattr(impact_mod, "_llm_call", _seed_audit_fake("indirect"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    events = list(impact_mod.run_impact_stream(
        "US Commerce Department freezes advanced AI chip exports", conn=conn))
    done = next(e for e in events if e["event"] == "done")["result"]
    assert done.get("seed") is None
    assert any(e["event"] == "error" and "seed" in e["message"].lower() for e in events)


def test_direct_seed_is_kept(conn, monkeypatch):
    # Same flow, but the audit says "direct" → the commodity seed survives and the
    # run propagates normally.
    monkeypatch.setattr(impact_mod, "_llm_call", _seed_audit_fake("direct"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    done = next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "done")["result"]
    assert done.get("seed") is not None
    assert done["impacts"]                      # propagation happened


def test_seed_audit_skipped_when_verify_disabled(conn, monkeypatch):
    # With VERIFY_ENABLED off, the audit never runs and the seed is kept even if a
    # stray "indirect" would be returned.
    monkeypatch.setattr(impact_mod, "_llm_call", _seed_audit_fake("indirect"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_ENABLED", False)
    done = next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "done")["result"]
    assert done.get("seed") is not None


# --- Hardening: commodity-seed magnitude clamp + direction validation --------

def _commodity_seed_fake(*, magnitude, direction):
    """No named companies; the commodity seed picks the first candidate with the
    given (possibly malformed) magnitude/direction. Ring/refine delegate to the
    normal echo fake so a kept seed still propagates."""
    def fake(prompt: str) -> str:
        if "Extract ONLY investable companies" in prompt:
            return "[]"
        if "Pick the ONE node" in prompt:
            m = re.search(r"^\s{2}(\S+)\s*\|", prompt, re.MULTILINE)
            return json.dumps({"node_id": m.group(1) if m else None,
                               "direction": direction, "magnitude": magnitude,
                               "reasoning": "t"})
        return _fake_llm(prompt)
    return fake


def test_commodity_seed_magnitude_clamped(conn, monkeypatch):
    # An out-of-range magnitude (>1) from the seed LLM must be clamped into
    # [0,1], not stored raw. The commodity seed is the sole anchor here.
    monkeypatch.setattr(impact_mod, "_llm_call",
                        _commodity_seed_fake(magnitude=5.0, direction="negative"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    seeds = next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "seeds")["seeds"]
    commodity = [s for s in seeds if s["hop"] == 0]
    assert commodity, "expected a hop-0 commodity seed"
    for s in commodity:
        assert 0.0 <= s["magnitude"] <= 1.0        # clamped, never the raw 5.0


def test_commodity_seed_magnitude_junk_falls_back(conn, monkeypatch):
    # A non-numeric magnitude falls back to the 0.9 default rather than crashing.
    monkeypatch.setattr(impact_mod, "_llm_call",
                        _commodity_seed_fake(magnitude="lots", direction="negative"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    seeds = next(e for e in impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn) if e["event"] == "seeds")["seeds"]
    commodity = [s for s in seeds if s["hop"] == 0]
    assert commodity
    assert all(s["magnitude"] == 0.9 for s in commodity)


def test_commodity_seed_invalid_direction_dropped(conn, monkeypatch):
    # An invalid direction ("sideways") must DROP the commodity seed rather than
    # store garbage. With no other seed, the run reports "no seed".
    monkeypatch.setattr(impact_mod, "_llm_call",
                        _commodity_seed_fake(magnitude=0.9, direction="sideways"))
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    events = list(impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn))
    done = next(e for e in events if e["event"] == "done")["result"]
    assert done.get("seed") is None
    assert any(e["event"] == "error" and "seed" in e["message"].lower() for e in events)
    # No seed ever carried the garbage direction.
    seeds_events = [e for e in events if e["event"] == "seeds"]
    for ev in seeds_events:
        assert all(s["direction"] in ("positive", "negative") for s in ev["seeds"])


def test_lighter_config_skips_passes_and_limits_hops(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _fake_llm)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    calls = {"refine": 0, "verify": 0, "seedverify": 0}
    monkeypatch.setattr(impact_mod, "_refinement_pass",
                        lambda **k: calls.__setitem__("refine", calls["refine"] + 1) or {"considered": 0, "rescored": 0, "applied": 0})
    monkeypatch.setattr(impact_mod, "_verification_pass",
                        lambda **k: calls.__setitem__("verify", calls["verify"] + 1) or {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0})
    monkeypatch.setattr(impact_mod, "_verify_seed_directness",
                        lambda *a, **k: calls.__setitem__("seedverify", calls["seedverify"] + 1) or True)
    events = list(impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn, max_hops=2, refine=False, verify=False))
    hops = [e["hop"] for e in events if e["event"] == "hop"]
    assert max(hops) <= 2                      # max_hops respected
    assert calls == {"refine": 0, "verify": 0, "seedverify": 0}   # all passes skipped
    done = next(e for e in events if e["event"] == "done")["result"]
    assert done["max_hops"] == 2


def test_default_config_runs_all_passes(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _fake_llm)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    seen = {"refine": 0, "verify": 0}
    orig_r, orig_v = impact_mod._refinement_pass, impact_mod._verification_pass
    monkeypatch.setattr(impact_mod, "_refinement_pass",
                        lambda **k: seen.__setitem__("refine", 1) or orig_r(**k))
    monkeypatch.setattr(impact_mod, "_verification_pass",
                        lambda **k: seen.__setitem__("verify", 1) or orig_v(**k))
    list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    assert seen == {"refine": 1, "verify": 1}   # defaults unchanged


# --- P6: seed_hint_id injection for batch precompute --------------------------

def _first_company(conn):
    row = conn.execute("SELECT id, name, type FROM nodes WHERE type='Company' LIMIT 1").fetchone()
    return row


def test_seed_hint_injected_at_hop0(conn, monkeypatch):
    # A hint node the engine's own extraction would NOT seed (fake extraction
    # returns no entities; commodity seed picks a different first candidate).
    hint = _first_company(conn)
    monkeypatch.setattr(impact_mod, "_score_seed_node",
                        lambda text, name, ntype: {"direction": "negative", "magnitude": 0.7, "reasoning": "hint"})
    events = list(impact_mod.run_impact_stream("some headline", conn=conn, seed_hint_id=hint["id"]))
    seeds_ev = next(e for e in events if e["event"] == "seeds")
    seed_ids = {v["node_id"] for v in seeds_ev["seeds"]}
    assert hint["id"] in seed_ids                                  # guaranteed seed present
    hint_seed = next(v for v in seeds_ev["seeds"] if v["node_id"] == hint["id"])
    assert hint_seed["hop"] == 0 and hint_seed["direction"] == "negative"


def test_seed_hint_none_is_unchanged(conn):
    # Backward-compat: passing no hint == today's behavior for the same input.
    base = {v["node_id"] for v in
            next(e for e in impact_mod.run_impact_stream("global crude oil supply shock", conn=conn)
                 if e["event"] == "seeds")["seeds"]}
    withn = {v["node_id"] for v in
             next(e for e in impact_mod.run_impact_stream("global crude oil supply shock", conn=conn, seed_hint_id=None)
                  if e["event"] == "seeds")["seeds"]}
    assert base == withn


def test_seed_hint_scoring_failopen(conn, monkeypatch):
    # If scoring returns None AND the engine finds no other seed, it must still
    # produce the normal "no seeds" result (not crash). Force no LLM seeds + no score.
    monkeypatch.setattr(impact_mod, "_extract_named_entities", lambda text: [])
    monkeypatch.setattr(impact_mod, "_llm_call", lambda prompt: "")   # commodity seed returns nothing
    monkeypatch.setattr(impact_mod, "_score_seed_node", lambda *a, **k: None)
    hint = _first_company(conn)
    result = impact_mod.run_impact("x headline", conn=conn, seed_hint_id=hint["id"])
    assert result.get("impacts") == [] and "seed" in result        # graceful, no exception


def test_seed_hint_already_seeded_no_duplicate(conn, monkeypatch):
    # If extraction already resolves the hint id, passing it must not duplicate it.
    hint = _first_company(conn)
    monkeypatch.setattr(impact_mod, "_extract_named_entities",
                        lambda text: [{"company_name": hint["name"], "direction": "negative",
                                       "magnitude": 0.8, "reasoning": "t"}])
    calls = {"n": 0}
    monkeypatch.setattr(impact_mod, "_score_seed_node",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or {"direction": "negative", "magnitude": 0.5, "reasoning": "t"})
    events = list(impact_mod.run_impact_stream("x", conn=conn, seed_hint_id=hint["id"]))
    seeds = next(e for e in events if e["event"] == "seeds")["seeds"]
    assert sum(1 for v in seeds if v["node_id"] == hint["id"]) == 1  # exactly one
    assert calls["n"] == 0                                          # hint already seeded → no scoring call


def test_score_seed_node_failopen_on_exception(monkeypatch):
    # If the scoring LLM call raises, _score_seed_node must return None (fail-open),
    # so the injection block skips the hint instead of aborting the whole trace.
    def boom(prompt):
        raise RuntimeError("provider down")
    monkeypatch.setattr(impact_mod, "_llm_call", boom)
    assert impact_mod._score_seed_node("some news", "Apple Inc.", "Company") is None
