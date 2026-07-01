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
