import json
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


def test_health_degrades_without_node_impact_table(tmp_path):
    db = _seed(tmp_path)
    conn = store.connect(db)
    conn.execute("DROP TABLE node_impact")            # simulate a pre-P4 DB
    conn.commit(); conn.close()
    api_main.set_db_path(db)
    body = TestClient(api_main.app).get("/health").json()
    assert body["node_impact_rows"] == 0 and body["node_impact_computed_at"] is None
