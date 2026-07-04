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


# ── story signature (cross-time / cross-source dedup) ───────────────────────

def test_story_signature_deterministic_and_seed_scoped():
    a = store.story_signature("cik:1", "Walmart acquires regional grocery chain today")
    assert a and a.startswith("sig:")
    assert a == store.story_signature("cik:1", "WALMART!!  acquires  regional Grocery chain TODAY")  # normalized
    assert a != store.story_signature("cik:2", "Walmart acquires regional grocery chain today")      # seed-scoped
    assert a != store.story_signature("cik:1", "Walmart recalls faulty product line nationwide")     # diff story


def test_story_signature_none_cases():
    assert store.story_signature(None, "Walmart acquires regional grocery chain") is None
    assert store.story_signature("cik:1", None) is None
    assert store.story_signature("cik:1", "") is None
    assert store.story_signature("cik:1", "Its shares slip today") is None      # <3 distinctive tokens
    assert store.story_signature("cik:1", "Stock report update") is None        # all boilerplate → none


def test_story_signature_keys_on_distinctive_tokens_not_boilerplate():
    # Distinct analyst notes sharing a boilerplate prefix must NOT collapse — the
    # differentiating number/broker (beyond word 6) now enters the signature.
    a = store.story_signature("cik:1", "Apple stock price target raised to 250 at Morgan Stanley")
    b = store.story_signature("cik:1", "Apple stock price target raised to 240 at Wedbush")
    assert a and b and a != b
    # a verbatim cross-source copy still collapses (case/punct/whitespace-insensitive)
    assert a == store.story_signature("cik:1", "Apple  STOCK price target Raised to 250 at Morgan Stanley!")


def test_story_signature_ascii_folds_accents():
    assert store.story_signature("cik:1", "Nestlé raises full-year sales guidance") == \
           store.story_signature("cik:1", "Nestle raises full-year sales guidance")


def test_story_sig_seen_falls_back_on_nonISO_published_at():
    conn = _mem()
    sig = store.story_signature("cik:1", "Acme acquires Beta in landmark cash deal")
    # compact non-ISO published_at → SQLite date() is NULL → must fall back to ingested_at
    conn.execute("INSERT INTO events (id, headline, seed_node_id, published_at, ingested_at, status, story_sig) "
                 "VALUES ('e','h','cik:1','20260704', datetime('now','-1 days'), 'traced', ?)", (sig,))
    conn.commit()
    assert store.story_sig_seen(conn, sig, 7) is True


def test_insert_event_persists_and_computes_story_sig():
    conn = _mem()
    hl = "Nvidia unveils new AI chip architecture"
    # explicit story_sig honored — incl. None for a _no_collapse-style row
    store.insert_event(conn, {"id": "e1", "headline": hl, "seed_node_id": "cik:9",
                              "status": "queued", "story_sig": None})
    assert conn.execute("SELECT story_sig FROM events WHERE id='e1'").fetchone()[0] is None
    # absent → computed from seed + headline
    store.insert_event(conn, {"id": "e2", "headline": hl, "seed_node_id": "cik:9", "status": "queued"})
    assert conn.execute("SELECT story_sig FROM events WHERE id='e2'").fetchone()[0] == \
        store.story_signature("cik:9", hl)


def test_story_sig_seen_windowed():
    conn = _mem()
    sig = store.story_signature("cik:1", "Walmart acquires regional grocery chain today")
    conn.execute("INSERT INTO events (id, headline, seed_node_id, published_at, status, story_sig) "
                 "VALUES ('old','h','cik:1', date('now','-6 days'), 'traced', ?)", (sig,))
    conn.commit()
    assert store.story_sig_seen(conn, sig, 7) is True      # 6 days old, within 7-day window
    assert store.story_sig_seen(conn, sig, 3) is False     # outside a 3-day window
    assert store.story_sig_seen(conn, "sig:unknown", 7) is False
    assert store.story_sig_seen(conn, None, 7) is False


def test_migration_adds_and_backfills_story_sig():
    import sqlite3
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    # OLD-shape events table (no story_sig column, no index)
    conn.execute("CREATE TABLE events (id TEXT PRIMARY KEY, headline TEXT NOT NULL, source TEXT, "
                 "url TEXT, category TEXT, published_at TEXT, ingested_at TEXT DEFAULT (datetime('now')), "
                 "seed_entity TEXT, seed_node_id TEXT, status TEXT NOT NULL DEFAULT 'queued')")
    conn.execute("INSERT INTO events (id, headline, seed_node_id) VALUES "
                 "('e1','Walmart acquires regional grocery chain today','cik:1')")
    conn.commit()
    store.init_db(conn)   # DDL is a no-op on the existing table; _migrate_story_sig adds+backfills
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    assert "story_sig" in cols
    got = conn.execute("SELECT story_sig FROM events WHERE id='e1'").fetchone()["story_sig"]
    assert got == store.story_signature("cik:1", "Walmart acquires regional grocery chain today")
    idx = [r["name"] for r in conn.execute("PRAGMA index_list(events)").fetchall()]
    assert "idx_events_story_sig" in idx
    store.init_db(conn)   # idempotent second run must not raise


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


def test_read_all_and_one_node_impact():
    conn = _mem()
    store.replace_node_impact(conn, [
        {"node_id": "b", "direction": "negative", "magnitude": 0.4, "mixed_signals": 0,
         "event_count": 1, "top_events": "[]", "computed_at": "2026-06-30T00:00:00"},
        {"node_id": "a", "direction": "positive", "magnitude": 0.7, "mixed_signals": 1,
         "event_count": 2, "top_events": "[]", "computed_at": "2026-06-30T00:00:00"},
    ])
    allrows = store.read_all_node_impact(conn)
    assert [r["node_id"] for r in allrows] == ["a", "b"]          # ordered by node_id
    assert allrows[0]["direction"] == "positive" and allrows[0]["event_count"] == 2
    assert "top_events" not in allrows[0]                          # compact rows only
    one = store.read_node_impact(conn, "a")
    assert one["magnitude"] == 0.7 and one["top_events"] == "[]"   # full row
    assert store.read_node_impact(conn, "missing") is None
    assert store.latest_node_impact_computed_at(conn) == "2026-06-30T00:00:00"


def test_latest_computed_at_empty():
    assert store.latest_node_impact_computed_at(_mem()) is None


def test_busy_timeout_is_set():
    conn = store.connect(":memory:")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_prune_old_events_deletes_only_aged_and_cascades_impacts():
    conn = _mem()
    # Aged event via published_at (well past a 30-day horizon).
    store.insert_event(conn, {
        "id": "old", "headline": "stale", "source": "s", "url": "u", "category": "c",
        "published_at": "2000-01-01", "seed_entity": "E", "seed_node_id": "cik:1",
        "status": "traced",
    })
    # Fresh event (published today) — must survive.
    store.insert_event(conn, {
        "id": "new", "headline": "fresh", "source": "s", "url": "u", "category": "c",
        "published_at": "2026-06-30", "seed_entity": "E", "seed_node_id": "cik:2",
        "status": "traced",
    })
    store.write_event_impacts(conn, "old", [
        {"node_id": "cik:1", "direction": "negative", "magnitude": 0.5, "hop": 0}])
    store.write_event_impacts(conn, "new", [
        {"node_id": "cik:2", "direction": "positive", "magnitude": 0.4, "hop": 0}])

    deleted = store.prune_old_events(conn, older_than_days=30)
    assert deleted == 1
    remaining = [r["id"] for r in conn.execute("SELECT id FROM events ORDER BY id")]
    assert remaining == ["new"]
    # event_impacts for the aged event cascaded out; the fresh one's stayed.
    imp = [r["event_id"] for r in conn.execute("SELECT event_id FROM event_impacts")]
    assert imp == ["new"]


def test_prune_old_events_falls_back_to_ingested_at():
    conn = _mem()
    # No published_at → age uses ingested_at. Freshly ingested now, so a 30-day
    # prune must NOT touch it.
    store.insert_event(conn, {
        "id": "nopub", "headline": "h", "source": "s", "url": "u", "category": "c",
        "published_at": None, "seed_entity": "E", "seed_node_id": "cik:1", "status": "queued",
    })
    assert store.prune_old_events(conn, older_than_days=30) == 0
    assert store.event_exists(conn, "nopub") is True


def test_read_all_node_impact_omits_no_effect():
    conn = _mem()
    store.replace_node_impact(conn, [
        {"node_id": "a", "direction": "positive", "magnitude": 0.7, "mixed_signals": 0,
         "event_count": 2, "top_events": "[]", "computed_at": "2026-06-30T00:00:00"},
        {"node_id": "b", "direction": "no_effect", "magnitude": 0.0, "mixed_signals": 0,
         "event_count": 1, "top_events": "[]", "computed_at": "2026-06-30T00:00:00"},
    ])
    allrows = store.read_all_node_impact(conn)
    assert [r["node_id"] for r in allrows] == ["a"]                 # no_effect excluded
    # But a directly-queried no_effect node still resolves.
    one = store.read_node_impact(conn, "b")
    assert one is not None and one["direction"] == "no_effect"


def test_read_helpers_reraise_lock_but_swallow_missing_table():
    import sqlite3, pytest
    class _LockedConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")
    class _MissingConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such table: node_impact")
    # lock → propagate (so the API can turn it into a retryable 503)
    for fn in (lambda c: store.read_all_node_impact(c),
               lambda c: store.read_node_impact(c, "x"),
               lambda c: store.latest_node_impact_computed_at(c)):
        with pytest.raises(sqlite3.OperationalError):
            fn(_LockedConn())
    # missing table → graceful empty/None (pre-P4 DB)
    assert store.read_all_node_impact(_MissingConn()) == []
    assert store.read_node_impact(_MissingConn(), "x") is None
    assert store.latest_node_impact_computed_at(_MissingConn()) is None
