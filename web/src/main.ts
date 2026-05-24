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
} from "./bubbles";
import {
  is3DRunning,
  resize3D,
  start3D,
  stop3D,
  update3D,
} from "./render3d";
import { wireFilters, type FilterState } from "./ui/filters";
import { showEdge, showEmpty, showNode } from "./ui/inspector";
import { wireSearch } from "./ui/search";
import { startStatusPolling } from "./ui/status";

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
  defaultEdgeType: "arrow",
  labelDensity: 1,
  labelGridCellSize: 80,
  labelRenderedSizeThreshold: 6,
  labelFont: "Inter, system-ui, sans-serif",
  // Dark-mode label color, matched to --text in styles.css.
  labelColor: { color: "#e8e3da" },
  labelSize: 11,
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

refreshEdgeVisibility(); // initial pass (everything visible)

function refreshEdgeVisibility(): void {
  renderer.setSetting("edgeReducer", (eid, eattrs) => {
    if (layoutMode === "bubble" && hiddenForBubble.edges.has(eid)) {
      return { ...eattrs, hidden: true };
    }
    const ok = filters.types.includes(eattrs.edgeType);
    const isProv = eattrs.apiEdge.attributes.below_threshold;
    return {
      ...eattrs,
      hidden: !ok || (isProv && !filters.includeProvisional),
    };
  });
  renderer.setSetting("nodeReducer", (id, nattrs) => {
    if (layoutMode === "bubble" && hiddenForBubble.nodes.has(id)) {
      return { ...nattrs, hidden: true, label: "" };
    }
    const isProv = !!nattrs.apiNode.attributes.provisional;
    const hide = isProv && !filters.includeProvisional;
    // Bubbles always show their label; regular nodes use the displayLabel
    // heuristic (top-N hubs labeled, hover-only otherwise).
    const isBubbleNode = isBubble(id);
    return {
      ...nattrs,
      label: isBubbleNode ? nattrs.label : (nattrs.displayLabel || ""),
      hidden: hide,
    };
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
  if (center) showNode(center, g);
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
  // Bubble-mode special case: clicking a sector hub toggles expansion;
  // clicking a company INSIDE an expanded sector collapses that sector
  // back. Both happen instantly (no double-click delay).
  if (layoutMode === "bubble") {
    if (isBubble(id)) {
      const sector = sectorOfBubble(id);
      toggleSector(sector);
      return;
    }
    // Company inside an expanded sector -- toggle that sector closed.
    const a = g.getNodeAttributes(id).apiNode.attributes;
    if (a.sector && expandedSectors.has(a.sector)) {
      toggleSector(a.sector);
      return;
    }
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
  showNode(attrs.apiNode, g);
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
