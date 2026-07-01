from pipeline import run_cycle as rc


def test_run_cycle_orders_stages_and_aggregates_summary(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(rc, "_run_ingest", lambda db: calls.append("i") or {"queued": 3})
    monkeypatch.setattr(rc, "_run_precompute", lambda db, provider: calls.append("p") or {"traced": 2})
    monkeypatch.setattr(rc, "_run_aggregate", lambda db: calls.append("a") or {"nodes": 5})
    s = rc.run_cycle(tmp_path / "g.db")
    assert calls == ["i", "p", "a"]
    assert s["ok"] is True
    assert s["ingest"] == {"queued": 3} and s["precompute"] == {"traced": 2} and s["aggregate"] == {"nodes": 5}
    assert "elapsed_s" in s


def test_run_cycle_isolates_stage_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, "_run_ingest", lambda db: {"queued": 1})
    def boom(db, provider): raise RuntimeError("throttled")
    monkeypatch.setattr(rc, "_run_precompute", boom)
    ran = {}
    monkeypatch.setattr(rc, "_run_aggregate", lambda db: ran.setdefault("agg", True) or {"nodes": 0})
    s = rc.run_cycle(tmp_path / "g.db")
    assert s["ok"] is False
    assert "error" in s["precompute"] and "throttled" in s["precompute"]["error"]
    assert ran.get("agg") is True                 # aggregate still ran after precompute failed


def test_scheduler_once_runs_single_cycle(monkeypatch):
    from pipeline import scheduler
    n = {"c": 0}
    monkeypatch.setattr(scheduler, "run_cycle", lambda **k: n.__setitem__("c", n["c"] + 1) or {"ok": True})
    slept = {"n": 0}
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: slept.__setitem__("n", slept["n"] + 1))
    scheduler.main(["--once"])
    assert n["c"] == 1 and slept["n"] == 0        # one cycle, no sleep
