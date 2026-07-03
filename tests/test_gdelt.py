from __future__ import annotations

import time

import pytest
import requests

from pipeline import gdelt
from pipeline import ingest_news as ing
from schema import store


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    # Never actually sleep between calls in the test suite.
    monkeypatch.setenv("GDELT_MIN_INTERVAL_S", "0")


class _Resp:
    """Minimal requests.Response stand-in."""

    def __init__(self, payload=None, *, text="", raise_json=False, status=200):
        self._payload = payload
        self.text = text
        self._raise_json = raise_json
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if self._raise_json:
            raise ValueError("no json")
        return self._payload


# ── gdelt.py client ────────────────────────────────────────────────────────

def test_seendate_to_date():
    assert gdelt.seendate_to_date("20260703T160000Z") == "2026-07-03"
    assert gdelt.seendate_to_date("") is None
    assert gdelt.seendate_to_date(None) is None
    assert gdelt.seendate_to_date("garbage") is None


def test_gdelt_user_agent_precedence(monkeypatch):
    monkeypatch.delenv("GDELT_USER_AGENT", raising=False)
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    assert "EconGraph" in gdelt.gdelt_user_agent()          # static fallback
    monkeypatch.setenv("EDGAR_USER_AGENT", "Me me@x.com")
    assert gdelt.gdelt_user_agent() == "Me me@x.com"        # reuses EDGAR UA
    monkeypatch.setenv("GDELT_USER_AGENT", "Custom c@x.com")
    assert gdelt.gdelt_user_agent() == "Custom c@x.com"     # explicit wins


def test_gdelt_search_parses_artlist(monkeypatch):
    payload = {"articles": [
        {"url": "http://n/1", "title": "Acme wins contract", "domain": "n.com",
         "seendate": "20260703T120000Z", "language": "English", "sourcecountry": "US"},
        {"url": "http://n/2", "title": "Acme opens plant", "domain": "m.com",
         "seendate": "20260703T130000Z", "language": "English", "sourcecountry": "US"},
    ]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    out = gdelt.gdelt_search("Acme Corp")
    assert [a["url"] for a in out] == ["http://n/1", "http://n/2"]
    assert out[0]["title"] == "Acme wins contract"
    assert out[0]["published_at"] == "2026-07-03"           # derived from seendate


def test_gdelt_search_builds_query_and_sends_ua(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers, timeout=timeout)
        return _Resp({"articles": []})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.delenv("GDELT_USER_AGENT", raising=False)   # else ambient env flips the result
    monkeypatch.setenv("EDGAR_USER_AGENT", "Me me@x.com")
    gdelt.gdelt_search("Acme Corp", timespan="2h", maxrecords=10)
    assert captured["url"] == gdelt.GDELT_DOC_URL
    p = captured["params"]
    assert p["query"] == '"Acme Corp" sourcelang:english'
    assert p["mode"] == "artlist" and p["format"] == "json" and p["sort"] == "datedesc"
    assert p["maxrecords"] == "10" and p["timespan"] == "2h"
    assert captured["headers"]["User-Agent"] == "Me me@x.com"


def test_gdelt_search_maxrecords_clamped(monkeypatch):
    seen = {}
    monkeypatch.setattr(requests, "get",
                        lambda url, params=None, **k: seen.update(params=params) or _Resp({"articles": []}))
    gdelt.gdelt_search("Acme", maxrecords=999)
    assert seen["params"]["maxrecords"] == "250"            # clamped to API max
    gdelt.gdelt_search("Acme", maxrecords=0)
    assert seen["params"]["maxrecords"] == "1"              # clamped to min 1


def test_gdelt_search_empty_entity_makes_no_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not hit the network for an empty entity")
    monkeypatch.setattr(requests, "get", boom)
    assert gdelt.gdelt_search("") == []
    assert gdelt.gdelt_search("   ") == []


def test_gdelt_search_non_json_returns_empty(monkeypatch):
    # GDELT returns HTML / empty body with a 200 when a query has no results.
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(text="<html/>", raise_json=True))
    assert gdelt.gdelt_search("Acme") == []


def test_gdelt_search_tolerates_missing_or_nondict_payload(monkeypatch):
    # A 200 whose JSON is a dict without "articles", or is not a dict at all,
    # must yield [] rather than crash.
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"status": "no results"}))
    assert gdelt.gdelt_search("Acme") == []
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(["not", "a", "dict"]))
    assert gdelt.gdelt_search("Acme") == []


def test_gdelt_search_skips_articles_missing_url_or_title(monkeypatch):
    payload = {"articles": [
        {"url": "", "title": "no url"},
        {"url": "http://n/2", "title": ""},
        {"url": "http://n/3", "title": "keep me", "seendate": "20260703T120000Z"},
        "not-a-dict",
    ]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    out = gdelt.gdelt_search("Acme")
    assert [a["url"] for a in out] == ["http://n/3"]


def test_gdelt_search_http_error_propagates(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(status=503))
    with pytest.raises(requests.HTTPError):
        gdelt.gdelt_search("Acme")


def test_gdelt_search_retries_once_on_429(monkeypatch):
    # The retry must ALSO pay the throttle + backoff — politeness on the exact
    # request that already got a 429 (invariant #7). Spy on both so a regression
    # that hoists acquire() out of the loop or drops the backoff sleep is caught.
    monkeypatch.setenv("GDELT_BACKOFF_S", "7")
    slept, acquired = [], {"n": 0}
    monkeypatch.setattr(gdelt.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(gdelt._LIMITER, "acquire", lambda: acquired.__setitem__("n", acquired["n"] + 1))
    calls = {"n": 0}

    def seq_get(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(status=429)             # first hit: throttled
        return _Resp({"articles": [{"url": "http://n/1", "title": "ok"}]})

    monkeypatch.setattr(requests, "get", seq_get)
    out = gdelt.gdelt_search("Acme")
    assert calls["n"] == 2 and [a["url"] for a in out] == ["http://n/1"]   # retry succeeded
    assert acquired["n"] == 2                     # throttle paid on BOTH attempts
    assert 7 in slept                             # backoff slept before the retry


def test_gdelt_search_persistent_429_raises(monkeypatch):
    monkeypatch.setenv("GDELT_BACKOFF_S", "0")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(status=429))
    with pytest.raises(requests.HTTPError):
        gdelt.gdelt_search("Acme")            # still 429 after the one retry → propagate


def test_throttle_sleeps_when_interval_positive(monkeypatch):
    slept = {"t": 0.0}
    monkeypatch.setenv("GDELT_MIN_INTERVAL_S", "5")
    monkeypatch.setattr(gdelt.time, "monotonic", lambda: 100.0)   # no time has passed
    monkeypatch.setattr(gdelt.time, "sleep", lambda s: slept.__setitem__("t", s))
    t = gdelt._Throttle()
    t._last = 100.0                                              # a call just happened
    t.acquire()
    assert slept["t"] == pytest.approx(5.0)                       # waited the full interval


# ── ingest_news.fetch_gdelt ────────────────────────────────────────────────

def _graph():
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name,tickers) VALUES ('cik:1','Company','Acme Corp','[\"ACME\"]')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('commodity:wheat','Commodity','Wheat')")
    conn.commit()
    return conn


def _article(name):
    return {"url": f"http://n/{name}", "title": f"{name} news", "domain": "d",
            "seendate": "20260703T120000Z", "language": "English",
            "sourcecountry": "US", "published_at": "2026-07-03"}


def test_fetch_gdelt_seeds_known_node_and_category_by_type(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0, "commodity:wheat": 0.9})
    monkeypatch.setattr("pipeline.gdelt.gdelt_search", lambda name, **kw: [_article(name)])
    out = ing.fetch_gdelt(conn)
    by_seed = {c["seed_node_id"]: c for c in out}
    assert set(by_seed) == {"cik:1", "commodity:wheat"}
    co = by_seed["cik:1"]
    assert co["source"] == "GDELT" and co["category"] == "company"
    assert co["seed_entity"] == "Acme Corp" and co["headline"] == "Acme Corp news"
    assert co["published_at"] == "2026-07-03" and co["id"].startswith("ev:")
    assert by_seed["commodity:wheat"]["category"] == "commodity"   # by node type


def test_fetch_gdelt_respects_top_nodes_cap(monkeypatch):
    conn = store.connect(":memory:")
    store.init_db(conn)
    for i in (1, 2, 3):
        conn.execute(f"INSERT INTO nodes (id,type,name) VALUES ('cik:{i}','Company','Co{i}')")
    conn.commit()
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0, "cik:2": 0.5, "cik:3": 0.1})
    monkeypatch.setattr(ing, "GDELT_TOP_NODES", 2)
    queried = []
    monkeypatch.setattr("pipeline.gdelt.gdelt_search",
                        lambda name, **kw: queried.append(name) or [])
    ing.fetch_gdelt(conn)
    assert queried == ["Co1", "Co2"]                     # top-2 by centrality only


def test_fetch_gdelt_disabled_by_env(monkeypatch):
    conn = _graph()
    monkeypatch.setenv("INGEST_GDELT", "0")
    monkeypatch.setattr("pipeline.gdelt.gdelt_search",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not query")))
    assert ing.fetch_gdelt(conn) == []


def test_fetch_gdelt_isolates_a_failing_query(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0, "commodity:wheat": 0.9})

    def flaky(name, **kw):
        if name == "Acme Corp":
            raise requests.HTTPError("429 rate limited")
        return [_article(name)]

    monkeypatch.setattr("pipeline.gdelt.gdelt_search", flaky)
    out = ing.fetch_gdelt(conn)
    assert [c["seed_node_id"] for c in out] == ["commodity:wheat"]   # one failed, other survived


def test_fetch_gdelt_deadline_stops_before_any_query(monkeypatch):
    conn = _graph()
    queried = []
    # A recording stub (NOT one that raises — raises get swallowed by the
    # per-node except, hiding a dropped `break`). If the deadline break is
    # removed, this stub records a call and the assertion fails.
    monkeypatch.setattr("pipeline.gdelt.gdelt_search", lambda name, **kw: queried.append(name) or [])
    out = ing.fetch_gdelt(conn, deadline=time.monotonic() - 1)
    assert out == [] and queried == []               # stopped before ANY query


def test_fetch_gdelt_deadline_stops_mid_batch(monkeypatch):
    # 3 top nodes, clock trips the deadline after the 2nd loop check → exactly
    # the first two are queried and their partial results returned (no raise).
    conn = store.connect(":memory:")
    store.init_db(conn)
    for i in (1, 2, 3):
        conn.execute(f"INSERT INTO nodes (id,type,name) VALUES ('cik:{i}','Company','Co{i}')")
    conn.commit()
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0, "cik:2": 0.9, "cik:3": 0.8})
    queried = []
    monkeypatch.setattr("pipeline.gdelt.gdelt_search",
                        lambda name, **kw: queried.append(name) or [_article(name)])
    mono = {"n": 0}
    # 1st & 2nd loop-top checks: before deadline; 3rd: past it → break.
    monkeypatch.setattr(ing.time, "monotonic",
                        lambda: mono.__setitem__("n", mono["n"] + 1) or (0.0 if mono["n"] <= 2 else 2000.0))
    out = ing.fetch_gdelt(conn, deadline=1000.0)
    assert queried == ["Co1", "Co2"]                 # 3rd skipped by the deadline
    assert [c["seed_node_id"] for c in out] == ["cik:1", "cik:2"]   # partial results kept


def test_fetch_gdelt_uses_real_hub_centrality_field(monkeypatch, tmp_path):
    # Regression guard for the hubs.jsonl field name. identify_hubs.py writes
    # "centrality", so _centrality must read that. This test does NOT stub
    # _centrality — it points HUBS_PATH at a real-shaped fixture and asserts the
    # HIGHEST-centrality node is the one queried. (Fails if _centrality reads the
    # wrong key and degrades to the degree fallback → lowest-id node instead.)
    import json as _json
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:1','Company','LowCentral')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:2','Company','HighCentral')")
    conn.commit()
    hubs = tmp_path / "hubs.jsonl"
    hubs.write_text(
        _json.dumps({"id": "cik:1", "name": "LowCentral", "centrality": 0.001}) + "\n"
        + _json.dumps({"id": "cik:2", "name": "HighCentral", "centrality": 0.999}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(ing, "HUBS_PATH", hubs)
    monkeypatch.setattr(ing, "GDELT_TOP_NODES", 1)
    queried = []
    monkeypatch.setattr("pipeline.gdelt.gdelt_search", lambda name, **kw: queried.append(name) or [])
    ing.fetch_gdelt(conn)
    assert queried == ["HighCentral"]                # top by real "centrality" field


def test_fetch_gdelt_category_by_node_type(monkeypatch):
    conn = store.connect(":memory:")
    store.init_db(conn)
    for nid, ntype, name in [("cik:1", "Company", "C"), ("commodity:o", "Commodity", "Oil"),
                             ("material:s", "Material", "Steel"), ("region:us", "Region", "US"),
                             ("regulator:fda", "Regulator", "FDA")]:
        conn.execute("INSERT INTO nodes (id,type,name) VALUES (?,?,?)", (nid, ntype, name))
    conn.commit()
    monkeypatch.setattr(ing, "_centrality", lambda c: {})         # all tie → all queried
    monkeypatch.setattr("pipeline.gdelt.gdelt_search", lambda name, **kw: [_article(name)])
    cat = {c["seed_node_id"]: c["category"] for c in ing.fetch_gdelt(conn)}
    assert cat == {"cik:1": "company", "commodity:o": "commodity", "material:s": "commodity",
                   "region:us": "macro", "regulator:fda": "politics"}
