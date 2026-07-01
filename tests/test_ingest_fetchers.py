from __future__ import annotations
import json
from schema import store
from pipeline import ingest_news as ing


def _graph():
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:0000000001','Company','Acme','[\"ACME\"]')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('commodity:wheat','Commodity','Wheat')")
    conn.commit()
    return conn


def test_map_api_item_by_ticker():
    conn = _graph()
    idx = ing._ticker_index(conn)
    c = ing._candidate_from_ticker("ACME", "Acme wins contract", "Marketaux",
                                   "http://x/1", "company", "2026-06-17", idx)
    assert c and c["seed_node_id"] == "cik:0000000001"
    assert ing._candidate_from_ticker("ZZZ", "x", "Marketaux", "u", "company", None, idx) is None


def test_candidate_from_ticker_tolerates_null_headline():
    # News APIs can return {"title": null}; dict.get("title","") yields None, not "".
    conn = _graph()
    idx = ing._ticker_index(conn)
    c = ing._candidate_from_ticker("ACME", None, "Marketaux", "http://x/1", "company", "2026-06-17", idx)
    assert c is not None and c["headline"] == ""     # coerced, no TypeError crash


def test_8k_category_from_item_code():
    assert ing._category_for_8k("2.01") == "m&a"
    assert ing._category_for_8k("1.01") == "agreement"
    assert ing._category_for_8k("5.02") == "exec"
    assert ing._category_for_8k("8.01") == "filing"


def test_extract_rss_events_parses_claude_json(monkeypatch):
    conn = _graph()
    raw = [{"title": "Acme buys Wheat supplier", "source": "RSS-World", "url": "http://x/9", "pub_date": "2026-06-17"}]
    monkeypatch.setattr(ing, "_claude_call",
        lambda p: json.dumps([{"index": 1, "headline": "Acme acquires wheat supplier",
                               "entity": "Acme", "category": "m&a"}]))
    out = ing.extract_rss_events(raw)
    assert len(out) == 1 and out[0]["seed_entity"] == "Acme" and out[0]["category"] == "m&a"
    assert out[0]["url"] == "http://x/9"


def test_extract_rss_events_empty_when_claude_unavailable(monkeypatch):
    monkeypatch.setattr(ing, "_claude_call", lambda p: "")   # CLI failure → fail-open
    assert ing.extract_rss_events([{"title": "t", "source": "s", "url": "u", "pub_date": None}]) == []


def test_marketaux_skipped_without_key(monkeypatch):
    monkeypatch.delenv("MARKETAUX_KEY", raising=False)
    assert ing.fetch_marketaux(ing._ticker_index(_graph())) == []
