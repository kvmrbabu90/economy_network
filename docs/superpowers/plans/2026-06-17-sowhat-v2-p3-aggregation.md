# So What? V2 · Phase 3 — Impact Aggregation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Roll `event_impacts` (P2) into a per-node `node_impact` combined verdict over a 7-day recency-decayed window, with top contributing events; fully rebuildable.

**Architecture:** New `node_impact` table + `replace_node_impact` helper; `pipeline/aggregate_impacts.py` joins `event_impacts`→`events`, applies recency decay, nets positive/negative mass (mirroring `_merge_impact_results`), and writes the rollup. `today` is injectable for deterministic tests.

**Tech Stack:** Python 3.11 / sqlite3 / pytest. **Branch:** `feat/sowhat-v2`. `python -B`. **Spec:** `docs/superpowers/specs/2026-06-17-sowhat-v2-p3-aggregation-design.md`.

---

## Task 1: `node_impact` table + `replace_node_impact`

**Files:** Modify `schema/store.py`; Test `tests/test_events_store.py`.

- [ ] **Step 1: Failing test** — append to `tests/test_events_store.py`:

```python
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
```

- [ ] **Step 2: Run — fail.**

- [ ] **Step 3: Add DDL + helper** — append the `node_impact` block to the `DDL` string in `schema/store.py`:
```sql
CREATE TABLE IF NOT EXISTS node_impact (
    node_id       TEXT PRIMARY KEY,
    direction     TEXT NOT NULL,
    magnitude     REAL NOT NULL,
    mixed_signals INTEGER NOT NULL DEFAULT 0,
    event_count   INTEGER NOT NULL,
    top_events    TEXT NOT NULL DEFAULT '[]',
    computed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_impact_direction ON node_impact(direction);
```
And add after `set_event_status`:
```python
def replace_node_impact(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Atomically swap the derived node_impact cache: wipe, then bulk-insert `rows`."""
    conn.execute("DELETE FROM node_impact")
    conn.executemany(
        "INSERT INTO node_impact (node_id, direction, magnitude, mixed_signals, "
        "event_count, top_events, computed_at) VALUES (?,?,?,?,?,?,?)",
        [(r["node_id"], r["direction"], float(r["magnitude"]), int(r.get("mixed_signals", 0)),
          int(r["event_count"]), r.get("top_events", "[]"), r["computed_at"]) for r in rows],
    )
    conn.commit()
```

- [ ] **Step 4: Run — pass.** `python -B -m pytest tests/test_events_store.py -v` (prior + 1 new). `python -B -m pytest tests/test_schema.py -q` (unaffected).

- [ ] **Step 5: Commit** — `git add schema/store.py tests/test_events_store.py && git commit -m "feat(v2): node_impact table + replace_node_impact atomic-swap helper"`

---

## Task 2: Aggregation runnable

**Files:** Create `pipeline/aggregate_impacts.py`; Test `tests/test_aggregate_impacts.py`; Modify `.env.example`.

- [ ] **Step 1: Failing tests** — `tests/test_aggregate_impacts.py`:

```python
from __future__ import annotations
import json
from datetime import date
from schema import store
from pipeline import aggregate_impacts as agg


def _db(tmp_path):
    conn = store.connect(tmp_path / "g.db"); store.init_db(conn)
    return conn


def _event(conn, eid, published_at):
    store.insert_event(conn, {"id": eid, "headline": f"H {eid}", "source": "SEC 8-K",
                              "url": f"u/{eid}", "category": "m&a", "published_at": published_at,
                              "seed_entity": "E", "seed_node_id": "cik:1", "status": "traced"})


def test_nets_positive_and_negative_with_mixed_flag(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17"); _event(conn, "e2", "2026-06-17")
    store.write_event_impacts(conn, "e1", [{"node_id": "cik:9", "direction": "positive", "magnitude": 0.8, "hop": 1}])
    store.write_event_impacts(conn, "e2", [{"node_id": "cik:9", "direction": "negative", "magnitude": 0.3, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT * FROM node_impact WHERE node_id='cik:9'").fetchone()
    assert r["direction"] == "positive"
    assert abs(r["magnitude"] - 0.5) < 1e-6          # |0.8 - 0.3|, same-day weight = 1.0
    assert r["mixed_signals"] == 1 and r["event_count"] == 2


def test_recency_decay_favors_newer(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "new", "2026-06-17"); _event(conn, "old", "2026-06-11")   # 6 days old
    store.write_event_impacts(conn, "new", [{"node_id": "a", "direction": "positive", "magnitude": 0.5, "hop": 1}])
    store.write_event_impacts(conn, "old", [{"node_id": "b", "direction": "positive", "magnitude": 0.5, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17), halflife=3.0)
    a = conn.execute("SELECT magnitude FROM node_impact WHERE node_id='a'").fetchone()["magnitude"]
    b = conn.execute("SELECT magnitude FROM node_impact WHERE node_id='b'").fetchone()["magnitude"]
    assert a > b                                      # newer event weighted higher


def test_window_excludes_stale(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "old", "2026-06-01")                 # 16 days old, outside 7-day window
    store.write_event_impacts(conn, "old", [{"node_id": "z", "direction": "negative", "magnitude": 0.9, "hop": 0}])
    agg.aggregate(conn, today=date(2026, 6, 17), window_days=7)
    assert conn.execute("SELECT COUNT(*) c FROM node_impact WHERE node_id='z'").fetchone()["c"] == 0


def test_no_effect_counts_but_zero_mass(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17")
    store.write_event_impacts(conn, "e1", [{"node_id": "n", "direction": "no_effect", "magnitude": 0.0, "hop": 2}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    r = conn.execute("SELECT * FROM node_impact WHERE node_id='n'").fetchone()
    assert r["direction"] == "no_effect" and r["magnitude"] == 0.0 and r["event_count"] == 1


def test_top_events_capped_and_ordered(tmp_path):
    conn = _db(tmp_path)
    for i in range(7):
        _event(conn, f"e{i}", "2026-06-17")
        store.write_event_impacts(conn, f"e{i}", [{"node_id": "hub", "direction": "negative",
                                                   "magnitude": (i + 1) / 10, "hop": 1}])
    agg.aggregate(conn, today=date(2026, 6, 17), top_n=5)
    top = json.loads(conn.execute("SELECT top_events FROM node_impact WHERE node_id='hub'").fetchone()["top_events"])
    assert len(top) == 5
    mags = [abs(t["weighted"]) for t in top]
    assert mags == sorted(mags, reverse=True)         # ordered by |weighted| desc


def test_rebuild_is_deterministic_and_drops_stale_nodes(tmp_path):
    conn = _db(tmp_path)
    _event(conn, "e1", "2026-06-17")
    store.write_event_impacts(conn, "e1", [{"node_id": "x", "direction": "positive", "magnitude": 0.5, "hop": 0}])
    agg.aggregate(conn, today=date(2026, 6, 17))
    first = conn.execute("SELECT node_id, direction, magnitude FROM node_impact ORDER BY node_id").fetchall()
    agg.aggregate(conn, today=date(2026, 6, 17))       # rerun
    second = conn.execute("SELECT node_id, direction, magnitude FROM node_impact ORDER BY node_id").fetchall()
    assert [tuple(r) for r in first] == [tuple(r) for r in second]
```

- [ ] **Step 2: Run — fail.**

- [ ] **Step 3: Implement** `pipeline/aggregate_impacts.py`:

```python
"""So What? V2 · Phase 3 — impact aggregation.

Roll event_impacts into a per-node combined verdict (node_impact) over a recency-
decayed rolling window. Fully rebuildable from event_impacts.

    python -B -m pipeline.aggregate_impacts
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional

from schema import store

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "econgraph.db"

IMPACT_WINDOW_DAYS = int(os.environ.get("IMPACT_WINDOW_DAYS", "7"))
IMPACT_HALFLIFE_DAYS = float(os.environ.get("IMPACT_HALFLIFE_DAYS", "3"))
TOP_EVENTS_PER_NODE = int(os.environ.get("TOP_EVENTS_PER_NODE", "5"))


def _age_days(row_date: Optional[str], today: date) -> Optional[int]:
    if not row_date:
        return None
    try:
        return (today - date.fromisoformat(str(row_date)[:10])).days
    except Exception:
        return None


def aggregate(conn, *, today: Optional[date] = None, window_days: int = IMPACT_WINDOW_DAYS,
              halflife: float = IMPACT_HALFLIFE_DAYS, top_n: int = TOP_EVENTS_PER_NODE) -> dict:
    today = today or date.today()
    rows = conn.execute(
        """
        SELECT ei.node_id, ei.event_id, ei.direction, ei.magnitude, ei.hop,
               e.headline, e.published_at, e.ingested_at
        FROM event_impacts ei JOIN events e ON e.id = ei.event_id
        """
    ).fetchall()

    acc: dict[str, dict] = {}
    for r in rows:
        age = _age_days(r["published_at"] or r["ingested_at"], today)
        if age is None or age > window_days:
            continue
        w = 0.5 ** (max(0, age) / halflife)
        mag = float(r["magnitude"] or 0.0)
        contrib = w * mag
        direction = r["direction"]
        a = acc.setdefault(r["node_id"], {"pos": 0.0, "neg": 0.0, "count": 0, "events": []})
        a["count"] += 1
        signed = 0.0
        if direction == "positive":
            a["pos"] += contrib; signed = contrib
        elif direction == "negative":
            a["neg"] += contrib; signed = -contrib
        a["events"].append({"event_id": r["event_id"], "headline": r["headline"],
                            "direction": direction, "magnitude": round(mag, 3),
                            "weighted": round(signed, 4), "hop": r["hop"],
                            "published_at": r["published_at"]})

    computed_at = today.isoformat() + "T00:00:00"
    out = []
    for node_id, a in acc.items():
        pos, neg = a["pos"], a["neg"]
        mixed = 1 if (pos > 0 and neg > 0) else 0
        if pos > neg:
            direction, magnitude = "positive", min(1.0, pos - neg)
        elif neg > pos:
            direction, magnitude = "negative", min(1.0, neg - pos)
        else:
            direction, magnitude = "no_effect", 0.0
        if mixed and 0 < magnitude < 0.15:
            magnitude = 0.15
        top = sorted(a["events"], key=lambda e: -abs(e["weighted"]))[:top_n]
        out.append({"node_id": node_id, "direction": direction, "magnitude": round(magnitude, 3),
                    "mixed_signals": mixed, "event_count": a["count"],
                    "top_events": json.dumps(top), "computed_at": computed_at})

    store.replace_node_impact(conn, out)
    summary = {"nodes": len(out),
               "positive": sum(1 for r in out if r["direction"] == "positive"),
               "negative": sum(1 for r in out if r["direction"] == "negative"),
               "mixed": sum(1 for r in out if r["mixed_signals"]),
               "events_in_window": len({e["event_id"] for a in acc.values() for e in a["events"]})}
    log.info("aggregate: %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = store.connect(DB_PATH)
    store.init_db(conn)
    try:
        s = aggregate(conn)
    finally:
        conn.close()
    print(f"aggregate cycle: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run — pass.** `python -B -m pytest tests/test_aggregate_impacts.py -v` (6 pass). Then `python -B -m pytest tests/test_events_store.py tests/test_precompute_impacts.py -q` (no regression).

- [ ] **Step 5: `.env.example`** — add under the V2 block:
```env
IMPACT_WINDOW_DAYS=7           # combined-impact rolling window
IMPACT_HALFLIFE_DAYS=3         # recency decay half-life
TOP_EVENTS_PER_NODE=5          # contributors kept per node
```

- [ ] **Step 6: Commit** — `git add pipeline/aggregate_impacts.py tests/test_aggregate_impacts.py .env.example && git commit -m "feat(v2): aggregate event_impacts -> node_impact (decayed window + top events)"`

---

## Self-Review

- `node_impact` table + `replace_node_impact` atomic swap → Task 1. ✓
- Decayed-window rollup, net pos/neg mass + mixed flag (mirrors `_merge_impact_results`), recency weight, top-N events, deterministic rebuild → Task 2. ✓
- Tests: netting+mixed, recency decay, window exclusion, no_effect zero-mass, top_events cap/order, deterministic rebuild → Task 2 Step 1. ✓
- `today` injectable for deterministic tests. ✓
- **Placeholder scan:** full code every step. ✓
- **Naming consistency:** `replace_node_impact`, `aggregate`, `_age_days`, `IMPACT_WINDOW_DAYS`/`IMPACT_HALFLIFE_DAYS`/`TOP_EVENTS_PER_NODE`, `top_events`/`weighted`/`event_count`/`mixed_signals` consistent with the spec, the `node_impact` DDL, and tests. ✓
- Note: aggregation is a pure rebuild from `event_impacts` (+ `events` dates); no dependency on P4/P5.
