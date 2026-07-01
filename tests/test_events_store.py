from __future__ import annotations
from schema import store


def _mem():
    conn = store.connect(":memory:")
    store.init_db(conn)
    return conn


def test_events_table_created_and_insert_query():
    conn = _mem()
    store.insert_event(conn, {
        "id": "e1", "headline": "Acme acquires Beta", "source": "SEC 8-K",
        "url": "http://x/1", "category": "m&a", "published_at": "2026-06-17",
        "seed_entity": "Acme", "seed_node_id": "cik:0000000001", "status": "queued",
    })
    assert store.event_exists(conn, "e1") is True
    assert store.event_exists(conn, "nope") is False
    q = store.queued_events(conn)
    assert len(q) == 1 and q[0]["id"] == "e1" and q[0]["status"] == "queued"


def test_insert_event_is_idempotent_on_id():
    conn = _mem()
    row = {"id": "e1", "headline": "h", "source": "s", "url": "u", "category": "c",
           "published_at": None, "seed_entity": "E", "seed_node_id": "cik:1", "status": "queued"}
    store.insert_event(conn, row)
    store.insert_event(conn, {**row, "headline": "changed"})   # same id → ignored
    q = store.queued_events(conn)
    assert len(q) == 1 and q[0]["headline"] == "h"


def test_event_impacts_write_and_replace():
    conn = _mem()
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:1", "direction": "negative", "magnitude": 0.8, "hop": 0, "reasoning": "seed"},
        {"node_id": "cik:2", "direction": "positive", "magnitude": 0.3, "hop": 1, "reasoning": "x"},
    ])
    rows = conn.execute("SELECT node_id, direction, magnitude, hop FROM event_impacts WHERE event_id='e1' ORDER BY hop").fetchall()
    assert [r["node_id"] for r in rows] == ["cik:1", "cik:2"]
    # Re-write is delete-then-insert (no dup PK, replaces cleanly).
    store.write_event_impacts(conn, "e1", [
        {"node_id": "cik:3", "direction": "negative", "magnitude": 0.5, "hop": 1, "reasoning": "y"}])
    rows = conn.execute("SELECT node_id FROM event_impacts WHERE event_id='e1'").fetchall()
    assert [r["node_id"] for r in rows] == ["cik:3"]


def test_set_event_status():
    conn = _mem()
    store.insert_event(conn, {"id": "e1", "headline": "h", "source": "s", "url": "u",
                              "category": "c", "published_at": None, "seed_entity": "E",
                              "seed_node_id": "cik:1", "status": "queued"})
    store.set_event_status(conn, "e1", "traced")
    assert conn.execute("SELECT status FROM events WHERE id='e1'").fetchone()["status"] == "traced"


def test_replace_node_impact_atomic_swap():
    conn = _mem()
    store.replace_node_impact(conn, [
        {"node_id": "cik:1", "direction": "negative", "magnitude": 0.6, "mixed_signals": 0,
         "event_count": 2, "top_events": '[]', "computed_at": "2026-06-17T00:00:00"},
    ])
    r = conn.execute("SELECT * FROM node_impact").fetchall()
    assert len(r) == 1 and r[0]["node_id"] == "cik:1" and r[0]["direction"] == "negative"
    # Replace wipes the prior set (node no longer present drops out).
    store.replace_node_impact(conn, [
        {"node_id": "cik:2", "direction": "positive", "magnitude": 0.3, "mixed_signals": 1,
         "event_count": 1, "top_events": '[]', "computed_at": "2026-06-18T00:00:00"},
    ])
    ids = [x["node_id"] for x in conn.execute("SELECT node_id FROM node_impact")]
    assert ids == ["cik:2"]
