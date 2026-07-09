# Usage statistics tab — design

**Date:** 2026-07-07
**Status:** approved (design), building autonomously

## Goal

A "Usage" tab in the SoWhat frontend showing LLM token utilization over time as a
bar chart, with an Hourly / Daily / Weekly granularity toggle. Bars stack input /
output / cache-read tokens; an overlaid line shows cost per bucket.

## Feasibility (verified)

The Claude CLI is invoked with `--output-format json`; `_claude_call` in
`api/impact.py` parses the envelope but discards its `usage` block. That block
carries EXACT counts — `input_tokens`, `output_tokens`, `cache_read_input_tokens`
(and `cache_creation_input_tokens`) — plus `total_cost_usd`. So usage is precise,
not estimated. All LLM traffic (ingest, precompute, on-demand) funnels through the
single `_claude_call`, so instrumenting it once captures everything.

No historical data exists (never recorded) → the chart fills going forward.

## Components

### 1. Store (`schema/store.py`)
- Table `llm_usage(id INTEGER PK, ts TEXT NOT NULL DEFAULT (datetime('now')),
  model TEXT, input_tokens INT NOT NULL DEFAULT 0, output_tokens INT NOT NULL
  DEFAULT 0, cache_read_tokens INT NOT NULL DEFAULT 0, cost_usd REAL NOT NULL
  DEFAULT 0)`. Index on `ts`. Created in DDL + idempotent `_migrate_llm_usage`.
- `record_llm_usage(usage: dict, db_path=None)` — opens its own short-lived
  connection (default Public DB), inserts one row. Fully fail-safe: any exception
  is swallowed (recording must NEVER break a trace). `usage` keys:
  `input_tokens, output_tokens, cache_read_tokens, cost_usd, model`.
- `usage_buckets(conn, granularity, since_days) -> list[dict]` — aggregates with
  strftime: hour `'%Y-%m-%dT%H:00'`, day `'%Y-%m-%d'`, week `'%Y-W%W'`. Filters
  `ts >= datetime('now', '-<since_days> days')`. Returns per bucket:
  `{bucket, input_tokens, output_tokens, cache_read_tokens, cost_usd, calls}`,
  ordered by bucket ascending. `granularity` validated to hour|day|week.
- `prune_llm_usage(conn, older_than_days=180)` — folded into run_cycle prune.

### 2. Capture + API
- `api/impact.py` `_claude_call`: after a successful envelope parse (returncode 0,
  not is_error), read `envelope.get("usage") or {}` + `envelope.get("total_cost_usd")`,
  map to the record dict, and call `store.record_llm_usage(...)`. Import is local
  (inside the function) to avoid a circular import and to keep the failure isolated.
- `api/main.py`: `GET /usage?granularity=day&days=30` → `{granularity, days,
  buckets: [...]}`. `granularity` default `day`, validated; `days` default 30,
  clamped to [1, 365].

### 3. Frontend
- `web/index.html`: a third `.sidebar-tab` (`data-tab="usage"`, label "Usage") and
  `<div id="panel-usage" class="sidebar-panel" hidden>` containing a granularity
  segmented control (Hourly/Daily/Weekly) and a chart mount `<div id="usage-chart">`.
- `web/src/api.ts`: `getUsage(granularity, days)` → typed `UsageResponse`.
- `web/src/ui/usageChart.ts`: dependency-free SVG renderer.
  - Pure helper `layoutBars(buckets, width, height) -> BarGeom[]` (unit-tested):
    computes x/width per bucket and stacked y/heights for input/output/cache scaled
    to the max total; plus cost-line points on a secondary scale. No DOM.
  - `renderUsageChart(root, resp)` builds the SVG from `layoutBars`: stacked rects
    (input, output, cache-read) per bar in the palette, a cost polyline + right-axis
    ticks, left-axis token ticks, x-axis bucket labels (thinned to avoid overlap),
    and a hover tooltip (title/overlay) showing exact tokens + calls + cost.
    Empty state: a "No usage recorded yet" message.
- `web/src/main.ts`: extend `wireSidebarTabs` to toggle `#panel-usage` and, on
  activation, fetch + render; wire the granularity toggle to re-fetch + re-render.

### Colors (reuse the app palette)
input → teal, output → coral, cache-read → gray (faint), cost line → blue.

## Data flow
```
_claude_call (any source) -> record_llm_usage -> llm_usage (Public DB)
Usage tab open / toggle -> GET /usage?granularity -> usage_buckets -> JSON
                        -> layoutBars -> SVG (stacked bars + cost line)
```

## Testing
- store: `usage_buckets` aggregates fixed-timestamp rows into correct hour/day/week
  buckets with summed tokens/cost/calls; `record_llm_usage` inserts + is fail-safe on
  a bad path; migration adds the table on an old DB.
- api: `/usage` returns buckets for seeded rows; validates granularity; clamps days.
- web: `layoutBars` geometry (bar count, stacked heights sum to total height at max
  bucket, empty input) via vitest.
- live: seed a usage row, open the Usage tab, confirm bars + cost line render;
  screenshot.

## Files
`schema/store.py`, `api/impact.py`, `api/main.py`, `pipeline/run_cycle.py`,
`web/index.html`, `web/src/api.ts`, `web/src/ui/usageChart.ts`, `web/src/main.ts`,
+ tests (`tests/test_events_store.py` or new `tests/test_usage.py`, `tests/test_api.py`,
`web/src/__tests__/usage-chart.test.ts`).

## Non-goals (v1)
Per-pipeline-stage breakdown (ingest/precompute/on-demand); provider != claude
(Ollama has no cost/usage envelope — records zeros, harmless). Recording schema
leaves room to add a `source` column later without migration pain.
