// Entrypoint. Wires Sigma + UI together.

import Sigma from "sigma";

import {
  ApiEdge,
  ApiNode,
  getEdge,
  getEgo,
  getNode,
  getSubgraph,
  onLoadingChange,
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
  is3DRunning,
  resize3D,
  start3D,
  stop3D,
  update3D,
} from "./render3d";
import { wireFilters, type FilterState } from "./ui/filters";
import { showEdge, showEmpty, showNode, type NodeExtras } from "./ui/inspector";
import { wireSearch } from "./ui/search";
import { startStatusPolling } from "./ui/status";
import { describeNode, runImpact, type ImpactResponse } from "./api";
import {
  buildImpactState,
  dimColor,
  tintColor,
  tintColorRGB,
  type ImpactState,
} from "./impact";

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
  defaultEdgeColor: "#c4c8ce",
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
let filters: FilterState = { types: [], includeProvisional: false, includeInferred: false };

// Layout mode for the 2D view. Force = FA2; sector = GICS clusters;
// bubble = each sector collapsed into a clickable hub with expand/collapse.
let layoutMode: "force" | "sector" | "bubble" = "force";
const expandedSectors = new Set<string>();
let hiddenForBubble = { nodes: new Set<string>(), edges: new Set<string>() };

function applyLayout() {
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
      runLayout(g, 220);
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
});

// Impact-overlay state. When set, the node + edge reducers below
// tint/dim everything accordingly. The 3D mesh tinting is applied
// separately in applyImpactToScene().
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
    renderer.setSetting("defaultEdgeColor", "#c4c8ce");
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
    // Impact overlay: edges in the impact chain pop, others dim
    // heavily so the affected subgraph reads through the rest.
    if (impactState) {
      const inChain = impactState.chainEdges.has(eid);
      return {
        ...eattrs,
        hidden: baseHidden,
        // Translucent rgba doesn't work at this edge density -- thousands
        // of overlapping 20%-alpha lines composite up to bright white.
        // Use a dark opaque grey only ~3 levels above the page background
        // so non-chain edges visually recede instead of stacking.
        color: inChain ? "#e8e3da" : "#1a1d22",
        size: inChain ? (eattrs.size ?? 1) * 1.4 : (eattrs.size ?? 1) * 0.4,
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
    const hide = isProv && !filters.includeProvisional;
    const label = isBubbleNode ? nattrs.label : (nattrs.displayLabel || "");
    if (impactState) {
      const verdict = impactState.byNode.get(id);
      const tint = tintColor(verdict);
      // Dark opaque grey for off-chain -- close to the page background
      // so non-impacted nodes recede without alpha-stacking into bright
      // smudges. Tinted nodes pop.
      return {
        ...nattrs,
        label,
        hidden: hide,
        color: tint ?? "#22262c",
      };
    }
    return { ...nattrs, label, hidden: hide };
  });
  renderer.refresh();
}

// ---------------------------------------------------------------------------
// Loaders
// ---------------------------------------------------------------------------

async function loadFullCore(): Promise<void> {
  // /subgraph seeded at SEC at hops=3 covers every filer + their high-confidence
  // neighbors. Provisional layer respects the user's current toggle (default off).
  const resp = await getSubgraph(FULL_VIEW_SEED, {
    hops: FULL_VIEW_HOPS,
    includeProvisional: filters.includeProvisional,
    includeInferred: filters.includeInferred,
  });
  replaceGraph(g, resp.nodes, resp.edges);
  restyleAfterMerge(g);
  applyLayout();
  cameraReset();
}

async function recenterOn(id: string): Promise<void> {
  const resp = await getEgo(id, {
    types: ["supplies", "customer_of", "competes_with", "regulated_by"],
    includeProvisional: filters.includeProvisional,
    includeInferred: filters.includeInferred,
  });
  replaceGraph(g, resp.nodes, resp.edges);
  restyleAfterMerge(g);
  applyLayout();
  cameraReset();
  // Show the focused node in the inspector.
  const center = resp.nodes.find((n) => n.key === resp.center);
  if (center) showNode(center, g, inspectorExtrasFor(center.key));
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
    runLayout(g, 120);
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
  if (pendingClick) {
    clearTimeout(pendingClick.timer);
    pendingClick = null;
  }
  pendingClick = {
    id,
    timer: window.setTimeout(() => {
      pendingClick = null;
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
renderer.on("enterNode", (event) => {
  const attrs = g.getNodeAttributes(event.node);
  showNode(attrs.apiNode, g, inspectorExtrasFor(event.node));
});
renderer.on("leaveNode", () => {
  // Keep the panel pinned to whatever the user last selected; don't blank it
  // on hover-out -- that's annoying when reading provenance.
});

renderer.on("clickEdge", async (event) => {
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
  // Click on empty canvas resets the inspector.
  showEmpty();
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
        onNodeClick: (id) => expandFrom(id).catch(console.error),
        onNodeDoubleClick: (id) => recenterOn(id).catch(console.error),
        onEdgeClick: async (id) => {
          try {
            const edge = await getEdge(id);
            showEdge(edge);
          } catch (err) {
            console.warn("edge fetch failed", err);
          }
        },
      },
      { layout: next === "globe" ? "globe" : "ball" },
    );
    // CRITICAL: re-apply impact tinting if an impact run is active.
    // start3D() creates a fresh scene with default colours; without this
    // the user switches 2D -> 3D/Globe and loses the red/green tint
    // even though impactState is still set. Globe arcs need a moment
    // for buildArcs() rAF callbacks to populate geometry, so we
    // schedule the impact re-tint a few times to win whichever frame
    // wins last.
    if (impactState) {
      const reapply = () => applyImpactToScene(impactState);
      requestAnimationFrame(reapply);
      setTimeout(reapply, 200);
      setTimeout(reapply, 800);
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
    const next = (btn.dataset.layout as "force" | "sector" | undefined) ?? "force";
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
  if (is3DRunning()) update3D(g);
}

// Override the existing call sites that previously only refreshed Sigma.
// We monkey-patch in a tiny way: the original functions call renderer.refresh();
// we ALSO call update3D when the 3D renderer is alive. The cheapest path is
// to add a global graph-change listener -- graphology emits events.
g.on("nodeAdded", () => { if (is3DRunning()) update3D(g); });
g.on("edgeAdded", () => { if (is3DRunning()) update3D(g); });
g.on("cleared",   () => { if (is3DRunning()) update3D(g); });

// Resize handling for the 3D canvas.
const ro3d = new ResizeObserver(() => {
  if (!is3DRunning()) return;
  const r = container3d.getBoundingClientRect();
  resize3D(r.width, r.height);
});
ro3d.observe(container3d);
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  // Don't hijack Esc when the user is typing in the search box -- they're
  // likely trying to close the dropdown / clear the input.
  const focused = document.activeElement as HTMLElement | null;
  if (focused && (focused.tagName === "INPUT" || focused.tagName === "TEXTAREA")) return;
  loadFullCore().catch(console.error);
});

// ---------------------------------------------------------------------------
// Impact propagation -- news/hypothetical -> tinted overlay
// ---------------------------------------------------------------------------

function applyImpactToScene(state: ImpactState | null): void {
  // 3D / Globe mesh tinting. Walk the live force-graph instance's
  // scene; for each node mesh, replace its material colour with the
  // verdict tint (or a dim grey if outside the chain). Reset on null.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const inst = (window as any).__force3D;
  if (!inst) return;
  const scene = inst.scene();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  scene.traverse((obj: any) => {
    if (obj.__graphObjType === "node" && obj.material && obj.material.color) {
      const data = obj.__data;
      if (state) {
        const verdict = data ? state.byNode.get(data.id) : undefined;
        const tint = tintColorRGB(verdict);
        if (tint) {
          obj.material.color.setRGB(tint.r, tint.g, tint.b);
          obj.material.opacity = 1.0;
          obj.material.transparent = true;
        } else {
          // Dark opaque grey -- alpha-stacking thousands of translucent
          // node spheres produces a bright glow in WebGL; opaque dark
          // recedes cleanly against the dark background.
          obj.material.color.setRGB(0.13, 0.15, 0.17);
          obj.material.opacity = 1.0;
          obj.material.transparent = false;
        }
      } else if (data && data.color) {
        obj.material.color.set(data.color);
        obj.material.opacity = 0.95;
        obj.material.transparent = true;
      }
      obj.material.needsUpdate = true;
    } else if (obj.__graphObjType === "link" && obj.material && obj.material.color) {
      const data = obj.__data;
      if (state) {
        const src = data && data.source && data.source.id;
        const tgt = data && data.target && data.target.id;
        const inChain = src && tgt && state.byNode.has(src) && state.byNode.has(tgt);
        if (inChain) {
          obj.material.color.setRGB(0.91, 0.89, 0.85);
          obj.material.opacity = 0.95;
          obj.material.transparent = true;
        } else {
          // Same reasoning: dark opaque, not translucent.
          obj.material.color.setRGB(0.11, 0.12, 0.14);
          obj.material.opacity = 1.0;
          obj.material.transparent = false;
        }
      } else if (data && data.color) {
        obj.material.color.set(data.color);
        obj.material.opacity = data.below ? 0.30 : 0.75;
        obj.material.transparent = true;
      }
      obj.material.needsUpdate = true;
    }
  });
}

const impactInput = document.getElementById("impact-input") as HTMLInputElement | null;
const impactRunBtn = document.getElementById("impact-run") as HTMLButtonElement | null;
const impactClearBtn = document.getElementById("impact-clear") as HTMLButtonElement | null;
const impactStatusEl = document.getElementById("impact-status") as HTMLDivElement | null;
const impactProviderEl = document.getElementById("impact-provider") as HTMLSelectElement | null;

function setImpactStatus(msg: string | null): void {
  if (!impactStatusEl) return;
  if (!msg) { impactStatusEl.hidden = true; impactStatusEl.textContent = ""; return; }
  impactStatusEl.hidden = false;
  impactStatusEl.textContent = msg;
}

async function handleImpactRun(): Promise<void> {
  if (!impactInput || !impactRunBtn) return;
  const text = impactInput.value.trim();
  if (!text) return;
  const provider = (impactProviderEl?.value as "claude" | "ollama" | undefined) ?? "claude";
  impactRunBtn.disabled = true;
  const niceProvider = provider === "claude" ? "Claude" : "Gemma";
  // Honest estimate: hop 3 fans out widely on broad-scope news
  // (war, sanctions, recession) and a 3-hop walk runs 10-20 parallel
  // LLM calls. Wall time scales with the worst hop's chunk count.
  const eta = provider === "claude" ? "~1-3 min" : "~3-8 min";
  setImpactStatus(`Asking ${niceProvider}... (${eta} for a 3-hop walk; longer for global-scope news)`);
  try {
    const resp: ImpactResponse = await runImpact(text, { provider });
    if (resp.error || !resp.seed) {
      setImpactStatus(`Failed: ${resp.error || "no seed identified"}`);
      return;
    }
    impactState = buildImpactState(g, resp);
    refreshEdgeVisibility();
    applyImpactToScene(impactState);
    if (impactClearBtn) impactClearBtn.hidden = false;
    setImpactStatus(
      `[${niceProvider}] Seed: ${resp.seed.name} (${resp.seed.direction}) -> ${resp.impacts.length} nodes touched across ${resp.max_hops || 3} hops`,
    );
  } catch (err) {
    console.error(err);
    setImpactStatus(`Error: ${String((err as Error).message || err)}`);
  } finally {
    impactRunBtn.disabled = false;
  }
}

function handleImpactClear(): void {
  impactState = null;
  refreshEdgeVisibility();
  applyImpactToScene(null);
  if (impactClearBtn) impactClearBtn.hidden = true;
  if (impactInput) impactInput.value = "";
  setImpactStatus(null);
}

if (impactRunBtn) impactRunBtn.addEventListener("click", () => { handleImpactRun().catch(console.error); });
if (impactInput) impactInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); handleImpactRun().catch(console.error); }
});
if (impactClearBtn) impactClearBtn.addEventListener("click", handleImpactClear);

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
