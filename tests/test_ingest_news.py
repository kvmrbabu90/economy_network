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


def test_resolve_to_node_id_prefix_tiebreak_is_deterministic():
    # Two nodes share the prefix "Acme"; the shortest name (then lexical id)
    # must win the LIKE-prefix fallback, reproducibly across rebuilds.
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:200','Company','Acme Holdings International')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:100','Company','Acme Labs')")
    conn.commit()
    # "acme" prefixes both; ORDER BY length(name), id → "Acme Labs" (shorter).
    assert ing._resolve_to_node_id(conn, "acme") == "cik:100"


def test_resolve_to_node_id_contains_tiebreak_is_deterministic():
    # Contains-fallback (>=5 chars) must also be deterministic.
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:2','Company','Global Widget Corporation')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:1','Company','Widget Co')")
    conn.commit()
    # "widget" is contained in both; shortest name wins.
    assert ing._resolve_to_node_id(conn, "widget") == "cik:1"


def test_resolve_to_node_id_escapes_like_wildcards():
    # A query term containing % or _ must match literally, not as a wildcard.
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:1','Company','AT&T Inc')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:2','Company','50% Off Corp')")
    conn.commit()
    # Bare "%" would match everything if unescaped; escaped it only hits the
    # node whose name literally contains "50%".
    assert ing._resolve_to_node_id(conn, "50%") == "cik:2"
    # An underscore term must not act as a single-char wildcard.
    assert ing._resolve_to_node_id(conn, "a_t") is None


def test_resolve_to_node_id_contains_gated_on_min_length():
    conn = _graph()
    # "cor" (len 3) is < 5 and must NOT trigger the contains-fallback into "Acme Corp".
    assert ing._resolve_to_node_id(conn, "cor") is None


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
    # The url-hash-id dedupe path keys on c["id"] only; give these no seed so the
    # cross-feed collapse (which needs a seed) can't interfere.
    out = ing.dedupe([{"id": "seen"}, {"id": "fresh"}, {"id": "fresh"}], conn)
    assert [c["id"] for c in out] == ["fresh"]   # prior-cycle 'seen' dropped; in-cycle dup collapsed


def test_dedupe_collapses_same_story_across_feeds():
    # The SAME story arriving via 8-K + Marketaux + AV + RSS has 4 distinct urls
    # (so 4 distinct _event_ids) but must collapse to ONE queued event via the
    # (seed, publish-date, first-6-headline-words) key. First-wins (8-K here).
    conn = _graph()
    def mk(source, url, headline):
        c = {"headline": headline, "source": source, "url": url, "category": "m&a",
             "published_at": "2026-07-01", "seed_entity": "Acme", "seed_node_id": "cik:1"}
        c["id"] = ing._event_id(c)
        return c
    # First 6 normalized words are identical across feeds; trailing text and
    # punctuation differ (as real syndication does). Punctuation is stripped by
    # the collapse-key normalizer, so "deal," and "deal!" fold together.
    cands = [
        mk("SEC 8-K", "https://sec.gov/a", "Acme acquires Beta in cash deal"),
        mk("Marketaux", "https://marketaux/x", "Acme acquires Beta in cash deal today"),
        mk("Alpha Vantage", "https://av/y", "Acme acquires Beta in cash deal!"),
        mk("RSS-World", "https://rss/z", "Acme acquires Beta in cash deal, sources say"),
    ]
    # Sanity: all four have distinct primary ids (url-based).
    assert len({c["id"] for c in cands}) == 4
    out = ing.dedupe(cands, conn)
    assert len(out) == 1 and out[0]["source"] == "SEC 8-K"   # collapsed to one; 8-K wins


def test_dedupe_keeps_distinct_stories_same_seed_and_date():
    # Different first-6 headline words → different collapse key → both kept,
    # even with the same seed + date. Guards against over-collapsing.
    conn = _graph()
    def mk(url, headline):
        c = {"headline": headline, "source": "RSS", "url": url, "category": "m&a",
             "published_at": "2026-07-01", "seed_entity": "Acme", "seed_node_id": "cik:1"}
        c["id"] = ing._event_id(c)
        return c
    out = ing.dedupe([mk("u1", "Acme wins defense contract worth billions"),
                      mk("u2", "Acme recalls faulty product line nationwide")], conn)
    assert len(out) == 2


def test_dedupe_no_seed_falls_back_to_id_only():
    # Candidates without a seed can't form a collapse key; they must survive on
    # url-hash id alone (only the exact-id dup collapses).
    conn = _graph()
    a = {"id": "n1", "headline": "same words here now again", "seed_node_id": None, "published_at": "2026-07-01"}
    b = {"id": "n2", "headline": "same words here now again", "seed_node_id": None, "published_at": "2026-07-01"}
    out = ing.dedupe([a, b, {"id": "n1"}], conn)
    assert [c["id"] for c in out] == ["n1", "n2"]


def test_run_ingest_isolates_a_failing_fetcher(monkeypatch, tmp_path):
    # One fetcher raising must NOT discard the others' candidates (esp. 8-K).
    db = tmp_path / "iso.db"
    conn = store.connect(db); store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:1','Company','Acme','[]')")
    conn.commit(); conn.close()

    def good_8k(c, **kw):
        return [{"id": "k1", "headline": "Acme 8-K", "source": "SEC 8-K", "url": "u8k",
                 "category": "m&a", "published_at": "2026-07-01",
                 "seed_entity": "cik:1", "seed_node_id": "cik:1"}]

    def boom(*a, **k):
        raise RuntimeError("marketaux is flaky right now")

    monkeypatch.setattr(ing, "fetch_8k", good_8k)
    monkeypatch.setattr(ing, "fetch_marketaux", boom)         # this one blows up
    monkeypatch.setattr(ing, "fetch_alphavantage", lambda idx: [])
    monkeypatch.setattr(ing, "fetch_rss_broad", lambda: [])
    monkeypatch.setenv("INGEST_MATERIALITY_GATE", "0")
    s = ing.run_ingest(db)
    assert s["queued"] == 1                                   # 8-K survived the marketaux failure
    conn = store.connect(db)
    assert store.queued_events(conn)[0]["seed_node_id"] == "cik:1"


def test_run_ingest_end_to_end(monkeypatch, tmp_path):
    import pipeline.ingest_news as ing
    db = tmp_path / "g.db"
    conn = store.connect(db); store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:1','Company','Acme','[]')")
    conn.commit(); conn.close()
    monkeypatch.setattr(ing, "fetch_8k", lambda c, **kw: [
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


def test_over_cap_events_not_persisted_and_reeligible(monkeypatch, tmp_path):
    # Over-cap material events must NOT be recorded as permanently-'skipped'
    # (which event_exists would then skip forever). They are left unpersisted so a
    # later cycle can queue them once there's room — deferred, not dropped.
    import pipeline.ingest_news as ing
    db = tmp_path / "cap.db"
    conn = store.connect(db); store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:1','Company','Acme','[]')")
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:2','Company','Beta','[]')")
    conn.commit(); conn.close()

    def two_8k(c, **kw):
        return [{"id": "ev1", "headline": "Acme deal", "source": "SEC 8-K", "url": "u1",
                 "category": "m&a", "published_at": "2026-07-02", "seed_entity": "cik:1", "seed_node_id": "cik:1"},
                {"id": "ev2", "headline": "Beta deal", "source": "SEC 8-K", "url": "u2",
                 "category": "m&a", "published_at": "2026-07-02", "seed_entity": "cik:2", "seed_node_id": "cik:2"}]
    monkeypatch.setattr(ing, "fetch_8k", two_8k)
    monkeypatch.setattr(ing, "fetch_marketaux", lambda idx: [])
    monkeypatch.setattr(ing, "fetch_alphavantage", lambda idx: [])
    monkeypatch.setattr(ing, "fetch_rss_broad", lambda: [])
    monkeypatch.setenv("INGEST_MATERIALITY_GATE", "0")
    # Force a cap of 1: first ranked → queued, the rest over-cap → skipped in-memory.
    monkeypatch.setattr(ing, "cap",
                        lambda ranked, **k: [dict(c, status=("queued" if i == 0 else "skipped"))
                                             for i, c in enumerate(ranked)])

    # Cycle 1: two material candidates, cap 1 → 1 queued, 1 deferred (NOT written).
    s1 = ing.run_ingest(db)
    assert s1["queued"] == 1 and s1["deferred"] == 1
    conn = store.connect(db)
    rows = conn.execute("SELECT id, status FROM events ORDER BY id").fetchall()
    conn.close()
    assert [r["id"] for r in rows] == ["ev1"]        # over-cap 'ev2' NOT persisted (no 'skipped' row)
    assert rows[0]["status"] == "queued"

    # Cycle 2: both re-fetched. 'ev1' is skipped (already stored); the previously
    # deferred 'ev2' is now fresh → gets queued. Proves it wasn't dropped forever.
    ing.run_ingest(db)
    conn = store.connect(db)
    ids = sorted(r["id"] for r in conn.execute("SELECT id FROM events").fetchall())
    conn.close()
    assert ids == ["ev1", "ev2"]                     # the deferred event got its shot next cycle
