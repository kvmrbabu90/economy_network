// 3D force-directed view backed by three.js. Reads the SAME graphology
// instance Sigma is using -- when nodes / edges are merged into `g` by the
// rest of the app, we just call update3D() to re-paint. Toggling the view
// off (returning to Sigma 2D) tears down the WebGL context.
//
// Click handlers reuse the app's existing recenterOn / expandFrom helpers
// via the optional callbacks parameter, so the 3D view supports the same
// gestures as the 2D view: click -> expand, double-click -> recenter.

// 3d-force-graph ships permissive TS types that don't model the factory
// shape cleanly. Cast to `any` at the import site; the runtime API is
// stable and well-documented at https://github.com/vasturiano/3d-force-graph.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
import ForceGraph3DLib from "3d-force-graph";
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const ForceGraph3D: any = ForceGraph3DLib as any;

import type { EconGraph } from "./graph";

export interface View3DCallbacks {
  onNodeClick?: (id: string) => void;
  onNodeDoubleClick?: (id: string) => void;
  onEdgeClick?: (edgeId: string) => void;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let instance: any = null;
let mountedContainer: HTMLElement | null = null;

// Convert the shared graphology graph into the {nodes, links} shape
// 3d-force-graph wants. We strip the heavy provenance payload off the
// edges (the inspector still reads it from the underlying graphology).
interface ForceNode {
  id: string;
  label: string;
  color: string;
  size: number;
  apiType: string;
  provisional: boolean;
}

interface ForceLink {
  source: string;
  target: string;
  edgeId: string;
  edgeType: string;
  color: string;
  below: boolean;
}

function toForceData(g: EconGraph) {
  const nodes: ForceNode[] = [];
  const links: ForceLink[] = [];
  g.forEachNode((id, attrs) => {
    const apiNode = attrs.apiNode;
    nodes.push({
      id,
      label: attrs.label,
      color: attrs.color,
      // 3d-force-graph uses node "val" for sphere radius; map from the size
      // we already computed for Sigma (degree-based, 3..18).
      size: Math.max(2, attrs.size * 0.7),
      apiType: apiNode.attributes.type,
      provisional: apiNode.attributes.provisional,
    });
  });
  g.forEachEdge((id, attrs, src, tgt) => {
    links.push({
      source: src,
      target: tgt,
      edgeId: id,
      edgeType: attrs.edgeType,
      color: attrs.color,
      below: attrs.apiEdge.attributes.below_threshold,
    });
  });
  return { nodes, links };
}

export function start3D(
  container: HTMLElement,
  g: EconGraph,
  cbs: View3DCallbacks = {},
): void {
  if (instance) stop3D();
  mountedContainer = container;

  instance = ForceGraph3D({ controlType: "orbit" })(container)
    .backgroundColor("#14171a")  // match --bg in styles.css
    .graphData(toForceData(g))
    .nodeLabel((n: ForceNode) => `<span style="color:#e8e3da">${n.label}</span>`)
    .nodeColor((n: ForceNode) => n.color)
    .nodeVal((n: ForceNode) => n.size)
    .nodeResolution(12)
    .nodeOpacity(0.95)
    .linkColor((l: ForceLink) => l.color)
    .linkWidth((l: ForceLink) => (l.below ? 0.4 : 1.2))
    .linkOpacity((l: ForceLink) => (l.below ? 0.18 : 0.6))
    .linkDirectionalArrowLength((l: ForceLink) => (l.edgeType === "competes_with" ? 0 : 3))
    .linkDirectionalArrowRelPos(0.92)
    .linkDirectionalArrowColor((l: ForceLink) => l.color)
    .showNavInfo(false);

  // Single click distinguished from double-click via a small delay.
  let pendingClick: { id: string; timer: number } | null = null;
  const DOUBLE = 280;
  instance.onNodeClick((node: ForceNode) => {
    const id = node.id;
    if (pendingClick) {
      window.clearTimeout(pendingClick.timer);
      pendingClick = null;
    }
    pendingClick = {
      id,
      timer: window.setTimeout(() => {
        pendingClick = null;
        cbs.onNodeClick?.(id);
      }, DOUBLE),
    };
  });
  // 3d-force-graph fires onNodeClick twice in quick succession for a
  // double-click; intercept the second one as the recenter signal.
  let lastClickAt = 0;
  instance.onNodeClick((node: ForceNode) => {
    const now = performance.now();
    if (now - lastClickAt < DOUBLE) {
      if (pendingClick) {
        window.clearTimeout(pendingClick.timer);
        pendingClick = null;
      }
      cbs.onNodeDoubleClick?.(node.id);
    }
    lastClickAt = now;
  });

  instance.onLinkClick((link: ForceLink) => {
    cbs.onEdgeClick?.(link.edgeId);
  });

  // Force-graph sizes itself off the container's bounding rect; make sure
  // it picks up changes when the topbar layout adjusts on first load.
  requestAnimationFrame(() => {
    if (instance && mountedContainer) {
      const rect = mountedContainer.getBoundingClientRect();
      instance.width(rect.width).height(rect.height);
    }
  });
}

export function update3D(g: EconGraph): void {
  if (!instance) return;
  instance.graphData(toForceData(g));
}

export function resize3D(width: number, height: number): void {
  if (!instance) return;
  instance.width(width).height(height);
}

export function stop3D(): void {
  if (!instance) return;
  try {
    // 3d-force-graph exposes _destructor() via three's renderer cleanup path.
    instance._destructor?.();
  } catch {
    /* best-effort cleanup; the WebGL context may already be gone */
  }
  if (mountedContainer) {
    mountedContainer.innerHTML = "";
  }
  instance = null;
  mountedContainer = null;
}

export function is3DRunning(): boolean {
  return instance !== null;
}
