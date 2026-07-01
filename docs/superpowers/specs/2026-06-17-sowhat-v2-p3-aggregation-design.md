# So What? V2 · Phase 3 — Impact Aggregation (Design)

**Date:** 2026-06-17
**Status:** Approved autonomously (user delegated P2+P3 build)
**Parent:** [`2026-06-17-sowhat-v2-architecture.md`](2026-06-17-sowhat-v2-architecture.md)
**Branch:** `feat/sowhat-v2`

---

## Goal

Roll the per-event verdicts in `event_impacts` (from P2) into a per-node **combined
impact** in a new `node_impact` table: for each node, the recency-decayed net of every
recent event that touched it or its ecosystem, plus the top contributing events. This
is what powers "select a node → see the combined impact of everything that hit it"
(vision point 4). Fully rebuildable from `event_impacts` — a derived cache.

## Locked / judgment-call decisions
- **7-day rolling window + recency decay.** Only events within `IMPACT_WINDOW_DAYS`
  (default 7) count. Each event's contribution is weighted `w = 0.5 ** (age_days /
  IMPACT_HALFLIFE_DAYS)` (halflife default 3). Age from the event's `published_at`
  (fallback `ingested_at`).
- **Netting mirrors `_merge_impact_results`** (the existing multi-event merge): sum
  positive weighted-mass and negative weighted-mass separately; net direction = the
  larger mass; net magnitude = `min(1, |pos − neg|)`; `mixed_signals = pos>0 and neg>0`
  (with a small floor so mixed nodes stay visible). P3 adds the **recency weight** to
  each contribution and a decayed-window filter — otherwise identical semantics.
- **"Ecosystem = any hop"** falls out for free: `event_impacts` already has one row per
  (event, node) at whatever hop the node was reached, with the engine's hop-attenuated
  magnitude. Aggregating all rows for a node = its combined impact across every event
  that touched it directly or downstream.
- **`unscored` / `no_effect` rows contribute 0 mass** (they carry no directional
  signal) but DO count toward `event_count` context. Seeds (hop 0) count normally.
- **Deterministic rebuild.** `aggregate_impacts` wipes and recomputes all of
  `node_impact` from `event_impacts` each run — no incremental drift.

## New table (`schema/store.py`)

```sql
CREATE TABLE IF NOT EXISTS node_impact (
    node_id       TEXT PRIMARY KEY,
    direction     TEXT NOT NULL,        -- net: positive|negative|no_effect
    magnitude     REAL NOT NULL,        -- net, decayed, clamped 0-1
    mixed_signals INTEGER NOT NULL DEFAULT 0,
    event_count   INTEGER NOT NULL,     -- # distinct recent events touching this node
    top_events    TEXT NOT NULL DEFAULT '[]',  -- JSON: top contributors
    computed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_impact_direction ON node_impact(direction);
```

`top_events` JSON entries: `{event_id, headline, direction, magnitude, weighted, hop,
published_at}` — the top `TOP_EVENTS_PER_NODE` (default 5) by `|weighted|`, so the UI can
show "driven by: <event> (−0.6), <event> (+0.3)…".

Store helper: `replace_node_impact(conn, rows)` — `DELETE FROM node_impact; executemany
INSERT` in one transaction (atomic swap of the derived cache).

## Aggregation (`pipeline/aggregate_impacts.py`)

`python -B -m pipeline.aggregate_impacts`:
1. Join `event_impacts` → `events` to get each impact row's `published_at`/`ingested_at`
   + `headline`, filtered to events within the window (`WHERE date >= today - WINDOW`).
2. Group by `node_id`. For each node, over its rows:
   - `w = 0.5 ** (age_days / HALFLIFE)`; `contrib = w * magnitude`.
   - `pos_mass += contrib` if direction=positive; `neg_mass += contrib` if negative;
     (no_effect/unscored add 0 but increment `event_count`).
   - collect `(event_id, headline, direction, magnitude, weighted=contrib, hop, published_at)`.
   - net: `direction = positive|negative|no_effect` by larger mass; `magnitude =
     round(min(1, abs(pos-neg)), 3)`; `mixed = pos>0 and neg>0` (magnitude floored to
     0.15 when mixed and below).
   - `top_events` = top-5 contributions by `abs(weighted)`.
3. `replace_node_impact(conn, rows)`.
4. Print summary: `{nodes, positive, negative, mixed, events_in_window}`.
- Config: `IMPACT_WINDOW_DAYS=7`, `IMPACT_HALFLIFE_DAYS=3`, `TOP_EVENTS_PER_NODE=5`.
- **`today` is injectable** (param, default `date.today()`) so tests are deterministic.

## Testing (deterministic — temp SQLite, hand-built event_impacts + events)
- **Netting:** node with 2 recent events (one +0.8, one −0.3, same day) → direction
  positive, magnitude ≈ 0.5, mixed_signals True, event_count 2.
- **Recency decay:** same-magnitude event today vs 6 days ago → today's `weighted` is
  larger; a node touched only by a same-mag +event today outranks one touched 6 days ago.
- **Window filter:** an event 10 days old (outside the 7-day window) contributes nothing
  (not in `event_count`, not in `top_events`).
- **no_effect/unscored:** a node touched only by `no_effect` rows → net `no_effect`,
  magnitude 0, but `event_count` counts them.
- **top_events:** node with 7 contributing events → `top_events` has exactly 5, ordered
  by `|weighted|` desc, each with headline+direction+published_at.
- **Deterministic rebuild:** running twice yields identical `node_impact`; editing one
  `event_impacts` row and re-running reflects the change (no stale rows).
- **`replace_node_impact`:** atomic wipe+insert; a node no longer touched drops out.

Tests pass an explicit `today` and hand-insert `events` + `event_impacts` rows; no LLM,
no network, no full `econgraph.db`.

## Files touched
| File | Change |
|---|---|
| `schema/store.py` | `node_impact` DDL + `replace_node_impact` helper |
| `pipeline/aggregate_impacts.py` | NEW — the decayed-window rollup |
| `tests/test_aggregate_impacts.py` | NEW — the deterministic tests above |
| `.env.example` | `IMPACT_WINDOW_DAYS`, `IMPACT_HALFLIFE_DAYS`, `TOP_EVENTS_PER_NODE` |

No change to P1/P2, the graph, or V1 endpoints. `node_impact` is a derived, additive
cache.

## Out of scope
Serving `node_impact` via the API + the 12h scheduler (P4); frontend (P5).
