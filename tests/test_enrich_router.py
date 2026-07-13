"""Tests for the enrichment router: enrich only AMBIGUOUS headlines (where the capsule
earns its tokens), ordered by seed REACH, capped by budget."""
from __future__ import annotations

import sqlite3

from schema import store
from pipeline import enrich

N1, N2, N3 = "cik:0000000001", "cik:0000000002", "cik:0000000003"


def _seed(tmp_path):
    conn = store.connect(tmp_path / "r.db"); store.init_db(conn)
    conn.row_factory = sqlite3.Row
    for nid in (N1, N2, N3):
        conn.execute("INSERT INTO nodes (id, type, name) VALUES (?,?,?)", (nid, "Company", nid))

    def edge(eid, s, t):
        conn.execute(
            "INSERT INTO edges (id, source, target, type, directed, confidence, "
            "prov_filing, prov_url, prov_snippet, prov_extracted_by) "
            "VALUES (?,?,?,?,1,0.9,'','','x','rule')", (eid, s, t, "supplies"))
    edge("e1", N1, N2); edge("e2", N1, N3); edge("e3", N2, N1)   # degree: N1=3, N2=2, N3=1
    conn.commit()

    def ev(eid, headline, seed):
        store.insert_event(conn, {"id": eid, "headline": headline, "url": f"http://x/{eid}",
                                  "seed_entity": seed, "seed_node_id": seed, "status": "queued"})
    ev("amb_hi", "Libya oil output hits a multi-year high amid steady regional demand", N1)  # ambiguous, high reach
    ev("amb_lo", "Shares drift sideways as investors weigh a murky outlook", N3)             # ambiguous, low reach
    ev("clear",  "Acme agrees to acquire Beta for $5 billion in cash", N1)                   # clear (material trigger)
    return conn


def test_router_keeps_ambiguous_drops_clear_orders_by_reach(tmp_path):
    conn = _seed(tmp_path)
    ids = [c["id"] for c in enrich._select_candidates(conn, 10, queued_only=True)]
    assert "clear" not in ids                       # a headline that names a hard event is skipped
    assert ids == ["amb_hi", "amb_lo"]              # ambiguous only, high-reach first


def test_router_budget_caps_to_highest_reach(tmp_path):
    conn = _seed(tmp_path)
    ids = [c["id"] for c in enrich._select_candidates(conn, 1, queued_only=True)]
    assert ids == ["amb_hi"]                         # budget=1 → the single highest-reach ambiguous event


def test_router_ambiguous_filter_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(enrich, "ENRICH_AMBIGUOUS_ONLY", False)
    ids = {c["id"] for c in enrich._select_candidates(_seed(tmp_path), 10, queued_only=True)}
    assert ids == {"amb_hi", "amb_lo", "clear"}      # filter off → everything eligible (for A/B)
