# So What? V2 · Phase 4 — Serving + Scheduler (Design)

**Date:** 2026-06-30
**Status:** Approved autonomously (user delegated P4+P5 build, "make judgement calls")
**Parent:** [`2026-06-17-sowhat-v2-architecture.md`](2026-06-17-sowhat-v2-architecture.md)
**Branch:** `feat/sowhat-v2`

---

## Goal

Serve the precomputed `node_impact` cache (built by P1→P2→P3) over the API with
**zero request-time LLM calls**, and provide an orchestrator that runs the full
ingest→precompute→aggregate cycle so it can be scheduled every 12h. The existing
`POST /impact/stream` is untouched — it remains the on-demand "sharpen with Claude"
action layered on top of the warm baseline.

## Locked / judgment-call decisions

- **Two read-only endpoints** (both read `node_impact`; no LLM, no compute):
  - `GET /impact/live` — the full precomputed node→verdict map for graph tinting.
    Compact rows only (`node_id`, `direction`, `magnitude`, `mixed_signals`,
    `event_count`) so the frontend can tint many nodes from one small payload.
    Includes a top-level `computed_at` freshness stamp.
  - `GET /node/{node_id:path}/impact` — the combined verdict + contributing events
    for one node (powers P5's node-click panel).
- **`top_events` enriched at query time.** P3 stores each contributing event as
  `{event_id, headline, direction, magnitude, weighted, hop, published_at}`. The
  P5 timeline needs each event to link to its source, so `/node/{id}/impact`
  **joins each `event_id` back to the `events` table** to add `url` and `source`.
  This keeps `events` the single source of truth for URLs and requires **no change
  to P3** or a re-aggregation. Missing/rolled-off events are tolerated (url/source
  become `null`).
- **Missing-node vs no-impact are distinct.** `/node/{id}/impact` on a node that
  does not exist → **404**. A node that exists but has no `node_impact` row (no
  recent event touched it) → **200** with `impact: null` (+ resolved `name`/`type`).
  The frontend renders "no recent impact" rather than an error.
- **Alias resolution reused.** `/node/{id}/impact` resolves `node_id` through the
  existing `query.resolve_id` (canonical id or alias), matching every other `/node`
  route.
- **Route ordering.** `/node/{node_id:path}/impact` must be declared **before** the
  catch-all `GET /node/{node_id:path}` (same constraint the existing `/ego` route
  documents), else the catch-all shadows it.
- **Plain dicts, no Pydantic response models.** Matches the current API layer
  (`query.py` returns graphology dicts; endpoints return plain dicts).
- **Orchestrator over a heavyweight scheduler.** No new dependency (no APScheduler).
  - `pipeline/run_cycle.py` — one-shot orchestrator: runs `ingest_news.run_ingest`
    → `precompute_impacts.run_precompute` → `aggregate_impacts.aggregate` in one
    process against one DB, with **per-stage error isolation** (a stage that raises
    is logged and recorded in the summary; later stages still run, because each
    reads the prior stage's persisted rows — precompute works off whatever is
    queued, aggregate works off whatever is traced). Returns a combined summary.
    Idempotent and safe to re-run (precompute skips already-traced; aggregate is a
    deterministic rebuild).
  - `pipeline/scheduler.py` — thin unattended wrapper: `--once` runs a single
    `run_cycle`; default loops `run_cycle` then sleeps `SCHEDULER_INTERVAL_S`
    (default 43200 = 12h). No new deps — just `time.sleep`.
  - **Recommended deployment:** an OS scheduler (Windows Task Scheduler / cron)
    invoking `python -B -m pipeline.run_cycle` every 12h — more robust on a
    workstation than a long-lived Python sleep loop. `scheduler.py` is the
    zero-config fallback. This is documented in the spec + `.env.example`.
- **Freshness observable.** `/health` gains `node_impact_rows` and
  `node_impact_computed_at` (cheap, lets an operator confirm the cycle ran).

## New store read helpers (`schema/store.py`)

```python
def read_all_node_impact(conn) -> list[dict]:
    """Compact rows for graph tinting: node_id, direction, magnitude,
    mixed_signals, event_count. Ordered by node_id for determinism."""

def read_node_impact(conn, node_id: str) -> Optional[dict]:
    """Full row for one node (top_events still a JSON string) or None."""

def latest_node_impact_computed_at(conn) -> Optional[str]:
    """MAX(computed_at) across node_impact, or None when empty."""
```

## Endpoint response shapes

`GET /impact/live`:
```json
{
  "computed_at": "2026-06-30T00:00:00",
  "count": 42,
  "impacts": [
    {"node_id": "cik:0000320193", "direction": "negative",
     "magnitude": 0.62, "mixed_signals": 0, "event_count": 3}
  ]
}
```
Empty cache → `{"computed_at": null, "count": 0, "impacts": []}`.

`GET /node/{node_id:path}/impact`:
```json
{
  "node_id": "cik:0000320193",
  "name": "Apple Inc.",
  "type": "Company",
  "impact": {
    "direction": "negative", "magnitude": 0.62, "mixed_signals": 0,
    "event_count": 3, "computed_at": "2026-06-30T00:00:00",
    "top_events": [
      {"event_id": "…", "headline": "…", "direction": "negative",
       "magnitude": 0.7, "weighted": -0.7, "hop": 1,
       "published_at": "2026-06-28", "url": "https://…", "source": "SEC 8-K"}
    ]
  }
}
```
Node exists, no impact → `impact: null`. Unknown node → HTTP 404.

## `run_cycle` shape

```python
def run_cycle(db_path=DB_PATH, *, provider=None) -> dict:
    # returns {"ingest": {...|error}, "precompute": {...|error},
    #          "aggregate": {...|error}, "ok": bool, "elapsed_s": float}
```
Each stage wrapped so an exception becomes `{"error": "<repr>"}` in its slot and
sets `ok=False`, without aborting the remaining stages.

## Testing (deterministic — FastAPI TestClient + temp SQLite, no LLM/network)

- **`/impact/live`**: seed 2 `node_impact` rows → correct compact shape, count,
  `computed_at`; empty DB → count 0, `computed_at` null.
- **`/node/{id}/impact`**: seed a `node_impact` row whose `top_events` reference two
  `events` rows (one with url/source, one whose event was rolled off) → response has
  `name`/`type`, verdict fields, and `top_events` enriched with url/source (null for
  the missing event). Node with no impact row → `impact: null`. Unknown node → 404.
  Alias id resolves to the canonical node's impact.
- **`/health`**: includes `node_impact_rows` + `node_impact_computed_at`.
- **store helpers**: `read_all_node_impact` ordering + fields; `read_node_impact`
  hit/miss; `latest_node_impact_computed_at` value/None.
- **`run_cycle`**: monkeypatch the three stage functions to record call order and
  return sentinels → summary aggregates all three, order is ingest→precompute→
  aggregate, `ok=True`. Make precompute raise → its slot holds an error, aggregate
  still runs, `ok=False`.
- **`scheduler --once`**: monkeypatch `run_cycle` → called exactly once, no sleep.

## Files touched

| File | Change |
|---|---|
| `schema/store.py` | `read_all_node_impact`, `read_node_impact`, `latest_node_impact_computed_at` |
| `api/main.py` | `GET /impact/live`, `GET /node/{id}/impact` (before catch-all), `/health` freshness |
| `pipeline/run_cycle.py` | NEW — one-shot orchestrator |
| `pipeline/scheduler.py` | NEW — thin loop / `--once` wrapper |
| `tests/test_api_impact_live.py` | NEW — endpoint tests |
| `tests/test_run_cycle.py` | NEW — orchestrator + scheduler tests |
| `.env.example` | `SCHEDULER_INTERVAL_S` + Task Scheduler / cron note |

No change to the graph, V1 endpoints, `/impact/stream`, or P1–P3 code.

## Out of scope
Frontend consumption of these endpoints (P5); sub-12h/real-time updates;
authn/rate-limiting on the read endpoints (localhost-only tool).
