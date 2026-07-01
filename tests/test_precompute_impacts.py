from __future__ import annotations
from schema import store
from pipeline import precompute_impacts as pc


def _db_with_queued(tmp_path, n=2):
    db = tmp_path / "g.db"
    conn = store.connect(db); store.init_db(conn)
    for i in range(n):
        store.insert_event(conn, {"id": f"e{i}", "headline": f"h{i}", "source": "SEC 8-K",
                                  "url": f"u{i}", "category": "m&a", "published_at": "2026-06-17",
                                  "seed_entity": f"cik:{i}", "seed_node_id": f"cik:{i}", "status": "queued"})
    conn.close()
    return db


def test_traces_queued_event_and_writes_impacts(tmp_path, monkeypatch):
    db = _db_with_queued(tmp_path, 1)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {
        "seed": {"node_id": "cik:0"}, "seeds": [{"node_id": "cik:0"}],
        "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.9, "hop": 0, "reasoning": "seed"},
                    {"node_id": "cik:9", "direction": "positive", "magnitude": 0.4, "hop": 1, "reasoning": "x"}]})
    s = pc.run_precompute(db)
    assert s["traced"] == 1 and s["failed"] == 0 and s["impacts_written"] == 2
    conn = store.connect(db)
    assert conn.execute("SELECT status FROM events WHERE id='e0'").fetchone()["status"] == "traced"
    assert conn.execute("SELECT COUNT(*) c FROM event_impacts WHERE event_id='e0'").fetchone()["c"] == 2


def test_no_seeds_marks_failed_no_rows(tmp_path, monkeypatch):
    db = _db_with_queued(tmp_path, 1)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {"error": "no seeds", "seeds": [], "impacts": []})
    s = pc.run_precompute(db)
    assert s["failed"] == 1 and s["traced"] == 0
    conn = store.connect(db)
    assert conn.execute("SELECT status FROM events WHERE id='e0'").fetchone()["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) c FROM event_impacts").fetchone()["c"] == 0


def test_run_impact_raising_marks_failed_and_continues(tmp_path, monkeypatch):
    db = _db_with_queued(tmp_path, 2)
    def boom(*a, **k): raise RuntimeError("kaboom")
    monkeypatch.setattr(pc._impact, "run_impact", boom)
    s = pc.run_precompute(db)
    assert s["failed"] == 2 and s["traced"] == 0   # both fail, loop didn't crash


def test_max_events_budget(tmp_path, monkeypatch):
    db = _db_with_queued(tmp_path, 2)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {
        "seeds": [{"node_id": "x"}], "impacts": [{"node_id": "x", "direction": "negative", "magnitude": 0.5, "hop": 0}]})
    s = pc.run_precompute(db, max_events=1)
    assert s["processed"] == 1
    conn = store.connect(db)
    assert conn.execute("SELECT COUNT(*) c FROM events WHERE status='queued'").fetchone()["c"] == 1


def test_idempotent_when_nothing_queued(tmp_path, monkeypatch):
    db = _db_with_queued(tmp_path, 1)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {
        "seeds": [{"node_id": "cik:0"}], "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.5, "hop": 0}]})
    pc.run_precompute(db)
    s2 = pc.run_precompute(db)          # nothing queued now
    assert s2["processed"] == 0
