// Entrypoint. Wires Sigma + UI together.

import Sigma from "sigma";
import { runLanding } from "./landing";
import { loadSubgraph, saveSubgraph, loadPositions, savePositions } from "./graph-cache";

import {
  ApiEdge,
  ApiNode,
  getEdge,
  getEgo,
  getNode,
  getSubgraph,
  onLoadingChange,
  type SubgraphResponse,
} from "./api";
import {
  FULL_VIEW_HOPS,
  FULL_VIEW_SEED,
  OPEN_MODE,
} from "./config";
import {
  EconGraph,
  createGraph,
  layoutBySector,
  mergeFromApi,
  replaceGraph,
  restyleAfterMerge,
  runLayout,
} from "./graph";
import {
  bubbleVisibility,
  ensureBubbleNodes,
  isBubble,
  layoutBubbleView,
  refreshBubbleEdges,
  sectorOfBubble,
  updateBubbleAppearance,
} from "./bubbles";
import {
  applyImpact3D,
  is3DRunning,
  resetGlobeCamera,
  resize3D,
  start3D,
  stop3D,
  update3D,
} from "./render3d";
import { wireFilters, countryInMarkets, type FilterState } from "./ui/filters";
import { showEdge, showEmpty, showNode, type NodeExtras } from "./ui/inspector";
import { wireSearch } from "./ui/search";
import { startStatusPolling } from "./ui/status";
import { describeNode, runImpact, runImpactStream, runMultiImpact, type ImpactResponse, type ImpactVerdict, type MultiImpactResponse } from "./api";
import { fetchHeadlines, type Headline } from "./news";
import {
  buildImpactState,
  buildMultiImpactState,
  tintColor,
  type ImpactState,
} from "./impact";
import {
  loadArchive,
  saveToArchive,
  saveMultiToArchive,
  removeFromArchive,
  type ArchiveEntry,
} from "./impact-archive";

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

const container = document.getElementById("canvas") as HTMLDivElement;
const container3d = document.getElementById("canvas-3d") as HTMLDivElement;
const loadingEl = document.getElementById("loading") as HTMLDivElement;

onLoadingChange((loading) => {
  loadingEl.hidden = !loading;
});

startStatusPolling();

const g: EconGraph = createGraph();

const renderer = new Sigma(g, container, {
  renderEdgeLabels: false,
  defaultEdgeType: "line",
  // Sigma defaults edge colour to "#ccc" (light grey) which fights the
  // dark theme everywhere and SHOWS THROUGH the impact dimming whenever
  // the reducer's per-edge colour isn't applied (e.g. some render
  // paths). Pin it to the monochrome grey we use elsewhere so dim
  // fallbacks don't look bright white.
  defaultEdgeColor: "#8a8e94",
  defaultNodeColor: "#c8ccd2",
  labelDensity: 1,
  labelGridCellSize: 80,
  labelRenderedSizeThreshold: 6,
  labelFont: "Inter, system-ui, sans-serif",
  // Dark-mode label color, matched to --text in styles.css.
  labelColor: { color: "#e8e3da" },
  labelSize: 11,
  // Don't bail out when the container measures 0 on first mount -- CSS grid
  // can take a frame to settle, and Sigma would otherwise refuse to render
  // until the next ResizeObserver tick, leaving the canvas blank.
  allowInvalidContainer: true,
});
// node/edge reducers are installed once in refreshEdgeVisibility() so they
// can read the live `filters` closure on every render.

// Sigma's internal ResizeObserver sometimes misses the initial layout
// transition (canvases stay at 300x150). Force an explicit resize on the
// next animation frame after construction, and observe the container so
// later resizes propagate too.
requestAnimationFrame(() => renderer.refresh());
const resizeObs = new ResizeObserver(() => renderer.refresh());
resizeObs.observe(container);
window.addEventListener("resize", () => renderer.refresh());

// Expose for in-browser debugging / preview-tool inspection.
declare global {
  interface Window {
    __ec: {
      renderer: typeof renderer;
      graph: typeof g;
      recenterOn: (id: string) => Promise<void>;
      expandFrom: (id: string) => Promise<void>;
      loadFullCore: () => Promise<void>;
      setIncludeProvisional: (v: boolean) => void;
    };
  }
}
window.__ec = {
  renderer,
  graph: g,
  recenterOn,
  expandFrom,
  loadFullCore,
  setIncludeProvisional: (v: boolean) => {
    const cb = document.getElementById("toggle-provisional") as HTMLInputElement | null;
    if (cb) {
      cb.checked = v;
      cb.dispatchEvent(new Event("change", { bubbles: true }));
    }
  },
};

// Current filter state -- declared so all callbacks can read it without
// dragging it through arguments.
let filters: FilterState = { types: [], includeProvisional: false, includeInferred: false, markets: null };

// Layout mode for the 2D view. Force = FA2; sector = GICS clusters;
// bubble = each sector collapsed into a clickable hub with expand/collapse.
let layoutMode: "force" | "sector" | "bubble" = "force";
const expandedSectors = new Set<string>();
let hiddenForBubble = { nodes: new Set<string>(), edges: new Set<string>() };

async function applyLayout(): Promise<void> {
  if (layoutMode === "bubble") {
    ensureBubbleNodes(g);
    refreshBubbleEdges(g);
    layoutBubbleView(g, expandedSectors);
    updateBubbleAppearance(g, expandedSectors);
    hiddenForBubble = bubbleVisibility(g, expandedSectors);
  } else {
    hiddenForBubble = { nodes: new Set(), edges: new Set() };
    if (layoutMode === "sector") {
      layoutBySector(g);
    } else {
      // runLayout is async — yields to the event loop every 5 FA2 iterations
      // so the main thread is never frozen for more than ~1 s at a stretch.
      await runLayout(g, 220);
    }
  }
  renderer.refresh();
}

function toggleSector(sector: string) {
  if (expandedSectors.has(sector)) expandedSectors.delete(sector);
  else expandedSectors.add(sector);
  layoutBubbleView(g, expandedSectors);
  updateBubbleAppearance(g, expandedSectors);
  hiddenForBubble = bubbleVisibility(g, expandedSectors);
  renderer.refresh();
}

filters = wireFilters((next) => {
  filters = next;
  // Filtering by type might add or hide edges; cheapest path is to redraw.
  // We don't refetch here -- the UI honors what's currently in the graph by
  // hiding edges whose type isn't in `filters.types`. Pure-visual change.
  refreshEdgeVisibility();
  // Propagate market / provisional filter to 3D view if it's active.
  if (is3DRunning()) update3D(g, filters);
});

// Global node scale — adjusted with [ / ] keys. Applied in the nodeReducer
// so the change is immediate without any graph data mutation.
let nodeScale = 1.0;
const NODE_SCALE_MIN = 0.3;
const NODE_SCALE_MAX = 4.0;
const NODE_SCALE_STEP = 0.15;

// Impact-overlay state. When set, the node + edge reducers below
// tint/dim everything accordingly. The 3D mesh tinting is applied
// via applyImpact3D() in render3d.ts.
let impactState: ImpactState | null = null;

// Build the inspector's contextual extras: surfaces the LLM verdict
// (if a propagation run is active for this node) and wires the
// "Describe" button to the cached /describe endpoint.
function inspectorExtrasFor(nodeId: string): NodeExtras {
  const extras: NodeExtras = {
    onDescribe: async (id: string) => {
      const resp = await describeNode(id);
      return resp.description;
    },
  };
  if (impactState) {
    const v = impactState.byNode.get(nodeId);
    if (v) extras.impact = v;
  }
  return extras;
}

refreshEdgeVisibility(); // initial pass (everything visible)

function refreshEdgeVisibility(): void {
  // Belt-and-braces: while an impact run is active, flip the SIGMA
  // default colours dark too, so the rare edge / node that slips past
  // the reducer (different render program, race during HMR, etc.)
  // doesn't pop as bright white over the dimmed background.
  if (impactState) {
    renderer.setSetting("defaultEdgeColor", "#1a1d22");
    renderer.setSetting("defaultNodeColor", "#22262c");
  } else {
    renderer.setSetting("defaultEdgeColor", "#7a7e84");
    renderer.setSetting("defaultNodeColor", "#c8ccd2");
  }
  renderer.setSetting("edgeReducer", (eid, eattrs) => {
    // Aggregated bubble<->bubble edges only belong in Bubbles layout.
    const isVirtualBubbleEdge = eid.startsWith("bubble-edge:");
    if (isVirtualBubbleEdge && layoutMode !== "bubble") {
      return { ...eattrs, hidden: true };
    }
    if (layoutMode === "bubble" && hiddenForBubble.edges.has(eid)) {
      return { ...eattrs, hidden: true };
    }
    const ok = filters.types.includes(eattrs.edgeType);
    const isProv = eattrs.apiEdge.attributes.below_threshold;
    const baseHidden = !ok || (isProv && !filters.includeProvisional);
    // Impact overlay: only show edges where BOTH endpoints fired (non-null
    // tint). Everything else is hidden so the impact subgraph renders clean.
    if (impactState) {
      const src = g.source(eid);
      const tgt = g.target(eid);
      const srcFired = tintColor(impactState.byNode.get(src)) !== null;
      const tgtFired = tintColor(impactState.byNode.get(tgt)) !== null;
      const inChain = srcFired && tgtFired;
      return {
        ...eattrs,
        hidden: baseHidden || !inChain,
        color: "#ffffff",
        size: (eattrs.size ?? 1) * 1.5,
      };
    }
    return { ...eattrs, hidden: baseHidden };
  });
  renderer.setSetting("nodeReducer", (id, nattrs) => {
    const isBubbleNode = isBubble(id);
    if (isBubbleNode && layoutMode !== "bubble") {
      return { ...nattrs, hidden: true, label: "" };
    }
    if (layoutMode === "bubble" && hiddenForBubble.nodes.has(id)) {
      return { ...nattrs, hidden: true, label: "" };
    }
    const isProv = !!nattrs.apiNode.attributes.provisional;
    // Market filter: hide Company nodes whose country isn't in the selected markets.
    // Non-company nodes (Commodity, Regulator, Region) are always visible.
    let hiddenByMarket = false;
    if (filters.markets !== null && filters.markets.length > 0) {
      const nodeType    = nattrs.apiNode.attributes.type;
      const nodeCountry = nattrs.apiNode.attributes.country;
      if (nodeType === "Company" && nodeCountry) {
        hiddenByMarket = !countryInMarkets(nodeCountry, filters.markets);
      }
    }
    const hide = (isProv && !filters.includeProvisional) || hiddenByMarket;
    const label = isBubbleNode ? nattrs.label : (nattrs.displayLabel || "");
    // Apply the global [ / ] scale factor so all types scale together.
    const baseSize = (nattrs.size ?? 6) * nodeScale;
    if (impactState) {
      const verdict = impactState.byNode.get(id);
      const tint = tintColor(verdict);
      const isImpacted = tint !== null;
      return {
        ...nattrs,
        label: isImpacted ? label : "",
        hidden: hide || !isImpacted,   // hide non-firing nodes (incl. no_effect)
        color: tint ?? "#1e2228",
        size: isImpacted ? baseSize * 1.8 : baseSize * 0.45,
        zIndex: isImpacted ? 10 : 0,
        forceLabel: isImpacted && (verdict?.hop === 0 || (verdict?.magnitude ?? 0) >= 0.6),
      };
    }
    return { ...nattrs, label, hidden: hide, size: baseSize };
  });
  renderer.refresh();
}

// ---------------------------------------------------------------------------
// Futuristic loading overlays
// ---------------------------------------------------------------------------

let _graphOverlayTimer: ReturnType<typeof setInterval> | null = null;

function showGraphOverlay(fixedMessage?: string): void {
  const overlay = document.getElementById("graph-overlay");
  const stageEl = document.getElementById("graph-overlay-stage");
  if (!overlay || !stageEl) return;
  overlay.style.display = "flex";
  if (fixedMessage) {
    // Cache-hit path: show a static message instead of the rotating API-fetch stages.
    stageEl.textContent = fixedMessage;
    return; // no rotating timer
  }
  const stages = [
    "Connecting to graph database…",
    "Fetching nodes from economy network…",
    "Streaming economic relationships…",
    "Building supply chain links…",
    "Resolving competitor networks…",
    "Constructing regulated-by graph…",
  ];
  let i = 0;
  stageEl.textContent = stages[0];
  _graphOverlayTimer = setInterval(() => {
    i = (i + 1) % stages.length;
    stageEl.textContent = stages[i];
  }, 2800);
}

function setGraphOverlayStage(msg: string): void {
  const stageEl = document.getElementById("graph-overlay-stage");
  if (stageEl) stageEl.textContent = msg;
}

function hideGraphOverlay(): void {
  if (_graphOverlayTimer) { clearInterval(_graphOverlayTimer); _graphOverlayTimer = null; }
  const overlay = document.getElementById("graph-overlay");
  if (overlay) overlay.style.display = "none";
}

let _impactOverlayTimer: ReturnType<typeof setInterval> | null = null;

function showImpactOverlay(provider: string): void {
  const overlay = document.getElementById("impact-overlay");
  const stageEl = document.getElementById("impact-overlay-stage");
  const etaEl  = document.getElementById("impact-overlay-eta");
  if (!overlay || !stageEl) return;
  overlay.style.display = "flex";
  if (etaEl) etaEl.textContent = provider === "claude" ? "est. 1–3 min" : "est. 3–8 min";
  const stages = [
    "Parsing event description…",
    "Identifying seed entity in graph…",
    "Propagating through supply chain (hop 1)…",
    "Expanding competitor network (hop 2)…",
    "Calculating cascade effects (hop 3)…",
    "Scoring impact magnitude…",
    "Aggregating affected nodes…",
  ];
  let i = 0;
  stageEl.textContent = stages[0];
  _impactOverlayTimer = setInterval(() => {
    i = (i + 1) % stages.length;
    stageEl.textContent = stages[i];
  }, 3200);
}

function hideImpactOverlay(): void {
  if (_impactOverlayTimer) { clearInterval(_impactOverlayTimer); _impactOverlayTimer = null; }
  const overlay = document.getElementById("impact-overlay");
  if (overlay) overlay.style.display = "none";
}

// ---------------------------------------------------------------------------
// Loaders
// ---------------------------------------------------------------------------

// Full-graph cache — avoids re-fetching and re-running FA2 on every
// "Full graph" / Esc press. Three tiers:
//   1. Already in full view          → cameraReset() only            (instant)
//   2. Graph replaced but cache warm → restore from memory, skip FA2 (~200ms)
//   3. Cold / filter changed         → network + FA2                  (full load)
//
// Cache key encodes the filter flags that change what the API returns.
// The positions snapshot is only saved for "force" layout (FA2 is the
// expensive step; sector + bubble layout is cheap to recompute).
let _fvResponse: SubgraphResponse | null = null;
let _fvCacheKey = "";
let _fvPositions: Map<string, { x: number; y: number }> | null = null;
let _graphIsFullView = false;
let _fvInFlight = false; // guard against concurrent loadFullCore() calls
let _fvInFlightPromise: Promise<void> | null = null; // shared promise for in-flight callers

function _fvKey(): string {
  return `${filters.includeProvisional}:${filters.includeInferred}`;
}

/**
 * Async-batched graph load: clears the graph and re-populates it in small
 * chunks, yielding to the event loop between each chunk via setTimeout(0).
 *
 * Why this exists: Sigma subscribes to every graphology nodeAdded / edgeAdded
 * event and does synchronous internal state updates for each one. Calling
 * replaceGraph() (i.e. g.clear() + mergeFromApi()) for the full S&P 500
 * corpus (2 600+ nodes, 18 000+ edges) in a single synchronous pass blocks
 * the main thread for 45+ seconds — freezing the UI, dropping frames, and
 * causing CDP timeouts.
 *
 * With BATCH_NODES=150 and BATCH_EDGES=600 we keep each main-thread hold
 * under ~150 ms, give the browser time to paint the "Loading…" overlay
 * between chunks, and let the 3D globe progressively populate instead of
 * appearing all at once after an opaque freeze.
 *
 * Call restyleAfterMerge(g) AFTER awaiting this function.
 */
/** Yield to the event loop between batch operations.
 *
 * In VISIBLE tabs, use setTimeout(0) so the browser can repaint between
 * batches and the user sees incremental progress.
 *
 * In HIDDEN / FROZEN tabs (document.hidden === true), Chrome throttles
 * setTimeout to ≥1 s and may completely freeze macrotask execution after
 * 5+ minutes of inactivity.  A microtask yield (Promise.resolve()) is
 * NOT subject to that throttling — it runs immediately after the current
 * microtask queue drains.  The tradeoff is that repaints can't happen
 * between microtask-yielded batches, but since the tab is hidden there
 * is nothing to paint anyway.
 */
function _yieldToScheduler(): Promise<void> {
  if (document.hidden) {
    // Microtask yield: immune to background-tab timer throttling.
    return Promise.resolve();
  }
  return new Promise<void>(r => setTimeout(r, 0));
}

async function _batchLoadGraph(
  nodes: ApiNode[],
  edges: ApiEdge[],
  onProgress?: (msg: string) => void,
  fast = false,
): Promise<void> {
  // fast=true (IDB cache hit): use larger batches so the merge completes in
  // ~3-4 yields (~2-3 s) instead of the ~67 yields (~15-20 s) needed for a
  // cold API fetch where we want granular progress feedback.
  //
  // _bulkLoadInProgress blocks the per-batch update3D() that would otherwise
  // fire between every setTimeout(0) yield. In globe mode each suppressed call
  // would dispose + rebuild 13k TubeGeometry arcs — multiplied by ~8 batches
  // that's the bulk of the 20-30 s lag. One final update3D() fires in the
  // finally block once the full graph is in memory.
  _bulkLoadInProgress = true;
  try {
    g.clear();
    const NODE_BATCH = fast ? 2000 : 150;
    const EDGE_BATCH = fast ? 4000 : 600;
    for (let i = 0; i < nodes.length; i += NODE_BATCH) {
      mergeFromApi(g, nodes.slice(i, i + NODE_BATCH), []);
      if (!fast) {
        onProgress?.(
          `Loading ${Math.min(i + NODE_BATCH, nodes.length).toLocaleString()} / ${nodes.length.toLocaleString()} nodes…`,
        );
      }
      await _yieldToScheduler();
    }
    for (let i = 0; i < edges.length; i += EDGE_BATCH) {
      mergeFromApi(g, [], edges.slice(i, i + EDGE_BATCH));
      if (!fast) {
        onProgress?.(
          `Loading edges… ${Math.min(i + EDGE_BATCH, edges.length).toLocaleString()} / ${edges.length.toLocaleString()}`,
        );
      }
      await _yieldToScheduler();
    }
  } finally {
    _bulkLoadInProgress = false;
    // One 3D sync now the full graph is loaded. The globe was dark during
    // batching; this single call populates the scene in one pass.
    if (is3DRunning()) update3D(g, filters);
  }
}

// skipLayout=true: only populate graphology `g` — skip FA2 and camera reset.
// Used by the background prefetch during the landing overlay so that the
// synchronous FA2 pass (~2 s for 5 000+ nodes) never blocks the main thread
// while the landing animation is running. Layout is deferred to the next
// non-skipLayout call (Tier 2.25 below).
async function loadFullCore(skipLayout = false): Promise<void> {
  const cacheKey = _fvKey();

  // Tier 1: fully loaded and laid out — nothing to do except reset camera.
  if (_graphIsFullView && _fvCacheKey === cacheKey) {
    if (!skipLayout) cameraReset();
    return;
  }

  // Guard: a data load is already in flight.
  // skipLayout callers just wait for the shared promise.
  // Layout callers chain a follow-up so layout runs once data is ready.
  if (_fvInFlight) {
    const inflight = _fvInFlightPromise ?? Promise.resolve();
    if (skipLayout) return inflight;
    return inflight.then(() => { if (!_graphIsFullView) return loadFullCore(false); });
  }

  // Tier 2: in-memory cache with saved positions (same session, same filters).
  // Skip network + FA2 entirely; just restore graph state from RAM.
  if (_fvResponse && _fvCacheKey === cacheKey && _fvPositions) {
    if (skipLayout) return; // data already in g, nothing more needed
    await _batchLoadGraph(_fvResponse.nodes, _fvResponse.edges, undefined, true);
    restyleAfterMerge(g);
    // Restore FA2 positions so the graph appears instantly settled.
    _fvPositions.forEach((pos, id) => {
      if (g.hasNode(id)) {
        g.setNodeAttribute(id, "x", pos.x);
        g.setNodeAttribute(id, "y", pos.y);
      }
    });
    refreshEdgeVisibility();
    renderer.refresh();
    cameraReset();
    _graphIsFullView = true;
    return;
  }

  // Tier 2.25: IDB response is cached in _fvResponse but the graph may be
  // empty (background prefetch skipped replaceGraph to avoid blocking the
  // landing animation) or already populated (prior non-skipLayout call).
  // Try to load saved FA2 positions from IDB — avoids the 6+ s synchronous
  // FA2 pass on every page reload. Falls back to FA2 if no positions saved.
  if (_fvResponse && _fvCacheKey === cacheKey && !skipLayout) {
    // Guard: if another Tier 2.25 call is already in flight, chain behind it.
    // Without this, runLanding().then() can trigger a second concurrent call
    // that races on the same IDB connection, causing onblocked hangs.
    if (_fvInFlight) {
      const inflight = _fvInFlightPromise ?? Promise.resolve();
      return inflight.then(() => { if (!_graphIsFullView) return loadFullCore(false); });
    }
    _fvInFlight = true;
    _fvInFlightPromise = (async () => {
      try {
        showGraphOverlay("Restoring graph…");
        if (g.order === 0) {
          // Graph is empty — background prefetch only stored _fvResponse without
          // calling replaceGraph (to keep the main thread free during landing).
          // Use fast=true: data already in memory, no API wait, so larger batches
          // are safe and reduce visible loading time from ~15 s to ~2-3 s.
          await _batchLoadGraph(
            _fvResponse!.nodes,
            _fvResponse!.edges,
            undefined,
            true, // fast mode — in-memory cache hit
          );
          restyleAfterMerge(g);
        }

        // Try to restore previously saved FA2 positions — skips the 6+ s FA2 block.
        setGraphOverlayStage("Restoring layout…");
        const savedPositions = await loadPositions(cacheKey);
        if (savedPositions) {
          savedPositions.forEach((pos, id) => {
            if (g.hasNode(id)) {
              g.setNodeAttribute(id, "x", pos.x);
              g.setNodeAttribute(id, "y", pos.y);
            }
          });
          _fvPositions = savedPositions;
          refreshEdgeVisibility();
          renderer.refresh();
        } else if (!is3DRunning()) {
          // No saved positions, 2D mode — run FA2 then cache for next load.
          // Globe mode skips FA2: nodes are pinned at lat/lon; the 6 s FA2
          // pass is wasted work and is the second biggest source of startup lag.
          setGraphOverlayStage("Computing layout…");
          await applyLayout();
          if (layoutMode === "force") {
            const snap: Map<string, { x: number; y: number }> = new Map();
            g.forEachNode((id) => {
              snap.set(id, {
                x: g.getNodeAttribute(id, "x") ?? 0,
                y: g.getNodeAttribute(id, "y") ?? 0,
              });
            });
            _fvPositions = snap;
            savePositions(cacheKey, _fvPositions); // fire-and-forget — persists for next reload
          }
        }
        // else: globe mode with no saved positions — skip FA2 (lat/lon pins provide layout)

        cameraReset();
        _graphIsFullView = true;
      } finally {
        // Always dismiss the overlay — even if an error is thrown before the
        // happy-path hideGraphOverlay() call inside the try block, the user
        // would otherwise be permanently stuck at "Restoring layout…".
        hideGraphOverlay();
        _fvInFlight = false;
        _fvInFlightPromise = null;
      }
    })();
    return _fvInFlightPromise;
  }

  // Tiers 2.5 + 3 both async — set the flight guard before any await.
  _graphIsFullView = false;
  _fvInFlight = true;

  const loadPromise: Promise<void> = (async () => {
    try {
      // ── Tier 2.5: IndexedDB persistent cache ────────────────────────────
      // On page refresh the in-memory cache is gone but IDB persists.
      // Reading 5 000+ nodes from IDB takes ~100–200 ms vs 45–70 s from API.
      const cached = await loadSubgraph(cacheKey);
      if (cached) {
        _fvResponse = cached;
        _fvCacheKey = cacheKey;
        _fvPositions = null;
        if (!skipLayout) {
          // Landing is gone — safe to populate the graph now.
          // Use fast=true: data comes from IDB (no API wait), so large batches
          // are safe and cut load time from ~15 s to ~2-3 s.
          showGraphOverlay("Restoring graph…");
          await _batchLoadGraph(
            cached.nodes,
            cached.edges,
            undefined,
            true, // fast mode — IDB cache hit
          );
          restyleAfterMerge(g);

          // Try saved positions first — avoids the 6+ s FA2 block.
          setGraphOverlayStage("Restoring layout…");
          const savedPos = await loadPositions(cacheKey);
          if (savedPos) {
            savedPos.forEach((pos, id) => {
              if (g.hasNode(id)) {
                g.setNodeAttribute(id, "x", pos.x);
                g.setNodeAttribute(id, "y", pos.y);
              }
            });
            _fvPositions = savedPos;
            refreshEdgeVisibility();
            renderer.refresh();
          } else if (!is3DRunning()) {
            // No saved positions, 2D mode — run FA2 then cache.
            // Globe mode skips FA2: nodes are pinned at lat/lon.
            setGraphOverlayStage("Computing layout…");
            await applyLayout();
            if (layoutMode === "force") {
              const snap: Map<string, { x: number; y: number }> = new Map();
              g.forEachNode((id) => {
                snap.set(id, {
                  x: g.getNodeAttribute(id, "x") ?? 0,
                  y: g.getNodeAttribute(id, "y") ?? 0,
                });
              });
              _fvPositions = snap;
              savePositions(cacheKey, _fvPositions); // fire-and-forget
            }
          }
          // else: globe mode with no saved positions — skip FA2 (lat/lon pins)
          cameraReset();
          hideGraphOverlay();
          _graphIsFullView = true;
        }
        // skipLayout=true (background prefetch): just store _fvResponse —
        // do NOT call replaceGraph/restyleAfterMerge here because those fire
        // Sigma callbacks for every node/edge and block the main thread.
        // Tier 2.25 will do the full setup when the user launches.
        return;
      }

      // ── Tier 3: cold API fetch ───────────────────────────────────────────
      // IDB miss (first ever load, or cache expired after 24 h).
      showGraphOverlay();
      try {
        const resp = await getSubgraph(FULL_VIEW_SEED, {
          hops: FULL_VIEW_HOPS,
          includeProvisional: filters.includeProvisional,
          includeInferred: filters.includeInferred,
        });
        // Persist to IDB — fire-and-forget, never delays the render pipeline.
        saveSubgraph(cacheKey, resp);

        _fvResponse = resp;
        _fvCacheKey = cacheKey;
        _fvPositions = null;
        if (!skipLayout) {
          // Landing is gone — safe to populate the graph now with
          // batched loading so the main thread never freezes.
          await _batchLoadGraph(
            resp.nodes,
            resp.edges,
            setGraphOverlayStage,
          );
          restyleAfterMerge(g);
          if (!is3DRunning()) {
            // 2D mode only: run FA2 and cache positions. Globe skips FA2
            // (lat/lon pins provide positioning without a force simulation).
            await applyLayout();
            if (layoutMode === "force") {
              const snap: Map<string, { x: number; y: number }> = new Map();
              g.forEachNode((id) => {
                snap.set(id, {
                  x: g.getNodeAttribute(id, "x") ?? 0,
                  y: g.getNodeAttribute(id, "y") ?? 0,
                });
              });
              _fvPositions = snap;
            }
          }
          cameraReset();
          _graphIsFullView = true;
        }
        // skipLayout=true (background prefetch during landing): just store
        // _fvResponse here — graph population is deferred to the Tier 2.25
        // call that fires after the landing overlay dismisses, which calls
        // _batchLoadGraph with a visible overlay and yields between chunks.
        // Mirroring Tier 2.5: do NOT populate g here, as the event storm from
        // 2 600+ nodeAdded callbacks blocks the main thread and freezes the
        // landing globe animation.
      } finally {
        hideGraphOverlay();
      }
    } finally {
      // Always dismiss the overlay — if an error bubbles out of the Tier 2.5
      // layout-restore block or the Tier 3 inner try-finally, the overlay
      // would be permanently stuck without this safety net. hideGraphOverlay()
      // is idempotent (double-calling when already hidden is a no-op).
      hideGraphOverlay();
      _fvInFlight = false;
      _fvInFlightPromise = null;
    }
  })();

  _fvInFlightPromise = loadPromise;
  return loadPromise;
}

async function recenterOn(id: string): Promise<void> {
  _graphIsFullView = false; // graph is about to be replaced with an ego subgraph
  const resp = await getEgo(id, {
    types: ["supplies", "customer_of", "competes_with", "regulated_by"],
    includeProvisional: filters.includeProvisional,
    includeInferred: filters.includeInferred,
  });
  replaceGraph(g, resp.nodes, resp.edges);
  restyleAfterMerge(g);
  if (!is3DRunning()) await applyLayout();
  cameraReset();
  // Show the focused node in the inspector.
  const center = resp.nodes.find((n) => n.key === resp.center);
  if (center) {
    showNode(center, g, inspectorExtrasFor(center.key));
    hideMorningBrief();
  }
}

async function expandFrom(id: string): Promise<void> {
  const resp = await getEgo(id, {
    types: ["supplies", "customer_of", "competes_with", "regulated_by"],
    includeProvisional: filters.includeProvisional,
    includeInferred: filters.includeInferred,
  });
  // Merge -- keep what's there.
  const changed = mergeFromApi(g, resp.nodes, resp.edges);
  if (changed) {
    restyleAfterMerge(g);
    if (!is3DRunning()) await runLayout(g, 120);
  }
  renderer.refresh();
}

function cameraReset(): void {
  const camera = renderer.getCamera();
  camera.animatedReset({ duration: 300 });
}

// ---------------------------------------------------------------------------
// Interactions: click / double-click / edge-click
// ---------------------------------------------------------------------------

// Sigma fires both clickNode and doubleClickNode. We use a short delay on
// single-click to avoid firing it ahead of an incoming double-click.
const DOUBLE_CLICK_WINDOW_MS = 280;
let pendingClick: { id: string; timer: number } | null = null;

renderer.on("clickNode", (event) => {
  const id = event.node;
  // Bubble nodes ARE the expand/collapse affordance; clicking one toggles
  // its sector immediately (no double-click delay). All other clicks --
  // including on companies inside an expanded cluster -- go through the
  // normal expand-in-place / double-click-recenter flow.
  if (isBubble(id)) {
    const sector = sectorOfBubble(id);
    toggleSector(sector);
    return;
  }
  // In impact mode, only tinted (active chain) nodes respond to clicks.
  // Dim/grey nodes are still rendered but should not open the inspector
  // or trigger expansion -- clicking them is a mis-click, not intent.
  if (impactState && tintColor(impactState.byNode.get(id)) === null) return;
  if (pendingClick) {
    clearTimeout(pendingClick.timer);
    pendingClick = null;
  }
  pendingClick = {
    id,
    timer: window.setTimeout(() => {
      pendingClick = null;
      setInspectorCollapsed(false); // expand so the user sees the node details
      expandFrom(id).catch(console.error);
    }, DOUBLE_CLICK_WINDOW_MS),
  };
});

renderer.on("doubleClickNode", (event) => {
  if (pendingClick) {
    clearTimeout(pendingClick.timer);
    pendingClick = null;
  }
  // Suppress Sigma's default zoom-on-doubleclick so we own the gesture.
  event.preventSigmaDefault();
  recenterOn(event.node).catch(console.error);
});

// Hovering shows node detail without committing to a navigation.
// In impact mode, dim (no-tint) nodes are intentionally suppressed -- hovering
// them would replace the active inspector with an irrelevant "no effect" node,
// which is confusing when the user is reading a red/green node's panel.
renderer.on("enterNode", (event) => {
  const id = event.node;
  if (impactState && tintColor(impactState.byNode.get(id)) === null) return;
  const attrs = g.getNodeAttributes(id);
  showNode(attrs.apiNode, g, inspectorExtrasFor(id));
  hideMorningBrief();
});
renderer.on("leaveNode", () => {
  // Keep the panel pinned to whatever the user last selected; don't blank it
  // on hover-out -- that's annoying when reading provenance.
});

renderer.on("clickEdge", async (event) => {
  // In impact mode, swallow edge clicks unless at least one endpoint is in
  // the active chain (mirrors the onNodeClick and 3D onEdgeClick guards).
  if (impactState) {
    const src = g.hasEdge(event.edge) ? g.source(event.edge) : null;
    const tgt = g.hasEdge(event.edge) ? g.target(event.edge) : null;
    const srcActive = src != null && tintColor(impactState.byNode.get(src)) !== null;
    const tgtActive = tgt != null && tintColor(impactState.byNode.get(tgt)) !== null;
    if (!srcActive && !tgtActive) return;
  }
  setInspectorCollapsed(false); // show details when user clicks an edge
  hideMorningBrief();
  // Edge click -> provenance panel. Source/target node attrs come from the
  // live graph so we can show the human names alongside the snippet.
  const eattrs = g.getEdgeAttributes(event.edge);
  const sNode = g.hasNode(eattrs.apiEdge.source)
    ? g.getNodeAttributes(eattrs.apiEdge.source).apiNode
    : null;
  const tNode = g.hasNode(eattrs.apiEdge.target)
    ? g.getNodeAttributes(eattrs.apiEdge.target).apiNode
    : null;
  // For richer detail (e.g. derived edges that resolve to the underlying
  // supplies row's full provenance), call the API.
  try {
    const fresh = await getEdge(eattrs.apiEdge.key);
    showEdge(fresh, { source: sNode, target: tNode });
  } catch {
    showEdge(eattrs.apiEdge, { source: sNode, target: tNode });
  }
});

renderer.on("clickStage", () => {
  // Click on empty canvas resets the inspector and brings back the brief.
  showEmpty();
  showMorningBrief();
});

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

wireSearch((hit) => {
  recenterOn(hit.id).catch(console.error);
});

// ---------------------------------------------------------------------------
// "Full graph" button + Esc shortcut: return to the open-on-load view from
// any zoomed/ego state. Cheap (it re-issues the same /subgraph call) and the
// most-asked navigation gesture after double-click-to-recenter exists.
// ---------------------------------------------------------------------------

const fullBtn = document.getElementById("btn-fullview");
fullBtn?.addEventListener("click", () => {
  loadFullCore().catch(console.error);
});

// ---------------------------------------------------------------------------
// 2D <-> 3D view toggle. Sigma (2D) is the default; flipping to 3D mounts a
// three.js-backed force-graph in #canvas-3d, hides the Sigma canvas, and
// reuses the same graphology instance so every merge propagates to whichever
// view is active.
// ---------------------------------------------------------------------------

let viewMode: "2d" | "3d" | "globe" = "2d";

function setView(next: "2d" | "3d" | "globe") {
  if (next === viewMode) return;
  // Cancel any pending single-click action before switching views to avoid
  // firing a expand/recenter in the wrong view after the transition.
  if (pendingClick) {
    clearTimeout(pendingClick.timer);
    pendingClick = null;
  }
  viewMode = next;
  document.querySelectorAll<HTMLButtonElement>(".view-tab").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.view === next);
  });
  const wantsThree = next === "3d" || next === "globe";
  if (wantsThree) {
    container.hidden = true;
    container3d.hidden = false;
    // Tear down + restart when switching between ball <-> globe so the new
    // layout takes effect (3d-force-graph doesn't recompute fx/fy/fz on
    // mid-flight option changes).
    stop3D();
    start3D(
      container3d,
      g,
      {
        onNodeClick: (id) => {
          // In impact mode, only tinted (active chain) nodes respond to clicks.
          // The globe renders every node as a sphere -- dim ones are still
          // ray-cast-able, so stray clicks on grey spheres must be swallowed.
          if (impactState && tintColor(impactState.byNode.get(id)) === null) return;
          hideMorningBrief();
          setInspectorCollapsed(false);
          // Show node details immediately from what's already in the graph,
          // then expand its neighbours in the background.
          if (g.hasNode(id)) {
            showNode(g.getNodeAttributes(id).apiNode, g, inspectorExtrasFor(id));
          }
          expandFrom(id).catch(console.error);
        },
        onNodeDoubleClick: (id) => recenterOn(id).catch(console.error),
        onEdgeClick: async (id) => {
          // In impact mode, swallow edge clicks unless at least one endpoint
          // is part of the active impact chain.  The globe's ray-caster fires
          // onLinkClick for edges near the cursor even on background clicks,
          // so without this guard stray clicks open the inspector for
          // unrelated edges during an active run.
          if (impactState) {
            const src = g.hasEdge(id) ? g.source(id) : null;
            const tgt = g.hasEdge(id) ? g.target(id) : null;
            const srcActive = src != null && tintColor(impactState.byNode.get(src)) !== null;
            const tgtActive = tgt != null && tintColor(impactState.byNode.get(tgt)) !== null;
            if (!srcActive && !tgtActive) return;
          }
          hideMorningBrief();
          try {
            const edge = await getEdge(id);
            showEdge(edge);
          } catch (err) {
            console.warn("edge fetch failed", err);
          }
        },
        onBackgroundClick: () => {
          // Empty-space click resets the inspector and brings back the
          // brief — mirrors the 2D renderer's clickStage handler.
          showEmpty();
          showMorningBrief();
        },
      },
      { layout: next === "globe" ? "globe" : "ball", filterState: filters },
    );
    // Re-apply impact tinting when switching to 3D while an impact is active.
    // start3D()'s nodeColor/nodeVisibility closures read _currentImpactState
    // directly, so node tinting is already correct from the first render.
    // applyImpact3D() handles link tinting (scheduled with delays so arcs settle).
    if (impactState) {
      applyImpact3D(g, impactState, filters);
    }
  } else {
    stop3D();
    container3d.hidden = true;
    container.hidden = false;
    renderer.refresh();
  }
}

document.querySelectorAll<HTMLButtonElement>(".view-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    const v = btn.dataset.view as "2d" | "3d" | "globe" | undefined;
    setView(v === "3d" || v === "globe" ? v : "2d");
  });
});

document.querySelectorAll<HTMLButtonElement>(".layout-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    const rawLayout = btn.dataset.layout;
    const next: "force" | "sector" | "bubble" =
      rawLayout === "sector" ? "sector" : rawLayout === "bubble" ? "bubble" : "force";
    if (next === layoutMode) return;
    layoutMode = next;
    document.querySelectorAll<HTMLButtonElement>(".layout-tab").forEach((b) => {
      b.classList.toggle("is-active", b.dataset.layout === next);
    });
    applyLayout();
  });
});

// Keep the 3D view in sync with graph mutations made by recenter/expand calls.
// `refreshAll()` is a thin wrapper so call sites don't need to know which
// view is active.
function refreshAll() {
  renderer.refresh();
  if (is3DRunning()) update3D(g, filters);
}

// Override the existing call sites that previously only refreshed Sigma.
// We monkey-patch in a tiny way: the original functions call renderer.refresh();
// we ALSO call update3D when the 3D renderer is alive. The cheapest path is
// to add a global graph-change listener -- graphology emits events.
//
// Debounce node/edge additions so a bulk mergeFromApi (50 nodes + 100 edges)
// doesn't trigger 150 redundant update3D calls. A single rAF deferred call
// wins the race and fires once per tick at most.
let _3dUpdateScheduled = false;
// Set during _batchLoadGraph to suppress per-batch update3D() calls. Without
// this, the RAF-debounced listener fires between every setTimeout(0) yield and
// triggers update3D() ~8 times per load — each call in globe mode disposes +
// rebuilds 13k TubeGeometry arcs. One final update3D() fires in the finally
// block once the full graph is resident.
let _bulkLoadInProgress = false;
// True while a per-hop streaming impact run is applying events. Used to keep
// the expensive globe arc work to the final `done` event (per-hop globe
// re-tints are skipped; 2D Sigma re-tints every hop).
let _impactStreaming = false;
function _schedule3DUpdate() {
  if (!is3DRunning() || _3dUpdateScheduled || _bulkLoadInProgress) return;
  _3dUpdateScheduled = true;
  requestAnimationFrame(() => {
    _3dUpdateScheduled = false;
    if (is3DRunning()) update3D(g, filters);
  });
}
g.on("nodeAdded", _schedule3DUpdate);
g.on("edgeAdded", _schedule3DUpdate);
g.on("cleared",   () => { if (is3DRunning() && !_bulkLoadInProgress) update3D(g, filters); });

// Resize handling for the 3D canvas.
const ro3d = new ResizeObserver(() => {
  if (!is3DRunning()) return;
  const r = container3d.getBoundingClientRect();
  resize3D(r.width, r.height);
});
ro3d.observe(container3d);
document.addEventListener("keydown", (ev) => {
  // Don't hijack keyboard shortcuts when the user is typing.
  const focused = document.activeElement as HTMLElement | null;
  const typing = focused && (focused.tagName === "INPUT" || focused.tagName === "TEXTAREA");

  if (ev.key === "Escape") {
    if (typing) return;
    loadFullCore().catch(console.error);
    return;
  }

  // [ / ] — shrink or grow all nodes uniformly (2D only; 3D uses its own scale).
  if (ev.key === "[" || ev.key === "]") {
    if (typing) return;
    nodeScale = ev.key === "]"
      ? Math.min(NODE_SCALE_MAX, nodeScale + NODE_SCALE_STEP)
      : Math.max(NODE_SCALE_MIN, nodeScale - NODE_SCALE_STEP);
    renderer.refresh();
  }
});

// ---------------------------------------------------------------------------
// Impact propagation -- news/hypothetical -> tinted overlay
// ---------------------------------------------------------------------------

const impactInput = document.getElementById("impact-input") as HTMLInputElement | null;
const impactInputsList = document.getElementById("impact-inputs-list") as HTMLDivElement | null;
const impactAddNewsBtn = document.getElementById("impact-add-news") as HTMLButtonElement | null;
const impactRunBtn = document.getElementById("impact-run") as HTMLButtonElement | null;
const impactClearBtn = document.getElementById("impact-clear") as HTMLButtonElement | null;
const impactCancelBtn = document.getElementById("impact-cancel-btn") as HTMLButtonElement | null;
const impactStatusEl = document.getElementById("impact-status") as HTMLDivElement | null;
const impactTop5El = document.getElementById("impact-top5") as HTMLDivElement | null;
const impactProviderEl = document.getElementById("impact-provider") as HTMLSelectElement | null;

// AbortController for the in-flight /impact fetch — lets the Cancel button
// terminate the LLM call without a page reload.
let _impactAbortController: AbortController | null = null;

function setImpactStatus(msg: string | null, isError = false): void {
  if (!impactStatusEl) return;
  if (!msg) {
    impactStatusEl.hidden = true;
    impactStatusEl.textContent = "";
    impactStatusEl.classList.remove("impact-status-error");
    return;
  }
  impactStatusEl.hidden = false;
  impactStatusEl.textContent = msg;
  impactStatusEl.classList.toggle("impact-status-error", isError);
}

/** Render the "Top 5 most affected" quick-read strip above the impact bar. */
function renderTop5(impacts: import("./api").ImpactVerdict[]): void {
  if (!impactTop5El) return;
  // Filter to real directional verdicts only; exclude seeds (hop 0 for seeds)
  // since those are always "starting points", not surprising findings.
  const verdicts = impacts
    .filter(v => v.direction !== "no_effect" && v.magnitude > 0.1 && v.hop > 0)
    .sort((a, b) => b.magnitude - a.magnitude)
    .slice(0, 6);  // show up to 6 chips

  if (!verdicts.length) {
    impactTop5El.hidden = true;
    return;
  }

  impactTop5El.innerHTML = "";
  const label = document.createElement("div");
  label.className = "impact-top5-label";
  label.textContent = "Most affected";
  impactTop5El.appendChild(label);

  for (const v of verdicts) {
    const chip = document.createElement("span");
    const dirClass = v.mixed_signals ? "mixed" : v.direction === "positive" ? "positive" : "negative";
    chip.className = `impact-top5-chip ${dirClass}`;
    chip.title = v.reasoning || "";

    const arrow = v.mixed_signals ? "⚡" : v.direction === "positive" ? "▲" : "▼";
    chip.innerHTML =
      `<span class="impact-top5-arrow">${arrow}</span>` +
      `<span>${v.name}</span>` +
      `<span class="impact-top5-mag">${v.magnitude.toFixed(2)}</span>`;
    impactTop5El.appendChild(chip);
  }
  impactTop5El.hidden = false;
}

function clearTop5(): void {
  if (!impactTop5El) return;
  impactTop5El.hidden = true;
  impactTop5El.innerHTML = "";
}

/** Collect all non-empty news texts from the multi-input list. */
function getNewsTexts(): string[] {
  if (!impactInputsList) return impactInput ? [impactInput.value.trim()].filter(Boolean) : [];
  return Array.from(impactInputsList.querySelectorAll<HTMLInputElement>(".impact-news-input"))
    .map(el => el.value.trim())
    .filter(Boolean);
}

/** Update remove-button visibility: hidden when only 1 row exists. */
function _syncRemoveBtns(): void {
  if (!impactInputsList) return;
  const rows = impactInputsList.querySelectorAll(".impact-news-row");
  rows.forEach(row => {
    const btn = row.querySelector<HTMLButtonElement>(".impact-news-remove");
    if (btn) btn.hidden = rows.length <= 1;
  });
}

/** Add a new news-input row to the list. */
function addImpactNewsRow(prefill = ""): void {
  if (!impactInputsList) return;
  const row = document.createElement("div");
  row.className = "impact-news-row";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "impact-news-input";
  input.placeholder = "Another news event…";
  input.autocomplete = "off";
  input.value = prefill;
  // Enter key is handled by delegated listener on impactInputsList; no per-input listener needed.

  const removeBtn = document.createElement("button");
  removeBtn.className = "impact-news-remove";
  removeBtn.title = "Remove this news item";
  removeBtn.textContent = "×";
  removeBtn.addEventListener("click", () => {
    row.remove();
    _syncRemoveBtns();
  });

  row.appendChild(input);
  row.appendChild(removeBtn);
  impactInputsList.appendChild(row);
  _syncRemoveBtns();
  input.focus();
}

if (impactAddNewsBtn) {
  impactAddNewsBtn.addEventListener("click", () => addImpactNewsRow());
}

// Flag to prevent concurrent multi-impact fetches (the AbortController
// only cancels the current one; a second call starting before abort
// completes would still race). Multi-path shares the same _impactAbortController
// mechanism but an extra flag makes the guard explicit for both paths.
let _impactInFlight = false;

async function handleImpactRun(): Promise<void> {
  if (!impactRunBtn) return;
  // Guard: prevent a second concurrent impact run (e.g. rapid double-click
  // on the Run button or Enter key). The button is disabled below, but the
  // check here is authoritative so the first concurrent call wins.
  if (impactRunBtn.disabled) return;
  if (_impactInFlight) return;
  const texts = getNewsTexts();
  if (!texts.length) return;
  const provider = (impactProviderEl?.value as "claude" | "ollama" | undefined) ?? "claude";
  const niceProvider = provider === "claude" ? "Claude" : "Gemma";

  impactRunBtn.disabled = true;
  _impactInFlight = true;
  setImpactStatus(null);
  showImpactOverlay(provider);
  _impactAbortController = new AbortController();

  try {
    if (texts.length === 1) {
      // Single event — streaming /impact/stream path with per-hop reveal.
      // Load the full graph up front so seed/hop nodes exist to tint as
      // events arrive (the old blocking path loaded it after the await).
      if (g.order === 0) {
        hideImpactOverlay();
        await loadFullCore();
        showImpactOverlay(provider);
      }

      const acc = new Map<string, ImpactVerdict>();
      const reapply = (isFinal: boolean) => {
        impactState = buildImpactState(g, { impacts: [...acc.values()] } as ImpactResponse);
        refreshEdgeVisibility();                 // 2D Sigma tint + renderer.refresh()
        // Globe: arc recolour is comparatively heavy; do it once at the end.
        // 2D-only sessions never enter is3DRunning(), so they tint every hop above.
        if (is3DRunning() && isFinal) applyImpact3D(g, impactState, filters);
      };

      _impactStreaming = true;
      let resp: ImpactResponse;
      try {
        resp = await runImpactStream(texts[0], {
          provider,
          signal: _impactAbortController.signal,
          onEvent: (ev) => {
            if (ev.event === "seeds") {
              ev.seeds.forEach((v) => acc.set(v.node_id, v));
              reapply(false);
            } else if (ev.event === "hop") {
              ev.new_impacts.forEach((v) => acc.set(v.node_id, v));
              reapply(false);
              setImpactStatus(`[${niceProvider}] hop ${ev.hop} → ${acc.size} nodes…`);
            } else if (ev.event === "refinement") {
              ev.updated.forEach((v) => acc.set(v.node_id, v));
              reapply(false);
            } else if (ev.event === "error") {
              setImpactStatus(ev.message, true);
            }
          },
        });
      } finally {
        _impactStreaming = false;
      }

      // Finalize from the canonical `done` payload — identical to the old path.
      const hasAnySeeds = resp.seed != null || (Array.isArray(resp.seeds) && resp.seeds.length > 0);
      if ((resp as any).no_neighbors) {
        setImpactStatus(resp.error || "Seed node has no recorded supply chain connections.", true);
        return;
      }
      if (resp.error && !hasAnySeeds) {
        setImpactStatus(`Failed: ${resp.error}`, true);
        return;
      }
      if (!hasAnySeeds) {
        setImpactStatus(`No seed identified — try a more specific entity name.`, true);
        return;
      }

      acc.clear();
      resp.impacts.forEach((v) => acc.set(v.node_id, v));
      reapply(true);                              // final globe tint
      if (impactClearBtn) impactClearBtn.hidden = false;
      const seedNames = resp.seeds && resp.seeds.length > 0
        ? resp.seeds.map((s) => `${s.name} (${s.direction})`).join(", ")
        : resp.seed ? `${resp.seed.name} (${resp.seed.direction})` : "unknown";
      setImpactStatus(
        `[${niceProvider}] Seeds: ${seedNames} → ${resp.impacts.length} nodes across ${resp.max_hops || 3} hops`,
      );
      renderTop5(resp.impacts);
      saveToArchive(texts[0], provider, resp);

    } else {
      // Multi-event — /impact/multi path
      const resp: MultiImpactResponse = await runMultiImpact(texts, { provider, signal: _impactAbortController.signal });
      if (resp.error) {
        setImpactStatus(`Failed: ${resp.error}`);
        return;
      }
      // Same guard as single-event: ensure the graph is populated first.
      if (g.order === 0) {
        hideImpactOverlay();
        await loadFullCore();
      }
      impactState = buildMultiImpactState(g, resp);
      refreshEdgeVisibility();
      applyImpact3D(g, impactState, filters);
      if (impactClearBtn) impactClearBtn.hidden = false;
      const mixedCount = resp.mixed_signal_nodes ?? 0;
      setImpactStatus(
        `[${niceProvider}] ${texts.length} events merged → ${resp.total_nodes ?? resp.merged.length} nodes` +
        (mixedCount > 0 ? `, ${mixedCount} amber (mixed signals)` : ""),
      );
      renderTop5(resp.merged);
      saveMultiToArchive(texts, provider, resp);
    }
  } catch (err) {
    if ((err as Error).name === "AbortError") {
      setImpactStatus("Impact trace cancelled.");
    } else {
      console.error(err);
      setImpactStatus(`Error: ${String((err as Error).message || err)}`);
    }
  } finally {
    _impactAbortController = null;
    _impactInFlight = false;
    hideImpactOverlay();
    impactRunBtn.disabled = false;
  }
}

function handleImpactClear(): void {
  impactState = null;
  refreshEdgeVisibility();
  applyImpact3D(g, null, filters);
  if (impactClearBtn) impactClearBtn.hidden = true;
  setImpactStatus(null);
  clearTop5();
  // Reset added-headline tracking so the morning brief buttons re-enable.
  _addedHeadlines.clear();
  if (_currentHeadlines.length > 0) renderMorningBrief(_currentHeadlines);
  // Reset to single input row
  if (impactInputsList) {
    impactInputsList.innerHTML = "";
    const row = document.createElement("div");
    row.className = "impact-news-row";
    const input = document.createElement("input");
    input.id = "impact-input";
    input.type = "text";
    input.className = "impact-news-input";
    input.placeholder = "Describe a news event — e.g. 'Tesla secures battery deal with Panasonic'";
    input.autocomplete = "off";
    // Enter key is handled by delegated listener on impactInputsList.
    const removeBtn = document.createElement("button");
    removeBtn.className = "impact-news-remove";
    removeBtn.title = "Remove this news item";
    removeBtn.textContent = "×";
    removeBtn.hidden = true;
    removeBtn.addEventListener("click", () => { row.remove(); _syncRemoveBtns(); });
    row.appendChild(input);
    row.appendChild(removeBtn);
    impactInputsList.appendChild(row);
  } else if (impactInput) {
    impactInput.value = "";
  }
}

if (impactRunBtn) impactRunBtn.addEventListener("click", () => { handleImpactRun().catch(console.error); });
// Use event delegation on the container so newly-created rows (after Clear)
// also trigger the run — stale references to individual input elements break.
if (impactInputsList) {
  impactInputsList.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.target as HTMLElement).classList.contains("impact-news-input")) {
      ev.preventDefault();
      handleImpactRun().catch(console.error);
    }
  });
} else if (impactInput) {
  impactInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); handleImpactRun().catch(console.error); }
  });
}
if (impactClearBtn) impactClearBtn.addEventListener("click", handleImpactClear);
if (impactCancelBtn) impactCancelBtn.addEventListener("click", () => {
  _impactAbortController?.abort();
});

// ---------------------------------------------------------------------------
// Morning brief — daily top-5 economic headlines in the inspector panel.
// Shown when nothing is selected; hidden when node/edge detail is active.
// ---------------------------------------------------------------------------

const mbBriefEl  = document.getElementById("morning-brief") as HTMLDivElement | null;
const mbListEl   = document.getElementById("mb-list")        as HTMLOListElement | null;
const mbDateEl   = document.getElementById("mb-date")        as HTMLSpanElement | null;

// Track which headline indices have already been added to the impact bar.
const _addedHeadlines = new Set<number>();
// Keep references so re-renders can restore added state.
let _currentHeadlines: Headline[] = [];

const MB_VISIBLE_DEFAULT  = 5;
const MB_VISIBLE_EXPANDED = 10;
let _mbExpanded = false;

function showMorningBrief(): void {
  if (mbBriefEl) mbBriefEl.hidden = false;
}
function hideMorningBrief(): void {
  if (mbBriefEl) mbBriefEl.hidden = true;
}

function _addHeadlineToBar(h: Headline, idx: number, li: HTMLLIElement, btn: HTMLButtonElement): void {
  // If the first input row is empty, fill it; otherwise append a new row.
  const rows = impactInputsList?.querySelectorAll<HTMLInputElement>(".impact-news-input") ?? [];
  const firstInput = rows[0];
  if (firstInput && !firstInput.value.trim()) {
    firstInput.value = h.text;
  } else {
    addImpactNewsRow(h.text);
  }
  // Mark dimmed in the brief panel.
  _addedHeadlines.add(idx);
  li.classList.add("mb-added");
  btn.textContent = "✓";
  btn.title = "Added to impact bar";
}

function renderMorningBrief(headlines: Headline[]): void {
  if (!mbListEl || !mbBriefEl) return;
  _currentHeadlines = headlines;

  if (mbDateEl) {
    const now = new Date();
    mbDateEl.textContent = now.toLocaleDateString("en-US", {
      weekday: "short", month: "short", day: "numeric",
    });
  }

  // Clamp expansion: if we received fewer than the expanded limit, collapse.
  if (headlines.length <= MB_VISIBLE_DEFAULT) _mbExpanded = false;

  const visible = _mbExpanded ? MB_VISIBLE_EXPANDED : MB_VISIBLE_DEFAULT;
  const slice   = headlines.slice(0, visible);

  mbListEl.innerHTML = "";
  slice.forEach((h, idx) => {
    const li = document.createElement("li");
    li.className = "mb-item" + (_addedHeadlines.has(idx) ? " mb-added" : "");

    const content = document.createElement("div");
    content.className = "mb-content";

    const headlineSpan = document.createElement("span");
    headlineSpan.className = "mb-headline";
    headlineSpan.textContent = h.text;

    // Meta line: "Jun 8 · Reuters Business"
    const metaSpan = document.createElement("span");
    metaSpan.className = "mb-source";
    let metaText = h.source;
    if (h.pub_date) {
      // Parse "YYYY-MM-DD" without timezone conversion (treat as local date).
      const [y, m, d] = h.pub_date.split("-").map(Number);
      const label = new Date(y, m - 1, d).toLocaleDateString("en-US", {
        month: "short", day: "numeric",
      });
      metaText = `${label} · ${h.source}`;
    }
    metaSpan.textContent = metaText;

    content.appendChild(headlineSpan);
    content.appendChild(metaSpan);

    const addBtn = document.createElement("button");
    addBtn.className = "mb-add-btn";
    addBtn.type = "button";
    addBtn.title = _addedHeadlines.has(idx) ? "Added to impact bar" : "Add to impact trace bar";
    addBtn.textContent = _addedHeadlines.has(idx) ? "✓" : "+";
    addBtn.setAttribute("aria-label", `Add "${h.text}" to impact bar`);

    if (!_addedHeadlines.has(idx)) {
      addBtn.addEventListener("click", () => _addHeadlineToBar(h, idx, li, addBtn));
    }

    li.appendChild(content);
    li.appendChild(addBtn);
    mbListEl.appendChild(li);
  });

  // Remove any existing toggle button before (re)inserting.
  mbBriefEl.querySelector(".mb-toggle-btn")?.remove();

  if (headlines.length > MB_VISIBLE_DEFAULT) {
    const toggleBtn = document.createElement("button");
    toggleBtn.className = "mb-toggle-btn";
    toggleBtn.type = "button";
    if (_mbExpanded) {
      toggleBtn.textContent = "Show less ▴";
      toggleBtn.title = `Show only ${MB_VISIBLE_DEFAULT} headlines`;
    } else {
      const extra = Math.min(MB_VISIBLE_EXPANDED, headlines.length) - MB_VISIBLE_DEFAULT;
      toggleBtn.textContent = `Show ${extra} more ▾`;
      toggleBtn.title = `Show ${MB_VISIBLE_EXPANDED} headlines`;
    }
    toggleBtn.addEventListener("click", () => {
      _mbExpanded = !_mbExpanded;
      renderMorningBrief(_currentHeadlines);
    });
    mbBriefEl.appendChild(toggleBtn);
  }
}

function initMorningBrief(): void {
  if (!mbListEl) return;
  // Show skeleton loading state immediately.
  mbListEl.innerHTML = '<li class="mb-loading">Fetching today\'s headlines…</li>';
  showMorningBrief();

  fetchHeadlines()
    .then((headlines) => {
      if (!headlines.length) {
        if (mbListEl) mbListEl.innerHTML = '<li class="mb-loading">No headlines available.</li>';
        return;
      }
      renderMorningBrief(headlines);
    })
    .catch((err) => {
      console.warn("Morning brief fetch failed:", err);
      if (mbListEl) mbListEl.innerHTML = '<li class="mb-loading">Headlines unavailable — API offline?</li>';
    });
}

initMorningBrief();

// ---------------------------------------------------------------------------
// Collapsible inspector panel
// ---------------------------------------------------------------------------

// Lifted to module scope so node/edge click handlers can expand the panel.
const _inspectorAppEl     = document.getElementById("app");
const _inspectorToggleBtn = document.getElementById("inspector-toggle") as HTMLButtonElement | null;

function setInspectorCollapsed(collapsed: boolean): void {
  if (!_inspectorAppEl || !_inspectorToggleBtn) return;
  _inspectorAppEl.classList.toggle("inspector-collapsed", collapsed);
  _inspectorToggleBtn.textContent = collapsed ? "›" : "‹";
  _inspectorToggleBtn.title = collapsed ? "Expand inspector" : "Collapse inspector";
  try { localStorage.setItem("inspectorCollapsed", String(collapsed)); } catch { /* quota or private mode */ }
}

// Default: collapsed unless the user explicitly opened it in a previous session.
(function wireInspectorToggle() {
  if (!_inspectorAppEl || !_inspectorToggleBtn) return;
  let stored: string | null = null;
  try { stored = localStorage.getItem("inspectorCollapsed"); } catch { /* private mode */ }
  // Default: EXPANDED so the Morning Brief is visible on first visit.
  // Only collapse if the user explicitly did so ("true" stored).
  setInspectorCollapsed(stored === "true");

  _inspectorToggleBtn.addEventListener("click", () => {
    setInspectorCollapsed(!_inspectorAppEl!.classList.contains("inspector-collapsed"));
  });
})();

// ---------------------------------------------------------------------------
// Impact archive — render list + restore
// ---------------------------------------------------------------------------

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function relativeTime(ts: number): string {
  const diff = Date.now() - ts;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

function expiresIn(expiresAt: number): string {
  const diff = expiresAt - Date.now();
  if (diff <= 0) return "expired";
  if (diff < 3_600_000) return `expires in ${Math.ceil(diff / 60_000)}m`;
  return `expires in ${Math.ceil(diff / 3_600_000)}h`;
}

function renderArchiveList(): void {
  const listEl = document.getElementById("archive-list");
  if (!listEl) return;
  const entries = loadArchive();
  if (entries.length === 0) {
    listEl.innerHTML =
      '<div class="archive-empty">No saved traces yet.<br>Run an impact trace and it will appear here automatically.</div>';
    return;
  }
  listEl.innerHTML = entries
    .map(
      (e) => `
    <div class="archive-entry" data-id="${e.id}">
      <div class="archive-entry-header">
        <span class="archive-entry-seed ${e.seedDirection}">${escapeHtml(e.seedName)}</span>
        <button class="archive-entry-delete" data-id="${e.id}" title="Delete entry">×</button>
      </div>
      <div class="archive-entry-text">${escapeHtml(e.text)}</div>
      <div class="archive-entry-meta">
        <span>${escapeHtml(String(e.nodesCount))} nodes</span>
        <span>${escapeHtml(String(e.maxHops))} hops</span>
        <span>${escapeHtml(e.provider)}</span>
      </div>
      <div class="archive-entry-time">
        <span>${relativeTime(e.timestamp)}</span>
        <span class="ae-expiry">${expiresIn(e.expiresAt)}</span>
      </div>
    </div>`,
    )
    .join("");

  listEl.querySelectorAll<HTMLElement>(".archive-entry").forEach((el) => {
    el.addEventListener("click", (ev) => {
      const target = ev.target as HTMLElement;
      if (target.classList.contains("archive-entry-delete")) {
        ev.stopPropagation();
        removeFromArchive(target.dataset.id!);
        renderArchiveList();
        return;
      }
      const id = el.dataset.id!;
      const entry = loadArchive().find((e) => e.id === id);
      if (entry) restoreFromArchive(entry).catch(console.error);
    });
  });
}

async function restoreFromArchive(entry: ArchiveEntry): Promise<void> {
  // Ensure the full graph is loaded so all impacted nodes are present.
  await loadFullCore();

  // Reset headline "added" tracking so the morning brief reflects the
  // restored state cleanly (not the previous session's added state).
  _addedHeadlines.clear();

  // Clear any previously-selected node/edge so the inspector doesn't show
  // stale data from a different trace or interaction.
  showEmpty();
  // Reset the globe camera to the default whole-globe view so the restored
  // trace is visible from a sensible vantage point, not wherever the user
  // last dragged/zoomed.
  resetGlobeCamera();

  if (entry.isMulti && entry.multiResponse) {
    impactState = buildMultiImpactState(g, entry.multiResponse);
    refreshEdgeVisibility();
    applyImpact3D(g, impactState, filters);
    if (impactClearBtn) impactClearBtn.hidden = false;
    renderTop5(entry.multiResponse.merged ?? []);
    // Restore multi-input rows
    if (impactInputsList && entry.texts && entry.texts.length > 1) {
      impactInputsList.innerHTML = "";
      entry.texts.forEach((t, i) => {
        if (i === 0) {
          const row = document.createElement("div");
          row.className = "impact-news-row";
          const inp = document.createElement("input");
          inp.id = "impact-input";
          inp.type = "text";
          inp.className = "impact-news-input";
          inp.autocomplete = "off";
          inp.value = t;
          // Enter key is handled by delegated listener on impactInputsList.
          const rb = document.createElement("button");
          rb.className = "impact-news-remove";
          rb.textContent = "×";
          rb.hidden = true;
          rb.addEventListener("click", () => { row.remove(); _syncRemoveBtns(); });
          row.appendChild(inp); row.appendChild(rb);
          impactInputsList.appendChild(row);
        } else {
          addImpactNewsRow(t);
        }
      });
    } else if (impactInput) {
      impactInput.value = entry.text;
    }
    setImpactStatus(
      `[Archived] ${entry.texts?.length ?? 1} events → ${entry.nodesCount} nodes (multi-news)`,
    );
  } else if (entry.response) {
    impactState = buildImpactState(g, entry.response);
    refreshEdgeVisibility();
    applyImpact3D(g, impactState, filters);
    if (impactClearBtn) impactClearBtn.hidden = false;
    renderTop5(entry.response.impacts ?? []);
    // Prefer the live first-row input (rebuilt by handleImpactClear) over the
    // possibly-detached module-level impactInput reference.
    const _firstInput = impactInputsList?.querySelector<HTMLInputElement>(".impact-news-input") ?? impactInput;
    if (_firstInput) _firstInput.value = entry.text;
    setImpactStatus(
      `[Archived] ${entry.seedName} (${entry.seedDirection}) → ${entry.nodesCount} nodes across ${entry.maxHops} hops`,
    );
  }
}

// ---------------------------------------------------------------------------
// Sidebar tab switching (Filters ↔ Archive)
// ---------------------------------------------------------------------------

(function wireSidebarTabs() {
  const tabs = document.querySelectorAll<HTMLButtonElement>(".sidebar-tab");
  const panelFilters = document.getElementById("panel-filters");
  const panelArchive = document.getElementById("panel-archive");
  if (!panelFilters || !panelArchive) return;

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.tab;
      tabs.forEach((t) => {
        t.classList.toggle("is-active", t.dataset.tab === target);
        t.setAttribute("aria-selected", String(t.dataset.tab === target));
      });
      panelFilters.hidden = target !== "filters";
      panelArchive.hidden = target !== "archive";
      if (target === "archive") renderArchiveList();
    });
  });
})();

// ---------------------------------------------------------------------------
// Initial load
// ---------------------------------------------------------------------------

(async function init() {
  if (OPEN_MODE === "search") return;
  try {
    await loadFullCore();
  } catch (err) {
    console.error("initial load failed", err);
  }
})();

// ---------------------------------------------------------------------------
// Landing intro — shown on every page load.
// The overlay renders its own standalone Three.js globe (no nodes/edges) and
// plays the "So What?" animation. After dismiss, activate Globe mode so the
// app opens on the globe view directly.
// ---------------------------------------------------------------------------

// Pre-fetch: populate g with node/edge data while the landing animation plays.
// skipLayout=true so FA2 (synchronous, ~2 s for 5 000+ nodes) never runs on
// the main thread during the animation — that would freeze the spinning globe.
// Layout is deferred to the post-landing loadFullCore() call (Tier 2.25).
loadFullCore(true).catch(console.error);

runLanding().then(() => {
  // ── Futuristic launch transition ──────────────────────────────────────
  // A bright scan line sweeps the screen while the dashboard flashes in.
  const scanEl = document.createElement("div");
  scanEl.className = "launch-scan-overlay";
  document.body.appendChild(scanEl);

  // Flash-overexposure reveal on the main app.
  const appEl = document.getElementById("app");
  appEl?.classList.add("dashboard-reveal");

  // Switch to Globe mode. start3D() reads whatever is already in `g`, so
  // if the background fetch finished the globe populates instantly.
  const globeTab = document.querySelector<HTMLButtonElement>('[data-view="globe"]');
  globeTab?.click();

  // Coalesces with the in-flight background call via _fvInFlight guard —
  // no double-fetch. Ensures the full graph is loaded even if the user
  // dismissed the landing before the background fetch completed.
  // After loading, explicitly refresh the Globe so it never shows 0 nodes:
  // if globeTab?.click() fired while g.order === 0, start3D returned an
  // empty scene and the nodeAdded events may have already resolved before
  // the .then() below runs — this guarantees a final sync.
  loadFullCore()
    .then(() => { if (is3DRunning()) update3D(g, filters); })
    .catch(console.error);

  // After scan line finishes (~700 ms), fade the overlay out and clean up.
  setTimeout(() => {
    scanEl.classList.add("lt-done");
    setTimeout(() => {
      scanEl.remove();
      appEl?.classList.remove("dashboard-reveal");
    }, 450);
  }, 700);
}).catch(console.error);
