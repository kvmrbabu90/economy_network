# So What? V2 · Phase 2 — Batch Impact Precompute — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Trace each `queued` event with a lighter Claude config and store per-node verdicts in `event_impacts`, marking events `traced`/`failed`; budgeted, idempotent, restartable.

**Architecture:** Parameterize `run_impact`/`run_impact_stream` (`max_hops`/`refine`/`verify`, defaults preserve current behavior); add `event_impacts` table + store helpers; `pipeline/precompute_impacts.py` loops `queued_events`, calls `run_impact(max_hops=2, refine=False, verify=False)`, writes impacts in a txn.

**Tech Stack:** Python 3.11 / sqlite3 / Claude CLI / pytest. **Branch:** `feat/sowhat-v2`. Run Python with `python -B`. **Spec:** `docs/superpowers/specs/2026-06-17-sowhat-v2-p2-precompute-design.md`.

---

## Task 1: Parameterize the impact engine (lighter config)

**Files:** Modify `api/impact.py`; Test `tests/test_impact_stream.py`.

- [ ] **Step 1: Failing tests** — append to `tests/test_impact_stream.py`:

```python
def test_lighter_config_skips_passes_and_limits_hops(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _fake_llm)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    calls = {"refine": 0, "verify": 0, "seedverify": 0}
    monkeypatch.setattr(impact_mod, "_refinement_pass",
                        lambda **k: calls.__setitem__("refine", calls["refine"] + 1) or {"considered": 0, "rescored": 0, "applied": 0})
    monkeypatch.setattr(impact_mod, "_verification_pass",
                        lambda **k: calls.__setitem__("verify", calls["verify"] + 1) or {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0})
    monkeypatch.setattr(impact_mod, "_verify_seed_directness",
                        lambda *a, **k: calls.__setitem__("seedverify", calls["seedverify"] + 1) or True)
    events = list(impact_mod.run_impact_stream(
        "global crude oil supply shock", conn=conn, max_hops=2, refine=False, verify=False))
    hops = [e["hop"] for e in events if e["event"] == "hop"]
    assert max(hops) <= 2                      # max_hops respected
    assert calls == {"refine": 0, "verify": 0, "seedverify": 0}   # all passes skipped
    done = next(e for e in events if e["event"] == "done")["result"]
    assert done["max_hops"] == 2


def test_default_config_runs_all_passes(conn, monkeypatch):
    monkeypatch.setattr(impact_mod, "_llm_call", _fake_llm)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    seen = {"refine": 0, "verify": 0}
    orig_r, orig_v = impact_mod._refinement_pass, impact_mod._verification_pass
    monkeypatch.setattr(impact_mod, "_refinement_pass",
                        lambda **k: seen.__setitem__("refine", 1) or orig_r(**k))
    monkeypatch.setattr(impact_mod, "_verification_pass",
                        lambda **k: seen.__setitem__("verify", 1) or orig_v(**k))
    list(impact_mod.run_impact_stream("global crude oil supply shock", conn=conn))
    assert seen == {"refine": 1, "verify": 1}   # defaults unchanged
```

- [ ] **Step 2: Run — fail** (`run_impact_stream` has no `max_hops`/`refine`/`verify` params).

- [ ] **Step 3: Add the params + apply them** in `api/impact.py`:

(a) Signature (line ~979):
```python
def run_impact_stream(
    text: str, *, conn: sqlite3.Connection, provider: Optional[str] = None,
    max_hops: Optional[int] = None, refine: bool = True, verify: bool = True,
):
```

(b) After the text-empty guard / near the top of the `try`, compute the effective hop count:
```python
        effective_max_hops = max_hops if max_hops is not None else MAX_HOPS
```

(c) Hop loop (line ~1170): change `for hop in range(1, MAX_HOPS + 1):` → `for hop in range(1, effective_max_hops + 1):`.

(d) Seed-verify gate (line ~1073): change `if commodity_summary and VERIFY_ENABLED and not _verify_seed_directness(` → `if commodity_summary and verify and VERIFY_ENABLED and not _verify_seed_directness(`.

(e) Refinement (line ~1328): gate it —
```python
        if refine:
            refinement_summary = _refinement_pass(
                text=text, impacts=impacts, seeds_block=seeds_block, conn=conn, debug_log=debug_log)
        else:
            refinement_summary = {"considered": 0, "rescored": 0, "applied": 0}
        yield {"event": "refinement",
               "updated": [v for v in impacts.values() if v.get("refined")],
               "summary": refinement_summary}
```

(f) Verification (line ~1342): change the gate to `if verify and VERIFY_ENABLED`:
```python
        verification_summary = (
            _verification_pass(text=text, impacts=impacts, seeds_block=seeds_block,
                               conn=conn, debug_log=debug_log)
            if (verify and VERIFY_ENABLED)
            else {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0}
        )
```

(g) Done result (line ~1372): change `"max_hops": MAX_HOPS,` → `"max_hops": effective_max_hops,`.

(h) `run_impact` wrapper (line ~1402): pass through —
```python
def run_impact(
    text: str, *, conn: sqlite3.Connection, provider: Optional[str] = None,
    max_hops: Optional[int] = None, refine: bool = True, verify: bool = True,
) -> dict[str, Any]:
    """Non-streaming wrapper: drain run_impact_stream, return the done payload."""
    final: dict[str, Any] = {}
    for ev in run_impact_stream(text, conn=conn, provider=provider,
                                max_hops=max_hops, refine=refine, verify=verify):
        if ev["event"] == "done":
            final = ev["result"]
    return final
```

- [ ] **Step 4: Run — pass.** `python -B -m pytest tests/test_impact_stream.py -v` (prior + 2 new all pass — defaults preserved so existing tests unaffected).

- [ ] **Step 5: Commit** — `git add api/impact.py tests/test_impact_stream.py && git commit -m "feat(impact): max_hops/refine/verify params for a lighter batch trace config"`

---

## Task 2: `event_impacts` table + store helpers

**Files:** Modify `schema/store.py`; Test `tests/test_events_store.py`.

- [ ] **Step 1: Failing tests** — append to `tests/test_events_store.py`:

```python
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
```

- [ ] **Step 2: Run — fail.**

- [ ] **Step 3: Add DDL + helpers** — append the `event_impacts` block to the `DDL` string in `schema/store.py`:
```sql
CREATE TABLE IF NOT EXISTS event_impacts (
    event_id   TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    direction  TEXT NOT NULL,
    magnitude  REAL NOT NULL,
    hop        INTEGER NOT NULL,
    reasoning  TEXT,
    PRIMARY KEY (event_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_event_impacts_node ON event_impacts(node_id);
```
And add helpers after `queued_events`:
```python
def write_event_impacts(conn: sqlite3.Connection, event_id: str, impacts: list[dict[str, Any]]) -> None:
    """Replace all impact rows for an event (delete-then-insert, so a re-trace is clean)."""
    conn.execute("DELETE FROM event_impacts WHERE event_id = ?", (event_id,))
    conn.executemany(
        "INSERT INTO event_impacts (event_id, node_id, direction, magnitude, hop, reasoning) "
        "VALUES (?,?,?,?,?,?)",
        [(event_id, v["node_id"], v.get("direction", "no_effect"),
          float(v.get("magnitude") or 0.0), int(v.get("hop") or 0), v.get("reasoning"))
         for v in impacts],
    )
    conn.commit()


def set_event_status(conn: sqlite3.Connection, event_id: str, status: str) -> None:
    conn.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
    conn.commit()
```

- [ ] **Step 4: Run — pass.** `python -B -m pytest tests/test_events_store.py -v` (prior + 2 new).

- [ ] **Step 5: Commit** — `git add schema/store.py tests/test_events_store.py && git commit -m "feat(v2): event_impacts table + write_event_impacts/set_event_status"`

---

## Task 3: Batch precompute runnable

**Files:** Create `pipeline/precompute_impacts.py`; Test `tests/test_precompute_impacts.py`; Modify `.env.example`.

- [ ] **Step 1: Failing tests** — `tests/test_precompute_impacts.py`:

```python
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
```

- [ ] **Step 2: Run — fail.**

- [ ] **Step 3: Implement** `pipeline/precompute_impacts.py`:

```python
"""So What? V2 · Phase 2 — batch impact precompute.

For each queued event, trace it with a lighter Claude config (2 hops, no
refinement/verification) and write per-node verdicts to event_impacts. Budgeted,
idempotent (only 'queued' events), restartable (per-event transaction).

    python -B -m pipeline.precompute_impacts [--retry-failed]
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path
from typing import Optional

from api import impact as _impact   # patched in tests via pc._impact.run_impact
from schema import store

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "econgraph.db"

PRECOMPUTE_MAX_EVENTS = int(os.environ.get("PRECOMPUTE_MAX_EVENTS", "25"))
PRECOMPUTE_WALLCLOCK_S = int(os.environ.get("PRECOMPUTE_WALLCLOCK_S", str(6 * 3600)))
BATCH_MAX_HOPS = int(os.environ.get("PRECOMPUTE_MAX_HOPS", "2"))


def run_precompute(db_path: Path = DB_PATH, *, max_events: int = PRECOMPUTE_MAX_EVENTS,
                   wallclock_s: int = PRECOMPUTE_WALLCLOCK_S,
                   provider: Optional[str] = None, retry_failed: bool = False) -> dict:
    conn = store.connect(db_path)
    store.init_db(conn)
    prov = provider or os.environ.get("IMPACT_LLM_PROVIDER", "claude")
    summary = {"processed": 0, "traced": 0, "failed": 0, "impacts_written": 0, "elapsed_s": 0.0}
    t0 = time.time()
    try:
        if retry_failed:
            conn.execute("UPDATE events SET status='queued' WHERE status='failed'")
            conn.commit()
        events = store.queued_events(conn)
        for ev in events:
            if summary["processed"] >= max_events or (time.time() - t0) >= wallclock_s:
                log.info("precompute: budget hit; deferring %d events", len(events) - summary["processed"])
                break
            summary["processed"] += 1
            try:
                r = _impact.run_impact(ev["headline"], conn=conn, provider=prov,
                                       max_hops=BATCH_MAX_HOPS, refine=False, verify=False)
            except Exception as exc:
                log.warning("precompute: %s trace raised %s", ev["id"], exc)
                store.set_event_status(conn, ev["id"], "failed")
                summary["failed"] += 1
                continue
            has_seeds = bool(r.get("seed")) or bool(r.get("seeds"))
            impacts = r.get("impacts") or []
            if not has_seeds or not impacts:
                store.set_event_status(conn, ev["id"], "failed")
                summary["failed"] += 1
                continue
            store.write_event_impacts(conn, ev["id"], impacts)
            store.set_event_status(conn, ev["id"], "traced")
            summary["traced"] += 1
            summary["impacts_written"] += len(impacts)
        summary["elapsed_s"] = round(time.time() - t0, 1)
        log.info("precompute: %s", summary)
        return summary
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Batch impact precompute for queued events.")
    ap.add_argument("--retry-failed", action="store_true", help="re-queue 'failed' events first")
    ap.add_argument("--max-events", type=int, default=PRECOMPUTE_MAX_EVENTS)
    args = ap.parse_args()
    s = run_precompute(max_events=args.max_events, retry_failed=args.retry_failed)
    print(f"precompute cycle: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run — pass.** `python -B -m pytest tests/test_precompute_impacts.py -v` (5 pass). Then `python -B -m pytest tests/test_events_store.py tests/test_ingest_news.py tests/test_impact_stream.py -q` (no regression).

- [ ] **Step 5: `.env.example`** — add under the V2 block:
```env
PRECOMPUTE_MAX_EVENTS=25       # events traced per precompute cycle
PRECOMPUTE_WALLCLOCK_S=21600   # hard stop (6h) — remaining events defer to next cycle
```

- [ ] **Step 6: Commit** — `git add pipeline/precompute_impacts.py tests/test_precompute_impacts.py .env.example && git commit -m "feat(v2): batch precompute runnable — trace queued events -> event_impacts"`

---

## Self-Review

- Engine params (max_hops/refine/verify) + all 4 apply-sites + wrapper passthrough → Task 1. ✓
- `event_impacts` table + `write_event_impacts` (delete-then-insert) + `set_event_status` → Task 2. ✓
- Budgeted (max_events + wallclock), idempotent (queued only), failure=defer, `--retry-failed` → Task 3. ✓
- Tests: param-gating + defaults-preserved (T1); write/replace + status (T2); trace/no-seed/raise/budget/idempotent (T3). ✓
- **Placeholder scan:** full code every step; the anchor line numbers are approximate — the implementer matches on the shown code, not the number. ✓
- **Naming consistency:** `max_hops`/`refine`/`verify`, `effective_max_hops`, `write_event_impacts`/`set_event_status`, `run_precompute`, `_impact.run_impact`, budget names consistent across tasks + tests. ✓
- Note: P2 keeps seeds in `event_impacts` (a seed IS an impact on its own node); P3 will decay/aggregate. No `node_impact` yet (P3).
