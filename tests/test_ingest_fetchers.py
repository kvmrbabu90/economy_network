from __future__ import annotations
import json
import time
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


# ── Fix 2: safe-float + null-tolerant parsing ──────────────────────────────

def test_safe_float_never_raises():
    # Non-numeric / None / bad types all fall back to 0.0 instead of raising.
    assert ing._safe_float("N/A") == 0.0
    assert ing._safe_float(None) == 0.0
    assert ing._safe_float("") == 0.0
    assert ing._safe_float({"x": 1}) == 0.0
    assert ing._safe_float("0.42") == 0.42
    assert ing._safe_float(-1.5) == -1.5


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_marketaux_tolerates_null_entities(monkeypatch):
    # A Marketaux article with "entities": null must not raise (dict.get would
    # return None and `for ent in None` would TypeError without the `or []`).
    conn = _graph()
    idx = ing._ticker_index(conn)
    payload = {"data": [
        {"title": "t1", "url": "u1", "published_at": "2026-07-01", "entities": None},
        {"title": "Acme wins", "url": "u2", "published_at": "2026-07-01",
         "entities": [{"symbol": "ACME"}]},
    ]}
    monkeypatch.setenv("MARKETAUX_KEY", "k")
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(payload))
    out = ing.fetch_marketaux(idx)
    assert [c["seed_node_id"] for c in out] == ["cik:0000000001"]   # null-entities row skipped, not crashed


# ── Fix 5: fetch_8k per-CIK cap + wall-clock budget ────────────────────────

def _graph_with_ciks(n):
    conn = store.connect(":memory:")
    store.init_db(conn)
    for i in range(n):
        conn.execute(f"INSERT INTO nodes (id,type,name) VALUES ('cik:{i:010d}','Company','Filer{i}')")
    conn.commit()
    return conn


def test_fetch_8k_caps_per_cik(monkeypatch):
    # A filer with many recent 8-Ks must contribute at most INGEST_8K_PER_CIK.
    conn = _graph_with_ciks(1)
    from pipeline import sec_8k
    many = [{"url": f"http://sec/{j}", "filing_date": "2026-07-01", "items": "1.01"} for j in range(20)]
    monkeypatch.setattr(sec_8k, "fetch_recent_8k_meta", lambda cik, **kw: many)
    monkeypatch.setattr(ing, "INGEST_8K_PER_CIK", 3)
    out = ing.fetch_8k(conn)
    assert len(out) == 3    # capped, not 20


def test_fetch_8k_marks_no_collapse(monkeypatch):
    # 8-K synthetic headlines ("<cik> 8-K item X") must opt out of story/collapse
    # dedup — distinct filings of the same item from one filer share the headline.
    conn = _graph_with_ciks(1)
    from pipeline import sec_8k
    monkeypatch.setattr(sec_8k, "fetch_recent_8k_meta",
                        lambda cik, **kw: [{"url": "http://sec/1", "filing_date": "2026-07-01", "items": "8.01"}])
    out = ing.fetch_8k(conn)
    assert out and all(c.get("_no_collapse") is True for c in out)


def test_fetch_8k_wallclock_stops_new_ciks(monkeypatch):
    # Once the wall-clock deadline is spent, no NEW CIK is crawled.
    conn = _graph_with_ciks(5)
    from pipeline import sec_8k
    calls = {"n": 0}
    def meta(cik, **kw):
        calls["n"] += 1
        return [{"url": f"http://sec/{cik}", "filing_date": "2026-07-01", "items": "8.01"}]
    monkeypatch.setattr(sec_8k, "fetch_recent_8k_meta", meta)
    # A deadline already in the past → loop breaks before the first CIK.
    out = ing.fetch_8k(conn, deadline=time.monotonic() - 1)
    assert out == [] and calls["n"] == 0


def test_fetch_8k_isolates_one_failing_cik(monkeypatch):
    # A per-CIK exception must not abort the whole crawl (existing behavior).
    conn = _graph_with_ciks(2)
    from pipeline import sec_8k
    def meta(cik, **kw):
        if cik.endswith("0000000000"):
            raise RuntimeError("SEC 500")
        return [{"url": "http://sec/ok", "filing_date": "2026-07-01", "items": "1.01"}]
    monkeypatch.setattr(sec_8k, "fetch_recent_8k_meta", meta)
    out = ing.fetch_8k(conn)
    assert len(out) == 1    # first CIK failed, second still yielded


def test_alphavantage_tolerates_null_and_nonnumeric_score(monkeypatch):
    # ticker_sentiment: null must not raise; a non-numeric score "N/A" must not
    # blow up the sort key (safe-float → 0.0).
    conn = _graph()
    idx = ing._ticker_index(conn)
    payload = {"feed": [
        {"title": "t1", "url": "u1", "time_published": "20260701T120000",
         "ticker_sentiment": None},
        {"title": "Acme moves", "url": "u2", "time_published": "20260701T120000",
         "ticker_sentiment": [
             {"ticker": "ACME", "ticker_sentiment_score": "N/A"},
             {"ticker": "ACME", "ticker_sentiment_score": "0.9"},
         ]},
    ]}
    monkeypatch.setenv("ALPHAVANTAGE_KEY", "k")
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(payload))
    out = ing.fetch_alphavantage(idx)
    assert [c["seed_node_id"] for c in out] == ["cik:0000000001"]   # null row skipped; sort survived "N/A"
    assert out[0]["published_at"] == "2026-07-01"


def _cand(i, headline, entity="Acme"):
    return {"headline": headline, "seed_entity": entity, "seed_node_id": f"cik:{i}",
            "source": "SEC 8-K", "url": f"u/{i}", "category": "m&a", "published_at": "2026-07-01", "id": f"e{i}"}


def test_materiality_gate_keeps_only_material(monkeypatch):
    cands = [_cand(1, "Acme acquires rival for $5B"), _cand(2, "Why Acme stock could rise"),
             _cand(3, "Acme wins $2B defense contract")]
    # LLM marks 1 and 3 material, 2 not.
    monkeypatch.setattr(ing, "_claude_call", lambda p: json.dumps([
        {"index": 1, "material": True}, {"index": 2, "material": False}, {"index": 3, "material": True}]))
    out = ing._materiality_filter(cands)
    assert [c["id"] for c in out] == ["e1", "e3"]          # order preserved, non-material dropped


def test_materiality_gate_failopen(monkeypatch):
    cands = [_cand(1, "x"), _cand(2, "y")]
    monkeypatch.setattr(ing, "_claude_call", lambda p: "")   # garbage/empty → keep all
    assert ing._materiality_filter(cands) == cands


def test_materiality_gate_toggle_off(monkeypatch):
    cands = [_cand(1, "x")]
    def boom(p):
        raise AssertionError("gate must not call the LLM when disabled")
    monkeypatch.setattr(ing, "_claude_call", boom)
    monkeypatch.setenv("INGEST_MATERIALITY_GATE", "0")
    assert ing._materiality_filter(cands) == cands


def test_materiality_gate_empty(monkeypatch):
    def boom(p):
        raise AssertionError("no call on empty input")
    monkeypatch.setattr(ing, "_claude_call", boom)
    assert ing._materiality_filter([]) == []


def test_materiality_gate_valid_all_nonmaterial_keeps_none(monkeypatch):
    # A well-formed verdict list judging everything non-material must keep NOTHING
    # (a quiet hour traces no noise), not fall back to keeping all.
    cands = [_cand(1, "opinion piece"), _cand(2, "stock rises 2%")]
    monkeypatch.setattr(ing, "_claude_call", lambda p: json.dumps(
        [{"index": 1, "material": False}, {"index": 2, "material": False}]))
    assert ing._materiality_filter(cands) == []


def test_materiality_gate_structurally_junk_failopen(monkeypatch):
    # A list with no well-formed verdicts (no usable index/material) is indistinguishable
    # from garbage → fail open (keep all) rather than zero the cycle.
    cands = [_cand(1, "Acme buys rival"), _cand(2, "y")]
    monkeypatch.setattr(ing, "_claude_call", lambda p: json.dumps([{"foo": "bar"}, "nonsense"]))
    assert ing._materiality_filter(cands) == cands


def test_materiality_gate_chunks_large_batches(monkeypatch):
    # >INGEST_MATERIALITY_BATCH candidates → multiple bounded _claude_call chunks
    # (guards the Windows argv-overflow → fail-open bug). Each chunk is numbered from
    # 1, so a per-chunk verdict for index 1 keeps exactly one candidate per chunk.
    monkeypatch.setattr(ing, "INGEST_MATERIALITY_BATCH", 2)
    cands = [_cand(i, f"headline {i}") for i in range(1, 6)]   # 5 cands, batch 2 → 3 chunks
    calls = {"n": 0}

    def fake(prompt):
        calls["n"] += 1
        return json.dumps([{"index": 1, "material": True}]
                          + [{"index": j, "material": False} for j in range(2, 20)])

    monkeypatch.setattr(ing, "_claude_call", fake)
    out = ing._materiality_filter(cands)
    assert calls["n"] == 3 and len(out) == 3     # 3 chunks, one kept per chunk, no overflow


def test_fetch_8k_sets_seed_ids_and_autokeep(monkeypatch):
    conn = _graph_with_ciks(1)
    from pipeline import sec_8k
    monkeypatch.setattr(sec_8k, "fetch_recent_8k_meta",
        lambda cik, **kw: [{"url": "http://sec/1", "filing_date": "2026-07-01", "items": "8.01"}])
    out = ing.fetch_8k(conn)
    import json
    assert out and json.loads(out[0]["seed_ids"]) == ["cik:0000000000"]
    assert out[0]["_prior"] == ing._MATERIALITY_AUTOKEEP


# ── Rule-based materiality prefilter (Cut B) ────────────────────────────────

def test_materiality_prefilter_autokeep_autodrop_and_judge(monkeypatch):
    monkeypatch.setattr(ing, "INGEST_MATERIALITY_KEEP", 5.0)
    monkeypatch.setattr(ing, "INGEST_MATERIALITY_DROP", 1.5)
    cands = [
        {"id": "hi", "_prior": 8.0, "headline": "h", "seed_entity": "A"},     # auto-keep
        {"id": "lo", "_prior": 0.5, "headline": "h", "seed_entity": "B"},     # auto-drop
        {"id": "mid", "_prior": 3.0, "headline": "h", "seed_entity": "C"},    # judge
        {"id": "rss", "headline": "h", "seed_entity": "D"},                   # no prior → judge
        {"id": "8k", "_prior": ing._MATERIALITY_AUTOKEEP, "headline": "h", "seed_entity": "E"},  # keep
    ]
    monkeypatch.setattr(ing, "_materiality_filter",
                        lambda judge: [c for c in judge if c["id"] == "mid"])
    out = ing._materiality_prefilter(cands)
    assert {c["id"] for c in out} == {"hi", "8k", "mid"}       # auto-keeps + judged-keep; lo/rss gone


def test_materiality_prefilter_rules_off_defers_all(monkeypatch):
    monkeypatch.setenv("INGEST_MATERIALITY_RULES", "0")
    seen = {}
    monkeypatch.setattr(ing, "_materiality_filter", lambda c: (seen.update(n=len(c)), c)[1])
    ing._materiality_prefilter([{"id": "x", "_prior": 8.0, "headline": "h", "seed_entity": "A"}])
    assert seen["n"] == 1               # rules off → everything goes to the LLM gate
