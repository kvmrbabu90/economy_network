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


def test_no_neighbors_marks_failed_no_rows(tmp_path, monkeypatch):
    # Seed resolved but the node has no supply-chain edges: run_impact returns seed-only
    # impacts plus error+no_neighbors. This must NOT be written as 'traced' (phantom rows).
    db = _db_with_queued(tmp_path, 1)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {
        "error": "seed exists but has no recorded supply chain connections yet",
        "seed": {"node_id": "cik:0"}, "seeds": [{"node_id": "cik:0"}],
        "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.9, "hop": 0}],
        "no_neighbors": True})
    s = pc.run_precompute(db)
    assert s["failed"] == 1 and s["traced"] == 0 and s["impacts_written"] == 0
    conn = store.connect(db)
    assert conn.execute("SELECT status FROM events WHERE id='e0'").fetchone()["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) c FROM event_impacts").fetchone()["c"] == 0


def test_error_result_marks_failed_no_rows(tmp_path, monkeypatch):
    # Any truthy error on the trace result fails the event even if seeds/impacts exist.
    db = _db_with_queued(tmp_path, 1)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {
        "error": "trace aborted mid-hop", "seeds": [{"node_id": "cik:0"}],
        "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.5, "hop": 0},
                    {"node_id": "cik:9", "direction": "positive", "magnitude": 0.3, "hop": 1}]})
    s = pc.run_precompute(db)
    assert s["failed"] == 1 and s["traced"] == 0
    conn = store.connect(db)
    assert conn.execute("SELECT status FROM events WHERE id='e0'").fetchone()["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) c FROM event_impacts").fetchone()["c"] == 0


def test_all_no_effect_trace_marks_failed_no_rows(tmp_path, monkeypatch):
    # A trace that reached neighbours but scored everything 'no_effect' produced no real
    # propagation — mark failed rather than injecting seed-only / no-signal rows.
    db = _db_with_queued(tmp_path, 1)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {
        "seeds": [{"node_id": "cik:0"}],
        "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.9, "hop": 0},
                    {"node_id": "cik:9", "direction": "no_effect", "magnitude": 0.0, "hop": 1},
                    {"node_id": "cik:8", "direction": "no_effect", "magnitude": 0.0, "hop": 2}]})
    s = pc.run_precompute(db)
    assert s["failed"] == 1 and s["traced"] == 0 and s["impacts_written"] == 0
    conn = store.connect(db)
    assert conn.execute("SELECT status FROM events WHERE id='e0'").fetchone()["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) c FROM event_impacts").fetchone()["c"] == 0


def test_seed_only_no_hop1_marks_failed_no_rows(tmp_path, monkeypatch):
    # Seed scored directionally but no downstream (hop>=1) impact: seed-only trace fails.
    db = _db_with_queued(tmp_path, 1)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {
        "seeds": [{"node_id": "cik:0"}],
        "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.9, "hop": 0}]})
    s = pc.run_precompute(db)
    assert s["failed"] == 1 and s["traced"] == 0
    conn = store.connect(db)
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


def test_retry_failed_requeues_and_retraces(tmp_path, monkeypatch):
    db = _db_with_queued(tmp_path, 1)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {"seeds": [], "impacts": []})
    pc.run_precompute(db)                                    # fails -> status 'failed'
    conn = store.connect(db)
    assert conn.execute("SELECT status FROM events WHERE id='e0'").fetchone()["status"] == "failed"
    conn.close()
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {
        "seeds": [{"node_id": "cik:0"}],
        "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.5, "hop": 0},
                    {"node_id": "cik:9", "direction": "negative", "magnitude": 0.4, "hop": 1}]})
    s = pc.run_precompute(db, retry_failed=True)             # re-queues the failed event, now succeeds
    assert s["traced"] == 1
    conn = store.connect(db)
    assert conn.execute("SELECT status FROM events WHERE id='e0'").fetchone()["status"] == "traced"
    assert conn.execute("SELECT COUNT(*) c FROM event_impacts WHERE event_id='e0'").fetchone()["c"] == 2


def test_wallclock_budget_stops_immediately(tmp_path, monkeypatch):
    db = _db_with_queued(tmp_path, 2)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {
        "seeds": [{"node_id": "x"}], "impacts": [{"node_id": "x", "direction": "negative", "magnitude": 0.5, "hop": 0}]})
    s = pc.run_precompute(db, wallclock_s=0)                 # budget hit before the first event
    assert s["processed"] == 0
    conn = store.connect(db)
    assert conn.execute("SELECT COUNT(*) c FROM events WHERE status='queued'").fetchone()["c"] == 2


def test_precompute_passes_known_seed_ids_fallback(tmp_path, monkeypatch):
    # No seed_ids on the event → known_seed_ids falls back to [seed_node_id].
    db = _db_with_queued(tmp_path, 1)              # seeds an event e0 with seed_node_id 'cik:0'
    captured = {}
    def fake_run_impact(headline, **kwargs):
        captured.update(kwargs); captured["headline"] = headline
        return {"seeds": [{"node_id": "cik:0"}],
                "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.5, "hop": 0}]}
    monkeypatch.setattr(pc._impact, "run_impact", fake_run_impact)
    pc.run_precompute(db)
    assert captured.get("known_seed_ids") == ["cik:0"]   # ingest-resolved seed handed to the engine


def test_precompute_passes_known_seed_ids_from_seed_ids(tmp_path, monkeypatch):
    # seed_ids present → the full known set is passed (multi-seed), superseding seed_node_id.
    db = tmp_path / "g.db"
    conn = store.connect(db); store.init_db(conn)
    store.insert_event(conn, {"id": "e0", "headline": "h", "source": "GDELT-GKG",
                              "seed_node_id": "cik:0", "seed_ids": '["cik:0","cik:1"]', "status": "queued"})
    conn.close()
    captured = {}
    def fake_run_impact(headline, **kwargs):
        captured.update(kwargs)
        return {"seeds": [{"node_id": "cik:0"}],
                "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.5, "hop": 0}]}
    monkeypatch.setattr(pc._impact, "run_impact", fake_run_impact)
    # Default PRECOMPUTE_SEED_CAP=1 → only the primary seed is traced (best matches
    # the LLM reference; secondaries ground via capsule + are reached by propagation).
    monkeypatch.setattr(pc, "PRECOMPUTE_SEED_CAP", 1)
    pc.run_precompute(db)
    assert captured.get("known_seed_ids") == ["cik:0"]
    assert captured.get("commodity_hint") is False       # primary is not a Commodity/Region node


def test_precompute_seed_cap_zero_passes_full_set(tmp_path, monkeypatch):
    db = tmp_path / "g.db"
    conn = store.connect(db); store.init_db(conn)
    store.insert_event(conn, {"id": "e0", "headline": "h", "source": "GDELT-GKG",
                              "seed_node_id": "cik:0", "seed_ids": '["cik:0","cik:1"]', "status": "queued"})
    conn.close()
    captured = {}
    monkeypatch.setattr(pc._impact, "run_impact",
                        lambda text, **kw: captured.update(kw) or {"seed": {"node_id": "cik:0"},
                        "impacts": [{"node_id": "n", "hop": 1, "direction": "negative", "magnitude": 0.5}]})
    monkeypatch.setattr(pc, "PRECOMPUTE_SEED_CAP", 0)     # 0 = no cap → full GKG org set
    pc.run_precompute(db)
    assert captured.get("known_seed_ids") == ["cik:0", "cik:1"]


def test_retrace_failure_clears_stale_impacts(tmp_path, monkeypatch):
    db = _db_with_queued(tmp_path, 1)
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {
        "seeds": [{"node_id": "cik:0"}],
        "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.9, "hop": 0},
                    {"node_id": "cik:9", "direction": "positive", "magnitude": 0.4, "hop": 1}]})
    pc.run_precompute(db)                                    # succeeds -> impacts written
    conn = store.connect(db)
    assert conn.execute("SELECT COUNT(*) c FROM event_impacts WHERE event_id='e0'").fetchone()["c"] == 2
    conn.execute("UPDATE events SET status='queued' WHERE id='e0'"); conn.commit(); conn.close()  # operator re-run
    monkeypatch.setattr(pc._impact, "run_impact", lambda *a, **k: {"seeds": [], "impacts": []})
    pc.run_precompute(db)                                    # re-trace now fails
    conn = store.connect(db)
    assert conn.execute("SELECT status FROM events WHERE id='e0'").fetchone()["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) c FROM event_impacts WHERE event_id='e0'").fetchone()["c"] == 0
