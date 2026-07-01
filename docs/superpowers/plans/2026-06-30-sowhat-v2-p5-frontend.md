# So What? V2 · P5 — Frontend V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Pre-tint the graph on load from `GET /impact/live` (no LLM at load), and turn a node click into its combined-impact story (net verdict + freshness + event timeline with source links + a "sharpen with Claude" button that runs the full V1 trace on the strongest event).

**Architecture:** Add typed API clients (`api.ts`) → a combined-tint helper (`impact.ts`) → a combined-impact inspector section (`inspector.ts`) → wire pre-tint + click-fetch + sharpen in `main.ts`.

**Tech Stack:** Vanilla TypeScript, Sigma 3 (2D) + three.js/3d-force-graph (3D), Graphology, Vite, vitest (jsdom). Test: `npm run test`; typecheck+build: `npm run build`. Frontend lives in `web/`.

**Design:** `docs/superpowers/specs/2026-06-30-sowhat-v2-p5-frontend-design.md`

> ⚠️ **AUTHORITATIVE API CONTRACT** (from the P4 build — the recon's field guesses were wrong; use THESE exact shapes):
> - `GET /impact/live` → `{ computed_at: string|null, count: number, impacts: LiveImpact[] }`
>   where `LiveImpact = { node_id, direction, magnitude, mixed_signals /* 0|1 */, event_count }`. **No** name/type/top_events on these rows.
> - `GET /node/{id}/impact` → `{ node_id, name, type: string|null, impact: NodeImpact | null }`
>   where `NodeImpact = { direction, magnitude, mixed_signals /* 0|1 */, event_count, computed_at, top_events: TopEvent[] }`
>   and `TopEvent = { event_id, headline, direction, magnitude, weighted, hop, published_at: string|null, url: string|null, source: string|null }`.
>   `mixed_signals` is an **integer 0/1** from SQLite (treat truthiness as `=== 1` or `!!`).

---

### Task 1: API client + types (`web/src/api.ts`)

**Files:** Modify `web/src/api.ts`; Test `web/src/__tests__/api-impact-live.test.ts` (create).

Context: `get<T>(path, params?)` builds `new URL(path, API_BASE_URL)` and throws `ApiError` on non-2xx. Add the new fns + interfaces at the end of the file (after the last export, ~line 500).

- [ ] **Step 1: Write failing test** (`web/src/__tests__/api-impact-live.test.ts`) — mirror the existing `api.test.ts` fetch-mock style:

```ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { getImpactLive, getNodeImpact } from "../api";

function mockFetch(body: unknown) {
  const f = vi.fn().mockResolvedValue({
    ok: true, status: 200, statusText: "OK",
    json: async () => body, text: async () => JSON.stringify(body),
  } as unknown as Response);
  vi.stubGlobal("fetch", f);
  return f;
}
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("impact-live client", () => {
  it("getImpactLive hits /impact/live and parses", async () => {
    const f = mockFetch({ computed_at: "2026-06-30T00:00:00", count: 1,
      impacts: [{ node_id: "cik:1", direction: "negative", magnitude: 0.6, mixed_signals: 0, event_count: 2 }] });
    const r = await getImpactLive();
    expect(f.mock.calls[0][0]).toContain("/impact/live");
    expect(r.count).toBe(1);
    expect(r.impacts[0].node_id).toBe("cik:1");
  });

  it("getNodeImpact hits /node/{id}/impact and parses top_events", async () => {
    const f = mockFetch({ node_id: "cik:1", name: "Apple", type: "Company",
      impact: { direction: "negative", magnitude: 0.6, mixed_signals: 0, event_count: 1, computed_at: "2026-06-30T00:00:00",
        top_events: [{ event_id: "e1", headline: "H", direction: "negative", magnitude: 0.7, weighted: -0.7,
                       hop: 1, published_at: "2026-06-29", url: "https://x/e1", source: "SEC 8-K" }] } });
    const r = await getNodeImpact("cik:1");
    expect(f.mock.calls[0][0]).toContain("/node/cik:1/impact");
    expect(r.impact!.top_events[0].source).toBe("SEC 8-K");
  });

  it("getNodeImpact tolerates impact:null", async () => {
    mockFetch({ node_id: "slug:oil", name: "Crude Oil", type: "Commodity", impact: null });
    const r = await getNodeImpact("slug:oil");
    expect(r.impact).toBeNull();
  });
});
```

- [ ] **Step 2: Run, verify fail** — `cd web && npm run test -- api-impact-live` → import errors.

- [ ] **Step 3: Implement** — append to `web/src/api.ts`:

```ts
// So What? V2 · P5 — precomputed impact endpoints (no request-time LLM).
export interface LiveImpact {
  node_id: string;
  direction: "positive" | "negative" | "no_effect";
  magnitude: number;
  mixed_signals: number;   // SQLite integer 0|1
  event_count: number;
}
export interface ImpactLiveResponse { computed_at: string | null; count: number; impacts: LiveImpact[]; }

export interface TopEvent {
  event_id: string;
  headline: string;
  direction: string;
  magnitude: number;
  weighted: number;
  hop: number;
  published_at: string | null;
  url: string | null;
  source: string | null;
}
export interface NodeImpact {
  direction: "positive" | "negative" | "no_effect";
  magnitude: number;
  mixed_signals: number;   // 0|1
  event_count: number;
  computed_at: string;
  top_events: TopEvent[];
}
export interface NodeImpactResponse { node_id: string; name: string; type: string | null; impact: NodeImpact | null; }

export function getImpactLive(): Promise<ImpactLiveResponse> {
  return get<ImpactLiveResponse>("/impact/live");
}
export function getNodeImpact(nodeId: string): Promise<NodeImpactResponse> {
  return get<NodeImpactResponse>(`/node/${nodeId}/impact`);
}
```
(Node ids contain colons, e.g. `cik:0000320193`; the backend route is `{node_id:path}`, so a raw colon in the path is fine — do NOT `encodeURIComponent` the colon away. Match how existing `/node/...` client calls build the path.)

- [ ] **Step 4: Run, verify pass** — `cd web && npm run test -- api-impact-live`.
- [ ] **Step 5: Commit** — `feat(v2): frontend API clients for /impact/live + /node/{id}/impact`.

---

### Task 2: Combined-impact tinting + live-map builder (`web/src/impact.ts`)

**Files:** Modify `web/src/impact.ts`; Test `web/src/__tests__/impact-combined.test.ts` (create).

Context: `impact.ts` has `TIERS` (positive/negative/mixed palettes), `pickTier(intensity)`, `tintColor`, `tintColorRGB`. A `node_impact` verdict is netted (no hop) — tint at full intensity by magnitude. Reuse `pickTier`/`TIERS`.

- [ ] **Step 1: Write failing test** (`web/src/__tests__/impact-combined.test.ts`)

```ts
import { describe, expect, it } from "vitest";
import { tintColorForCombined, buildLiveImpactMap } from "../impact";
import type { LiveImpact } from "../api";

const row = (p: Partial<LiveImpact>): LiveImpact =>
  ({ node_id: "x", direction: "positive", magnitude: 0.8, mixed_signals: 0, event_count: 1, ...p });

describe("tintColorForCombined", () => {
  it("high positive → a green rgb() string", () => {
    const c = tintColorForCombined(row({ direction: "positive", magnitude: 0.9 }))!;
    expect(c.startsWith("rgb(")).toBe(true);
  });
  it("mixed_signals selects amber even when net positive", () => {
    const mixed = tintColorForCombined(row({ direction: "positive", magnitude: 0.9, mixed_signals: 1 }));
    const plain = tintColorForCombined(row({ direction: "positive", magnitude: 0.9, mixed_signals: 0 }));
    expect(mixed).not.toBe(plain);
  });
  it("no_effect or ~0 magnitude → null (no tint)", () => {
    expect(tintColorForCombined(row({ direction: "no_effect", magnitude: 0 }))).toBeNull();
    expect(tintColorForCombined(row({ direction: "positive", magnitude: 0.02 }))).toBeNull();
  });
});

describe("buildLiveImpactMap", () => {
  it("keys rows by node_id; empty → empty", () => {
    const m = buildLiveImpactMap([row({ node_id: "a" }), row({ node_id: "b" })]);
    expect(m.size).toBe(2);
    expect(m.get("a")!.node_id).toBe("a");
    expect(buildLiveImpactMap([]).size).toBe(0);
  });
});
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** — insert after `tintColorRGB` in `web/src/impact.ts`. Reuse the module-private `TIERS`/`pickTier` (already in scope). `mixed_signals` may be `0|1` (number) or boolean — coerce with `!!`.

```ts
import type { LiveImpact } from "./api";   // add to existing imports if not present

/** Tint a node from a precomputed (netted, no-hop) combined verdict.
 *  Full intensity by magnitude; mixed_signals → amber palette. */
export function tintColorForCombined(
  row: { direction: string; magnitude: number; mixed_signals?: number | boolean },
): string | null {
  const mag = Number(row.magnitude);
  if (!isFinite(mag)) return null;
  if (row.direction === "no_effect" || mag <= 0.05) return null;
  const tier = pickTier(Math.min(1, mag));         // no hop decay for a combined verdict
  const palette = row.mixed_signals ? TIERS.mixed
    : row.direction === "positive" ? TIERS.positive : TIERS.negative;
  const c = palette[tier];
  return `rgb(${c.r}, ${c.g}, ${c.b})`;
}

/** Same tint as an {r,g,b} 0-1 triple for the 3D renderer. */
export function tintColorForCombinedRGB(
  row: { direction: string; magnitude: number; mixed_signals?: number | boolean },
): { r: number; g: number; b: number } | null {
  const mag = Number(row.magnitude);
  if (!isFinite(mag)) return null;
  if (row.direction === "no_effect" || mag <= 0.05) return null;
  const tier = pickTier(Math.min(1, mag));
  const palette = row.mixed_signals ? TIERS.mixed
    : row.direction === "positive" ? TIERS.positive : TIERS.negative;
  const c = palette[tier];
  return { r: c.r / 255, g: c.g / 255, b: c.b / 255 };
}

export function buildLiveImpactMap(rows: LiveImpact[]): Map<string, LiveImpact> {
  const m = new Map<string, LiveImpact>();
  for (const r of rows ?? []) m.set(r.node_id, r);
  return m;
}
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `feat(v2): combined-impact tint helper + live-map builder`.

---

### Task 3: Combined-impact inspector section (`web/src/ui/inspector.ts`)

**Files:** Modify `web/src/ui/inspector.ts`; Test `web/src/__tests__/inspector-timeline.test.ts` (create).

Context: `inspector.ts` uses an `el(tag, attrs, ...children)` DOM helper and `showNode(node, g, extras)` with a `NodeExtras` interface. Rendering is synchronous; the existing Describe button shows the async pattern. We add a **pure** `buildTimelineRows` view-model fn (unit-tested) + a `renderCombinedImpactInto(root, resp, handlers)` DOM fn, and extend `NodeExtras`.

- [ ] **Step 1: Write failing test** (`web/src/__tests__/inspector-timeline.test.ts`) — test only the pure view-model:

```ts
import { describe, expect, it } from "vitest";
import { buildTimelineRows } from "../ui/inspector";
import type { NodeImpact } from "../api";

const imp = (top: NodeImpact["top_events"]): NodeImpact =>
  ({ direction: "negative", magnitude: 0.6, mixed_signals: 0, event_count: top.length,
     computed_at: "2026-06-30T00:00:00", top_events: top });

describe("buildTimelineRows", () => {
  it("linkable when url present; plain when null; order preserved", () => {
    const rows = buildTimelineRows(imp([
      { event_id: "a", headline: "First", direction: "negative", magnitude: 0.7, weighted: -0.7, hop: 1,
        published_at: "2026-06-29", url: "https://x/a", source: "SEC 8-K" },
      { event_id: "b", headline: "Second", direction: "positive", magnitude: 0.2, weighted: 0.2, hop: 2,
        published_at: "2026-06-20", url: null, source: null },
    ]));
    expect(rows.map(r => r.headline)).toEqual(["First", "Second"]);
    expect(rows[0].linkUrl).toBe("https://x/a");
    expect(rows[0].sourceLabel).toBe("SEC 8-K");
    expect(rows[1].linkUrl).toBeNull();
  });
  it("empty top_events → empty rows", () => {
    expect(buildTimelineRows(imp([])).length).toBe(0);
  });
});
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement in `web/src/ui/inspector.ts`.**

(a) Extend `NodeExtras`:
```ts
  combinedImpact?: import("../api").NodeImpact | null;   // resolved verdict, or null = no recent impact
  onSharpen?: (headline: string) => void;                // run full V1 trace on the strongest event
```

(b) Add the pure view-model (exported):
```ts
export interface TimelineRow {
  headline: string; direction: string; magnitude: number;
  publishedAt: string | null; linkUrl: string | null; sourceLabel: string | null;
}
export function buildTimelineRows(impact: import("../api").NodeImpact): TimelineRow[] {
  return (impact.top_events ?? []).map(e => ({
    headline: e.headline, direction: e.direction, magnitude: e.magnitude,
    publishedAt: e.published_at, linkUrl: e.url ?? null, sourceLabel: e.source ?? null,
  }));
}
```

(c) Add the DOM renderer (exported so `main.ts` can call it after the async fetch). It replaces any prior combined section under `root` (so re-clicks don't stack):
```ts
export function renderCombinedImpactInto(
  root: HTMLElement,
  resp: import("../api").NodeImpactResponse,
  handlers: { onSharpen?: (headline: string) => void } = {},
): void {
  root.querySelector(".combined-impact-box")?.remove();
  const imp = resp.impact;
  const box = el("div", { class: "combined-impact-box" });
  box.appendChild(el("div", { class: "combined-impact-title" }, "Combined impact · precomputed"));
  if (!imp) {
    box.appendChild(el("p", { class: "combined-empty" }, "No recent impact."));
    root.appendChild(box);
    return;
  }
  const dir = imp.direction === "positive" ? "POSITIVE"
    : imp.direction === "negative" ? "NEGATIVE" : "NO EFFECT";
  box.appendChild(el("div", { class: "combined-header" },
    el("span", { class: "impact-dir" }, imp.mixed_signals ? "MIXED" : dir),
    el("span", { class: "impact-mag" }, `magnitude ${imp.magnitude.toFixed(2)}`),
    el("span", { class: "impact-count" }, `${imp.event_count} event${imp.event_count === 1 ? "" : "s"}`),
  ));
  box.appendChild(el("div", { class: "combined-freshness" }, `as of ${imp.computed_at}`));
  const rows = buildTimelineRows(imp);
  if (rows.length) {
    const tl = el("div", { class: "combined-timeline" });
    for (const rrow of rows) {
      const item = el("div", { class: "timeline-event" },
        el("span", { class: `event-dir ${rrow.direction}` }, rrow.direction.toUpperCase()),
        el("span", { class: "event-headline" }, rrow.headline),
        el("span", { class: "event-date" }, rrow.publishedAt ?? ""));
      if (rrow.linkUrl) {
        item.appendChild(el("a", { class: "event-source", href: rrow.linkUrl,
          target: "_blank", rel: "noopener noreferrer" }, rrow.sourceLabel ?? "source"));
      } else if (rrow.sourceLabel) {
        item.appendChild(el("span", { class: "event-source muted" }, rrow.sourceLabel));
      }
      tl.appendChild(item);
    }
    box.appendChild(tl);
  }
  if (handlers.onSharpen && rows.length) {
    const btn = el("button", { class: "sharpen-btn", type: "button" }, "Sharpen with Claude");
    const strongest = imp.top_events[0].headline;
    btn.addEventListener("click", () => handlers.onSharpen!(strongest));
    box.appendChild(btn);
  }
  root.appendChild(box);
}
```
(Find the inspector root element `showNode` renders into — reuse the same container variable `r`/panel node it uses; `renderCombinedImpactInto` receives that root from `main.ts`. Export whatever accessor `main.ts` needs, or expose the panel root id. If `showNode` already appends to a known `#inspector`/panel element, `main.ts` can query it directly — confirm the id/selector and document it in the fn's usage.)

- [ ] **Step 4: Run, verify pass** — `cd web && npm run test -- inspector-timeline`.
- [ ] **Step 5: Minimal CSS** — add styles for `.combined-impact-box`, `.combined-timeline`, `.timeline-event`, `.sharpen-btn` to the existing stylesheet, matching the look of the existing `.impact-box` (reuse variables/classes where possible). Keep it consistent with the palette (teal/purple/coral/blue/gray per CLAUDE.md is for edges; impact tints use green/red/amber).
- [ ] **Step 6: Commit** — `feat(v2): combined-impact inspector section + timeline view-model`.

---

### Task 4: Wire pre-tint, click-fetch, and sharpen (`web/src/main.ts`)

**Files:** Modify `web/src/main.ts` (+ `web/src/graph.ts` only if a color-set helper is needed). No new test file (renderer/DOM-heavy); gate is `npm run build` (tsc) + manual. Keep logic delegated to the already-tested pure fns from Tasks 1–3.

Context (from recon): a module-level `impactState` (on-demand trace overlay) drives the Sigma `nodeReducer`; when `impactState` is set, non-chain nodes are hidden. The 2D `clickNode` and 3D `onNodeClick` handlers call `showNode(...)`. `runImpactStream(text, {onEvent})` runs the full trace; there is an existing on-demand trace trigger that sets `impactState` — REUSE it for sharpen (find the function that currently runs a headline trace from the search/impact box).

- [ ] **Step 1: Add a live-impact overlay state** near the `impactState` declaration:
```ts
let liveImpactState: Map<string, import("./api").LiveImpact> = new Map();
```

- [ ] **Step 2: Pre-tint pass after load.** After the core graph is loaded and interactive (the post-landing block the recon identified, after `loadFullCore()`), add a non-blocking pass:
```ts
import { getImpactLive } from "./api";
import { buildLiveImpactMap, tintColorForCombined } from "./impact";

async function applyLiveImpactTint(): Promise<void> {
  try {
    const resp = await getImpactLive();
    liveImpactState = buildLiveImpactMap(resp.impacts);
    renderer.refresh();                    // reducer picks up liveImpactState
    if (is3DRunning()) update3D(g, filters);
  } catch (err) {
    console.error("live impact tint failed", err);   // graph still usable untinted
  }
}
```
Call `applyLiveImpactTint()` in the post-landing block. Also expose it on `window.__ec` as `tintLive: applyLiveImpactTint`.

- [ ] **Step 3: Extend the Sigma `nodeReducer`.** The on-demand `impactState` branch stays and takes precedence. Add an `else if (liveImpactState.size)` branch that tints from the live map WITHOUT hiding anything (unlike the on-demand branch):
```ts
  if (impactState) {
    /* ...existing on-demand branch unchanged... */
  }
  if (liveImpactState.size) {
    const row = liveImpactState.get(id);
    const tint = row ? tintColorForCombined(row) : null;
    if (tint) return { ...nattrs, label, color: tint, size: baseSize * 1.4, zIndex: 5 };
    // untinted nodes keep normal appearance (NOT hidden) in live mode
    return { ...nattrs, label, size: baseSize };
  }
  return { ...nattrs, label, hidden: hide, size: baseSize };
```
(Preserve the existing visibility/`hide` logic that precedes the impact branches. If a 3D tint path exists that reads verdict colors, add the analogous `tintColorForCombinedRGB(row)` branch in `update3D`/the 3D color function.)

- [ ] **Step 4: Fetch + render combined impact on node click.** In BOTH the 2D `clickNode` and 3D `onNodeClick` handlers, after `showNode(...)` renders, fetch and patch in the combined section. In live mode, do NOT gate clicks on `tintColor(impactState...)` (that guard only applies when an on-demand `impactState` is active). Add a helper:
```ts
import { getNodeImpact } from "./api";
import { renderCombinedImpactInto } from "./ui/inspector";

function showCombinedImpact(nodeId: string): void {
  getNodeImpact(nodeId)
    .then((resp) => {
      const root = document.getElementById("inspector");   // confirm the actual inspector root id/selector
      if (root) renderCombinedImpactInto(root, resp, { onSharpen: sharpenWithClaude });
    })
    .catch((err) => console.error("combined impact fetch failed", err));
}
```
Call `showCombinedImpact(id)` right after each `showNode(...)` call in the click handlers. (Confirm the inspector root element id/selector `showNode` renders into and use it here; if `showNode` doesn't render into a stable id, add one.)

- [ ] **Step 5: Sharpen handler.** Reuse the existing on-demand trace entrypoint:
```ts
function sharpenWithClaude(headline: string): void {
  // Reuse the SAME function the search/impact box uses to run a full streaming
  // trace and set `impactState` (find it in this file — e.g. runImpactAndRender()).
  // It runs runImpactStream(headline, {onEvent}) and updates impactState + renderer.
  <existing on-demand trace trigger>(headline);
}
```
Wire the actual existing trigger (do not re-implement streaming). The full trace overlays the live baseline (on-demand `impactState` takes precedence in the reducer), exactly matching the "sharpen" model.

- [ ] **Step 6: Typecheck + build** — `cd web && npm run build` (tsc --noEmit + vite build) passes with no type errors.
- [ ] **Step 7: Run the full frontend test suite** — `cd web && npm run test` (all green, including Tasks 1–3).
- [ ] **Step 8: Commit** — `feat(v2): pre-tint graph from /impact/live; node-click combined impact + sharpen`.

---

## Self-review checklist (after all tasks)
- Real API shapes used everywhere (compact `LiveImpact` for `/impact/live`; `impact` may be `null`; `mixed_signals` treated as 0/1; `top_events` has `url`/`source`).
- Live pre-tint does NOT hide untinted nodes; on-demand `impactState` still takes precedence and its hide-behavior is unchanged.
- Node ids with colons pass through the path unescaped.
- Sharpen reuses the existing streaming trace trigger (no duplicate streaming logic).
- `npm run build` (tsc) + `npm run test` both green.
- Manual E2E (warm tint on open → click → combined panel → sharpen) requires both servers + a populated `node_impact`; note it for the user rather than gating on it.
