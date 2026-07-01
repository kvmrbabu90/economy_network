# So What? V2 · P4 — Serving + Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Serve the precomputed `node_impact` cache over two read-only endpoints (no request-time LLM), and add a one-shot cycle orchestrator + thin scheduler wrapper.

**Architecture:** Read helpers in `schema/store.py` → FastAPI endpoints in `api/main.py` → orchestrator `pipeline/run_cycle.py` chaining the existing P1/P2/P3 runnables → optional `pipeline/scheduler.py` loop.

**Tech Stack:** Python 3.x, FastAPI + Starlette TestClient, SQLite (`sqlite3.Row`), pytest. Run everything with `python -B` (OneDrive stale-pycache hazard).

**Design:** `docs/superpowers/specs/2026-06-30-sowhat-v2-p4-serving-scheduler-design.md`

---

### Task 1: `node_impact` read helpers

**Files:**
- Modify: `schema/store.py` (add after `replace_node_impact`)
- Test: `tests/test_events_store.py` (append)

Context: `store.connect(db_path)` returns a `sqlite3.Row` connection; `init_db(conn)`
applies the DDL. `node_impact` columns: `node_id, direction, magnitude,
mixed_signals, event_count, top_events, computed_at`. `replace_node_impact(conn, rows)`
already exists. `Any`/`Optional`/`sqlite3` are imported.

- [ ] **Step 1: Write failing tests** (append to `tests/test_events_store.py`, reuse the existing `_mem()` helper)

```python
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
```

- [ ] **Step 2: Run, verify fail** — `python -B -m pytest tests/test_events_store.py -k node_impact -v` → AttributeError.

- [ ] **Step 3: Implement** (append after `replace_node_impact` in `schema/store.py`)

```python
def read_all_node_impact(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Compact node_impact rows for graph tinting, ordered by node_id."""
    rows = conn.execute(
        "SELECT node_id, direction, magnitude, mixed_signals, event_count "
        "FROM node_impact ORDER BY node_id"
    ).fetchall()
    return [dict(r) for r in rows]


def read_node_impact(conn: sqlite3.Connection, node_id: str) -> Optional[dict[str, Any]]:
    """Full node_impact row (top_events remains a JSON string) or None."""
    row = conn.execute(
        "SELECT node_id, direction, magnitude, mixed_signals, event_count, "
        "top_events, computed_at FROM node_impact WHERE node_id = ?", (node_id,)
    ).fetchone()
    return dict(row) if row else None


def latest_node_impact_computed_at(conn: sqlite3.Connection) -> Optional[str]:
    """MAX(computed_at) across node_impact, or None when the table is empty."""
    row = conn.execute("SELECT MAX(computed_at) AS c FROM node_impact").fetchone()
    return row["c"] if row and row["c"] is not None else None
```

- [ ] **Step 4: Run, verify pass** — `python -B -m pytest tests/test_events_store.py -v`.
- [ ] **Step 5: Commit** — `feat(v2): node_impact read helpers for serving`.

---

### Task 2: Serving endpoints + `/health` freshness

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_api_impact_live.py` (create)

Context (verify by reading `api/main.py`): `app = FastAPI(...)`; a `get_conn()`
dependency yields a per-request `sqlite3.Connection`; `set_db_path(path)` overrides the
module DB path (used by tests); there is an existing `GET /health` and a **catch-all**
`GET /node/{node_id:path}`. `api/query.py` exposes `resolve_id(conn, raw_id) -> str|None`.
Import `resolve_id` and the three new store helpers.

- [ ] **Step 1: Write failing tests** (`tests/test_api_impact_live.py`)

```python
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
```

- [ ] **Step 2: Run, verify fail** — `python -B -m pytest tests/test_api_impact_live.py -v` (404s / missing keys).

- [ ] **Step 3: Implement in `api/main.py`.** Add imports (`import json`, `from api.query import resolve_id`, and the three store helpers already reachable via the existing `store` import — verify the module alias). Add the endpoints; **`/node/{node_id:path}/impact` MUST be declared before the catch-all `GET /node/{node_id:path}`** (grep for the existing `/ego` route and place this next to it).

```python
@app.get("/impact/live")
def impact_live(conn: sqlite3.Connection = Depends(get_conn)):
    rows = store.read_all_node_impact(conn)
    return {"computed_at": store.latest_node_impact_computed_at(conn),
            "count": len(rows), "impacts": rows}


@app.get("/node/{node_id:path}/impact")   # BEFORE the catch-all /node/{node_id:path}
def node_impact(node_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    cid = resolve_id(conn, node_id)
    if not cid:
        raise HTTPException(status_code=404, detail=f"unknown node: {node_id}")
    nrow = conn.execute("SELECT name, type FROM nodes WHERE id = ?", (cid,)).fetchone()
    name = nrow["name"] if nrow else cid
    ntype = nrow["type"] if nrow else None
    raw = store.read_node_impact(conn, cid)
    if raw is None:
        return {"node_id": cid, "name": name, "type": ntype, "impact": None}
    top = json.loads(raw["top_events"] or "[]")
    ids = [e["event_id"] for e in top if e.get("event_id")]
    meta = {}
    if ids:
        q = "SELECT id, url, source FROM events WHERE id IN (%s)" % ",".join("?" * len(ids))
        meta = {r["id"]: r for r in conn.execute(q, ids).fetchall()}
    for e in top:
        m = meta.get(e.get("event_id"))
        e["url"] = m["url"] if m else None
        e["source"] = m["source"] if m else None
    return {"node_id": cid, "name": name, "type": ntype,
            "impact": {"direction": raw["direction"], "magnitude": raw["magnitude"],
                       "mixed_signals": raw["mixed_signals"], "event_count": raw["event_count"],
                       "computed_at": raw["computed_at"], "top_events": top}}
```

For `/health`: locate the existing handler and add to its returned dict:
```python
    "node_impact_rows": conn.execute("SELECT COUNT(*) c FROM node_impact").fetchone()["c"],
    "node_impact_computed_at": store.latest_node_impact_computed_at(conn),
```
(Ensure `/health` has access to a connection — reuse its existing conn/`get_conn`
dependency; if it doesn't take one, add `conn: sqlite3.Connection = Depends(get_conn)`.)

Confirm `HTTPException` and `Depends` are imported (they are used elsewhere — verify).

- [ ] **Step 4: Run, verify pass** — `python -B -m pytest tests/test_api_impact_live.py -v`.
- [ ] **Step 5: Regression** — `python -B -m pytest tests/ -q` (no existing test breaks; if `test_api.py` has the known-unrelated `test_walmart_customer_of_derivation` failure it stays as-is).
- [ ] **Step 6: Commit** — `feat(v2): serve /impact/live + /node/{id}/impact; /health freshness`.

---

### Task 3: `run_cycle` orchestrator

**Files:**
- Create: `pipeline/run_cycle.py`
- Test: `tests/test_run_cycle.py`

Context: **read the real signatures first** of `pipeline/ingest_news.py`'s ingest
entrypoint, `pipeline/precompute_impacts.py::run_precompute(db_path, *, ..., provider=None, ...)`,
and `pipeline/aggregate_impacts.py::aggregate(conn, *, ...)`. Wire the real calls in
`_run_ingest`/`_run_precompute`/`_run_aggregate` thin adapters so the un-mocked
`main()` path works; the tests monkeypatch the three module-level stage functions.

- [ ] **Step 1: Write failing tests** (`tests/test_run_cycle.py`)

```python
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
```

- [ ] **Step 2: Run, verify fail** — module missing.

- [ ] **Step 3: Implement `pipeline/run_cycle.py`**

```python
"""So What? V2 · Phase 4 — full-cycle orchestrator.

Runs the ingest -> precompute -> aggregate pipeline once, against one DB, with
per-stage error isolation. Idempotent + restartable. Schedule this every 12h.

    python -B -m pipeline.run_cycle
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from pipeline import ingest_news, precompute_impacts, aggregate_impacts
from schema import store

log = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "econgraph.db"


def _run_ingest(db_path) -> dict:
    return ingest_news.run_ingest(db_path=db_path)          # adapt to real signature


def _run_precompute(db_path, provider) -> dict:
    return precompute_impacts.run_precompute(db_path, provider=provider)


def _run_aggregate(db_path) -> dict:
    conn = store.connect(db_path); store.init_db(conn)
    try:
        return aggregate_impacts.aggregate(conn)
    finally:
        conn.close()


def _stage(name: str, fn: Callable[[], dict], summary: dict) -> None:
    try:
        summary[name] = fn()
    except Exception as exc:                                # isolate: log, record, continue
        log.exception("run_cycle: stage %s failed", name)
        summary[name] = {"error": repr(exc)}
        summary["ok"] = False


def run_cycle(db_path=DB_PATH, *, provider: Optional[str] = None) -> dict:
    t0 = time.time()
    summary: dict = {"ok": True}
    _stage("ingest", lambda: _run_ingest(db_path), summary)
    _stage("precompute", lambda: _run_precompute(db_path, provider), summary)
    _stage("aggregate", lambda: _run_aggregate(db_path), summary)
    summary["elapsed_s"] = round(time.time() - t0, 1)
    log.info("run_cycle: %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    s = run_cycle()
    print(f"cycle: {s}")
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Verify the real stage signatures** — open `ingest_news.py` and adjust
`_run_ingest` to match its actual entrypoint (name + params); confirm `run_precompute`
and `aggregate` calls compile. Do NOT change P1/P2/P3 code.
- [ ] **Step 5: Run, verify pass** — `python -B -m pytest tests/test_run_cycle.py -v`.
- [ ] **Step 6: Commit** — `feat(v2): run_cycle orchestrator (ingest->precompute->aggregate)`.

---

### Task 4: `scheduler` wrapper + docs

**Files:**
- Create: `pipeline/scheduler.py`
- Test: `tests/test_run_cycle.py` (append)
- Modify: `.env.example`

- [ ] **Step 1: Write failing test** (append to `tests/test_run_cycle.py`)

```python
def test_scheduler_once_runs_single_cycle(monkeypatch):
    from pipeline import scheduler
    n = {"c": 0}
    monkeypatch.setattr(scheduler, "run_cycle", lambda **k: n.__setitem__("c", n["c"] + 1) or {"ok": True})
    slept = {"n": 0}
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: slept.__setitem__("n", slept["n"] + 1))
    scheduler.main(["--once"])
    assert n["c"] == 1 and slept["n"] == 0        # one cycle, no sleep
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement `pipeline/scheduler.py`**

```python
"""So What? V2 · Phase 4 — unattended scheduler.

Loops run_cycle every SCHEDULER_INTERVAL_S (default 12h). Use --once for a single
cycle. RECOMMENDED on a workstation: instead of this long-lived loop, register an OS
scheduler (Windows Task Scheduler / cron) to run `python -B -m pipeline.run_cycle`
every 12h — more robust across sleep/restart.

    python -B -m pipeline.scheduler --once
    python -B -m pipeline.scheduler            # loop forever
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Optional, Sequence

from pipeline.run_cycle import run_cycle

log = logging.getLogger(__name__)
INTERVAL_S = int(os.environ.get("SCHEDULER_INTERVAL_S", str(12 * 3600)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="So What? V2 12h cycle scheduler.")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--interval", type=int, default=INTERVAL_S, help="seconds between cycles")
    args = ap.parse_args(argv)
    if args.once:
        s = run_cycle()
        return 0 if s.get("ok") else 1
    log.info("scheduler: looping every %ds; Ctrl-C to stop", args.interval)
    while True:
        run_cycle()
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run, verify pass** — `python -B -m pytest tests/test_run_cycle.py -v`.
- [ ] **Step 5: `.env.example`** — append under the So What? V2 block:
```
SCHEDULER_INTERVAL_S=43200   # 12h; used only by `python -B -m pipeline.scheduler` (loop mode)
# Recommended instead of the loop: an OS scheduler (Windows Task Scheduler / cron)
# running `python -B -m pipeline.run_cycle` every 12h.
```
- [ ] **Step 6: Commit** — `feat(v2): scheduler wrapper (--once / loop) + docs`.

---

## Self-review checklist (run after all tasks)
- `/node/{id}/impact` declared before the catch-all `/node` route.
- `run_cycle` calls real P1/P2/P3 signatures (verified against source), no P1–P3 edits.
- Full suite green except the pre-existing unrelated `test_walmart_customer_of_derivation`.
