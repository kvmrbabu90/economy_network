# So What? V2 · Phase 5 — Frontend V2 (Design)

**Date:** 2026-06-30
**Status:** Approved autonomously (user delegated P4+P5 build, "make judgement calls")
**Parent:** [`2026-06-17-sowhat-v2-architecture.md`](2026-06-17-sowhat-v2-architecture.md)
**Branch:** `feat/sowhat-v2`

---

## Goal

Make the graph **warm on open**: pre-tint every visible node from the precomputed
`GET /impact/live` map (no LLM at load), and turn a node click into its
**combined-impact story** — the netted verdict, when it was computed, and a timeline
of the recent events that drove it (each linking to its source) — with a
"sharpen with Claude" button that re-runs the full V1 streaming trace on the
strongest contributing event. V1 on-demand tracing is retained as that sharpen layer.

## Locked / judgment-call decisions

- **Pre-tint pass on load, additive and non-blocking.** After the core graph finishes
  loading, call `getImpactLive()` once, build `Map<node_id, LiveImpact>`, and tint
  every node currently in the graphology instance that has an entry. It runs after
  the graph is interactive and never blocks first paint; if the call fails or the
  cache is empty, the graph simply renders untinted (log + move on). Nodes not in the
  live map keep their default color.
- **Combined verdict tinting = hop-0 intensity.** A `node_impact` row is a *netted*
  verdict with no hop. Tint it at full intensity by `magnitude` + `direction`, reusing
  the existing tier/palette logic (`pickTier` + `TIERS`). `mixed_signals` selects the
  **mixed (amber)** palette regardless of net direction, matching the on-demand
  mixed-node treatment. `no_effect`/magnitude 0 → neutral (no tint / default).
  Implemented as a small `tintColorForCombined({direction, magnitude, mixed_signals})`
  in `impact.ts` that reuses `pickTier`/`TIERS` — no duplication of the palette.
- **Combined-impact panel is a distinct section in the inspector**, below the node
  detail, clearly labeled "Combined impact · precomputed" so it's not confused with a
  live on-demand trace. It fetches `GET /node/{id}/impact` asynchronously on node
  click (same async pattern as the existing Describe button): render the node detail
  synchronously, then patch in the combined-impact section when the fetch resolves.
- **Timeline of contributing events.** From `impact.top_events`, render each as a row:
  direction dot + magnitude, headline, `published_at`, and `source` as a link to `url`
  (opens in a new tab; rows with a null url render as plain text — rolled-off events).
  Ordered as returned (already sorted by `|weighted|` desc). Show at most what the API
  returns (top 5).
- **Freshness line.** The panel header shows `computed_at` as a human "as of <date>"
  line so the user knows how warm the baseline is.
- **No-impact and unknown states.** Node exists but `impact: null` → the section shows
  "No recent impact." A 404 (shouldn't happen for a node already in the graph) is
  caught and the section is simply omitted.
- **"Sharpen with Claude" button.** One button in the combined-impact panel. It takes
  the **strongest contributing event** (`top_events[0]`) headline and runs it through
  the existing `runImpactStream(...)` full-strength trace, reusing the current
  streaming impact UI (the same code path the on-demand feature already uses). Disabled
  when there are no contributing events. This is the architecture's "re-run with
  Claude" — the baseline is fast; the sharpen is on-demand and full-strength.
- **No framework churn.** Stay within the existing vanilla-TS + Sigma/three.js + the
  project's DOM-building idiom (whatever `inspector.ts` already uses — `el()` helper /
  template strings / `createElement`). No new runtime dependencies.

## New API client (`web/src/api.ts`)

```ts
export interface LiveImpact {
  node_id: string; direction: string; magnitude: number;
  mixed_signals: number; event_count: number;
}
export interface ImpactLiveResponse { computed_at: string | null; count: number; impacts: LiveImpact[]; }

export interface TopEvent {
  event_id: string; headline: string; direction: string; magnitude: number;
  weighted: number; hop: number; published_at: string | null;
  url: string | null; source: string | null;
}
export interface NodeImpact {
  direction: string; magnitude: number; mixed_signals: number;
  event_count: number; computed_at: string; top_events: TopEvent[];
}
export interface NodeImpactResponse {
  node_id: string; name: string; type: string | null; impact: NodeImpact | null;
}

export function getImpactLive(): Promise<ImpactLiveResponse>        // GET /impact/live
export function getNodeImpact(id: string): Promise<NodeImpactResponse>  // GET /node/{id}/impact
```
Both use the existing `get<T>(path, params?)` helper.

## Components touched

| File | Change |
|---|---|
| `web/src/api.ts` | `getImpactLive`, `getNodeImpact` + the interfaces above |
| `web/src/impact.ts` | `tintColorForCombined(row)` reusing `pickTier`/`TIERS`; export a helper to build a `Map<id, LiveImpact>` |
| `web/src/main.ts` | post-load pre-tint pass (fetch live map → set node colors → refresh renderer); expose on `window.__ec` for manual refresh |
| `web/src/ui/inspector.ts` | combined-impact section: verdict + freshness + event timeline + sharpen button; async patch-in |
| `web/src/__tests__/…` | vitest unit tests for the pure logic |

## Testing (vitest — pure logic; DOM wiring kept thin)

- **`tintColorForCombined`**: positive high-magnitude → high-tier green; negative →
  red; `mixed_signals=1` → amber even if net direction is positive; magnitude 0 /
  `no_effect` → neutral/default.
- **live-map builder**: array → `Map` keyed by node_id; empty → empty map.
- **timeline view-model** (a pure function that maps `NodeImpact.top_events` → row
  descriptors): event with url → link row; event with null url → plain-text row;
  ordering preserved; empty top_events → empty list; `impact: null` → "no impact" flag.
- **api client**: `getImpactLive`/`getNodeImpact` call the right paths (mock `get`).
- **Type-check**: `npm run build` (tsc) passes.

Full visual/E2E verification (open app → warm tint → click → panel → sharpen) requires
both servers running against a **populated `node_impact`** (a real cycle with Claude +
network), so it is validated manually by the user after a cycle runs; the automated
gate for this phase is vitest + tsc + code review.

## Out of scope
Changing the on-demand streaming UI itself; a global "refresh cache now" button in the
UI (the cycle/scheduler is P4/ops); mobile layout; persisting per-user view state.
