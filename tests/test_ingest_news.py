from __future__ import annotations
from schema import store
from pipeline import ingest_news as ing


def _graph():
    conn = store.connect(":memory:")
    store.init_db(conn)
    # Two company nodes (one a hub with many edges), one commodity.
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:1','Company','Acme Corp','[\"ACME\"]')")
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:2','Company','Beta Inc','[\"BETA\"]')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('commodity:wheat','Commodity','Wheat')")
    # Acme is a hub: 3 edges; Beta: 0.
    # NOTE: the graph schema has a UNIQUE index on edges(source,target,type)
    # (uq_edges_triple), so the three hub edges must be distinct triples. We give
    # Acme two supplies edges (Beta, wheat) plus a competes_with (Beta) — 3 edges
    # total, Acme degree 3 vs Beta degree < Acme, preserving the hub/leaf ranking.
    for i,(t,ty) in enumerate([('cik:2','supplies'),('commodity:wheat','supplies'),('cik:2','competes_with')]):
        conn.execute("INSERT INTO edges (id,source,target,type,confidence,prov_filing,prov_url,prov_snippet,prov_extracted_by)"
                     f" VALUES ('x{i}','cik:1','{t}','{ty}',0.9,'','','s','rule')")
    conn.commit()
    return conn


def test_resolve_to_node_id_all_types():
    conn = _graph()
    assert ing._resolve_to_node_id(conn, "Wheat") == "commodity:wheat"      # commodity by name
    assert ing._resolve_to_node_id(conn, "Acme Corp") == "cik:1"            # company by name
    assert ing._resolve_to_node_id(conn, "Nope") is None


def test_ticker_index_maps_symbol_to_node():
    conn = _graph()
    idx = ing._ticker_index(conn)
    assert idx["ACME"] == "cik:1" and idx["BETA"] == "cik:2"


def test_event_id_is_stable_and_url_based():
    a = ing._event_id({"url": "http://x/1", "source": "s", "headline": "h"})
    b = ing._event_id({"url": "http://x/1", "source": "other", "headline": "diff"})
    assert a == b   # same url → same id regardless of other fields


def test_rank_and_cap_selects_top_by_priority():
    conn = _graph()
    cands = [
        {"id": "hub",  "seed_node_id": "cik:1", "source": "SEC 8-K", "published_at": "2026-06-17"},  # hub + best source
        {"id": "leaf", "seed_node_id": "cik:2", "source": "RSS",     "published_at": "2026-06-17"},  # no edges + weak source
    ]
    ranked = ing.rank(cands, conn, today="2026-06-17")
    assert ranked[0]["id"] == "hub"   # higher centrality + source weight ranks first
    top = ing.cap(ranked, cap=1)
    assert [c["id"] for c in top if c["status"] == "queued"] == ["hub"]
    assert [c["id"] for c in top if c["status"] == "skipped"] == ["leaf"]


def test_dedupe_drops_ids_already_in_db():
    conn = _graph()
    store.insert_event(conn, {"id": "seen", "headline": "h", "source": "s", "url": "u",
                              "category": "c", "published_at": None, "seed_entity": "E",
                              "seed_node_id": "cik:1", "status": "traced"})
    out = ing.dedupe([{"id": "seen"}, {"id": "fresh"}, {"id": "fresh"}], conn)
    assert [c["id"] for c in out] == ["fresh"]   # prior-cycle 'seen' dropped; in-cycle dup collapsed


def test_run_ingest_end_to_end(monkeypatch, tmp_path):
    import pipeline.ingest_news as ing
    db = tmp_path / "g.db"
    conn = store.connect(db); store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:1','Company','Acme','[]')")
    conn.commit(); conn.close()
    monkeypatch.setattr(ing, "fetch_8k", lambda c: [
        {"id": "a", "headline": "h", "source": "SEC 8-K", "url": "u1", "category": "m&a",
         "published_at": "2026-06-17", "seed_entity": "cik:1", "seed_node_id": "cik:1"}])
    monkeypatch.setattr(ing, "fetch_marketaux", lambda idx: [])
    monkeypatch.setattr(ing, "fetch_alphavantage", lambda idx: [])
    monkeypatch.setattr(ing, "fetch_rss_broad", lambda: [])
    monkeypatch.setenv("INGEST_MATERIALITY_GATE", "0")   # keep this test hermetic (no LLM call)
    s = ing.run_ingest(db)
    assert s["queued"] == 1
    assert s["material"] == 1
    conn = store.connect(db)
    assert store.queued_events(conn)[0]["seed_node_id"] == "cik:1"
