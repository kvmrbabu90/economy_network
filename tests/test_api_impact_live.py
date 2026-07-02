import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from api import main as api_main
from schema import store


def _seed(tmp_path):
    db = tmp_path / "g.db"
    conn = store.connect(db); store.init_db(conn)
    # two nodes
    conn.execute("INSERT INTO nodes (id, type, name) VALUES ('cik:1','Company','Apple')")
    conn.execute("INSERT INTO nodes (id, type, name) VALUES ('slug:oil','Commodity','Crude Oil')")
    # one event with url/source; a second event id that will be rolled off (absent)
    store.insert_event(conn, {"id": "ev1", "headline": "Apple hit", "source": "SEC 8-K",
                              "url": "https://x/ev1", "category": "m&a", "published_at": "2026-06-29",
                              "seed_entity": "Apple", "seed_node_id": "cik:1", "status": "traced"})
    top = [{"event_id": "ev1", "headline": "Apple hit", "direction": "negative", "magnitude": 0.7,
            "weighted": -0.7, "hop": 1, "published_at": "2026-06-29"},
           {"event_id": "gone", "headline": "rolled off", "direction": "positive", "magnitude": 0.2,
            "weighted": 0.2, "hop": 2, "published_at": "2026-06-20"}]
    store.replace_node_impact(conn, [
        {"node_id": "cik:1", "direction": "negative", "magnitude": 0.62, "mixed_signals": 0,
         "event_count": 2, "top_events": json.dumps(top), "computed_at": "2026-06-30T00:00:00"}])
    conn.commit(); conn.close()
    return db


def _client(tmp_path):
    api_main.set_db_path(_seed(tmp_path))
    return TestClient(api_main.app)


def test_impact_live(tmp_path):
    r = _client(tmp_path).get("/impact/live")
    assert r.status_code == 200
    body = r.json()
    assert body["computed_at"] == "2026-06-30T00:00:00" and body["count"] == 1
    row = body["impacts"][0]
    assert row == {"node_id": "cik:1", "direction": "negative", "magnitude": 0.62,
                   "mixed_signals": 0, "event_count": 2}


def test_node_impact_enriched(tmp_path):
    r = _client(tmp_path).get("/node/cik:1/impact")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Apple" and body["type"] == "Company"
    imp = body["impact"]
    assert imp["direction"] == "negative" and imp["event_count"] == 2
    te = imp["top_events"]
    assert te[0]["url"] == "https://x/ev1" and te[0]["source"] == "SEC 8-K"
    assert te[1]["url"] is None and te[1]["source"] is None      # rolled-off event tolerated


def test_node_without_impact(tmp_path):
    body = _client(tmp_path).get("/node/slug:oil/impact").json()
    assert body["name"] == "Crude Oil" and body["impact"] is None


def test_node_impact_unknown_404(tmp_path):
    assert _client(tmp_path).get("/node/cik:999/impact").status_code == 404


def test_health_has_freshness(tmp_path):
    body = _client(tmp_path).get("/health").json()
    assert body["node_impact_rows"] == 1
    assert body["node_impact_computed_at"] == "2026-06-30T00:00:00"


def test_impact_live_empty(tmp_path):
    db = tmp_path / "empty.db"
    conn = store.connect(db); store.init_db(conn); conn.close()   # node_impact exists but empty
    api_main.set_db_path(db)
    body = TestClient(api_main.app).get("/impact/live").json()
    assert body == {"computed_at": None, "count": 0, "impacts": []}


def test_node_impact_resolves_alias(tmp_path):
    db = _seed(tmp_path)
    conn = store.connect(db)
    conn.execute("INSERT INTO aliases (alias, node_id, alias_normalized) VALUES ('AAPL','cik:1','aapl')")
    conn.commit(); conn.close()
    api_main.set_db_path(db)
    body = TestClient(api_main.app).get("/node/AAPL/impact").json()
    assert body["node_id"] == "cik:1"                 # alias resolved to canonical id
    assert body["impact"]["event_count"] == 2


def test_impact_endpoints_survive_pre_p4_db(tmp_path):
    # A DB built before P4 has nodes but none of the V2 tables. The impact
    # endpoints must serve empty results, not 500 on the missing node_impact table.
    db = tmp_path / "old.db"
    conn = store.connect(db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, name TEXT)")
    conn.execute("INSERT INTO nodes (id, type, name) VALUES ('cik:1','Company','Apple')")
    conn.commit(); conn.close()
    api_main.set_db_path(db)
    client = TestClient(api_main.app)
    assert client.get("/impact/live").json() == {"computed_at": None, "count": 0, "impacts": []}
    assert client.get("/node/cik:1/impact").json()["impact"] is None    # 200, not 500


def test_health_degrades_without_node_impact_table(tmp_path):
    db = _seed(tmp_path)
    conn = store.connect(db)
    conn.execute("DROP TABLE node_impact")            # simulate a pre-P4 DB
    conn.commit(); conn.close()
    api_main.set_db_path(db)
    body = TestClient(api_main.app).get("/health").json()
    assert body["node_impact_rows"] == 0 and body["node_impact_computed_at"] is None


def test_impact_live_locked_db_returns_503(tmp_path, monkeypatch):
    # A transient write-lock (the hourly aggregate is mid-commit) must NOT be
    # laundered into an empty overlay — the cache is fine, we just collided.
    # Surface 503 so the frontend retries. Simulate by making the store read
    # helper raise the exact OperationalError SQLite raises on lock contention.
    _client(tmp_path)  # seed + set_db_path; we replace the read helper below

    def _locked(_conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "read_all_node_impact", _locked)
    r = TestClient(api_main.app).get("/impact/live")
    assert r.status_code == 503
    assert r.json()["detail"] == "impact cache temporarily locked, retry"


def test_node_impact_locked_db_returns_503(tmp_path, monkeypatch):
    _client(tmp_path)

    def _locked(_conn, _node_id):
        raise sqlite3.OperationalError("database is busy")

    monkeypatch.setattr(store, "read_node_impact", _locked)
    r = TestClient(api_main.app).get("/node/cik:1/impact")
    assert r.status_code == 503
    assert r.json()["detail"] == "impact cache temporarily locked, retry"


def test_impact_live_computed_at_matches_rows_atomically(tmp_path):
    # The snapshot read must return computed_at drawn from the SAME view as the
    # rows. With a populated seed, the timestamp is the seeded one and the row
    # count is consistent — the two reads happen inside one transaction.
    body = _client(tmp_path).get("/impact/live").json()
    assert body["count"] == 1
    assert body["computed_at"] == "2026-06-30T00:00:00"


def test_node_impact_locked_on_resolve_returns_503(tmp_path, monkeypatch):
    # A lock during resolve_id (BEFORE the guarded read) must also surface 503,
    # not an uncaught 500.
    _client(tmp_path)
    def _locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(api_main, "resolve_id", _locked)
    r = TestClient(api_main.app).get("/node/cik:1/impact")
    assert r.status_code == 503


def test_impact_refresh_start_and_status(tmp_path, monkeypatch):
    # POST starts a background cycle; GET status reports completion + result.
    _client(tmp_path)
    import pipeline.run_cycle as rc
    calls = {"n": 0}
    monkeypatch.setattr(rc, "run_cycle", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or {"ok": True, "ingest": {"queued": 3}})
    import time as _t
    client = TestClient(api_main.app)
    assert client.post("/impact/refresh").json()["status"] in ("started", "already_running")
    for _ in range(100):
        if not client.get("/impact/refresh/status").json()["running"]:
            break
        _t.sleep(0.02)
    st = client.get("/impact/refresh/status").json()
    assert st["running"] is False and st["result"] == {"ok": True, "ingest": {"queued": 3}} and st["error"] is None
    assert calls["n"] == 1


def test_health_reports_event_counts(tmp_path):
    # /health now surfaces how many news events are traced (P11 counter).
    body = _client(tmp_path).get("/health").json()
    assert body["events_traced"] == 1          # _seed() inserts one traced event
    assert body["events"].get("traced") == 1
