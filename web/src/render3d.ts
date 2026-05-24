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
import * as THREE from "three";

import type { EconGraph } from "./graph";

export interface View3DCallbacks {
  onNodeClick?: (id: string) => void;
  onNodeDoubleClick?: (id: string) => void;
  onEdgeClick?: (edgeId: string) => void;
}

export interface View3DOptions {
  /** "globe" pins nodes at their Wikidata HQ lat/lon on a sphere; "ball"
      uses 3d-force-graph's default force-directed layout. */
  layout?: "ball" | "globe";
}

const GLOBE_RADIUS = 200;

/**
 * Project a Wikidata coord (lat, lon, degrees) onto a sphere of the given
 * radius. Standard latitude-longitude -> Cartesian mapping; lat=0 lon=0
 * lands on +X, north pole on +Y.
 */
function latLonToXYZ(lat: number, lon: number, radius = GLOBE_RADIUS) {
  const latR = (lat * Math.PI) / 180;
  const lonR = (lon * Math.PI) / 180;
  return {
    x: radius * Math.cos(latR) * Math.cos(lonR),
    y: radius * Math.sin(latR),
    z: radius * Math.cos(latR) * Math.sin(lonR),
  };
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
  // Globe-mode fixed positions (set when the node has Wikidata HQ coords).
  fx?: number;
  fy?: number;
  fz?: number;
}

interface ForceLink {
  source: string;
  target: string;
  edgeId: string;
  edgeType: string;
  color: string;
  below: boolean;
}

function toForceData(g: EconGraph, opts: View3DOptions = {}) {
  const layout = opts.layout ?? "ball";
  const nodes: ForceNode[] = [];
  const links: ForceLink[] = [];
  g.forEachNode((id, attrs) => {
    const apiNode = attrs.apiNode;
    const node: ForceNode = {
      id,
      label: attrs.label,
      color: attrs.color,
      // 3d-force-graph uses node "val" for sphere radius; map from the size
      // we already computed for Sigma (degree-based, 3..18).
      size: Math.max(2, attrs.size * 0.7),
      apiType: apiNode.attributes.type,
      provisional: apiNode.attributes.provisional,
    };
    if (layout === "globe") {
      // Wikidata enrichment writes metadata.wikidata = { lat, lon, ... }.
      // Honor it when present so the node lands at its real HQ position.
      const wd = (apiNode.attributes.metadata?.wikidata as
        | { lat?: number; lon?: number }
        | undefined);
      if (wd && typeof wd.lat === "number" && typeof wd.lon === "number") {
        const p = latLonToXYZ(wd.lat, wd.lon);
        node.fx = p.x; node.fy = p.y; node.fz = p.z;
      } else {
        // Random point on the sphere surface for nodes without coords
        // (regulators, slugs without geographic data). Pinned so they
        // don't drift around when force-layout runs.
        const u = Math.random(), v = Math.random();
        const theta = 2 * Math.PI * u;
        const phi = Math.acos(2 * v - 1);
        node.fx = GLOBE_RADIUS * Math.sin(phi) * Math.cos(theta);
        node.fy = GLOBE_RADIUS * Math.sin(phi) * Math.sin(theta);
        node.fz = GLOBE_RADIUS * Math.cos(phi);
      }
    }
    nodes.push(node);
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

// Globe-mode flag remembered across update3D calls so click-expand keeps
// new nodes pinned at their HQ coords too.
let currentLayout: "ball" | "globe" = "ball";

export function start3D(
  container: HTMLElement,
  g: EconGraph,
  cbs: View3DCallbacks = {},
  opts: View3DOptions = {},
): void {
  if (instance) stop3D();
  mountedContainer = container;
  currentLayout = opts.layout ?? "ball";

  instance = ForceGraph3D({ controlType: "orbit" })(container)
    .backgroundColor("#14171a")  // match --bg in styles.css
    .graphData(toForceData(g, opts))
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

  // Globe mode -- add a translucent wireframe sphere as a positional anchor.
  // Without it, nodes floating in 3D space look unmoored; with it, you see
  // the surface they're pinned to and can orient by "this side is North
  // America, that side is Europe."
  if (currentLayout === "globe") {
    const scene = instance.scene();
    // Wireframe globe -- thin lines, low opacity so node colors dominate.
    const sphere = new THREE.Mesh(
      new THREE.SphereGeometry(GLOBE_RADIUS - 4, 36, 24),
      new THREE.MeshBasicMaterial({
        color: 0x6ea8f0,
        wireframe: true,
        transparent: true,
        opacity: 0.18,
      }),
    );
    sphere.name = "econgraph-globe";
    scene.add(sphere);
    // Equator + prime-meridian arcs at slightly stronger weight for
    // orientation (so "north" reads at a glance).
    const ringGeom = new THREE.RingGeometry(GLOBE_RADIUS - 4, GLOBE_RADIUS - 3.5, 64);
    const ringMat = new THREE.MeshBasicMaterial({
      color: 0x9c978a,
      transparent: true,
      opacity: 0.35,
      side: THREE.DoubleSide,
    });
    const equator = new THREE.Mesh(ringGeom, ringMat);
    equator.name = "econgraph-equator";
    equator.rotation.x = Math.PI / 2;
    scene.add(equator);
    const prime = new THREE.Mesh(ringGeom, ringMat);
    prime.name = "econgraph-prime-meridian";
    scene.add(prime);
    // Step the camera back so the whole globe fits comfortably.
    instance.cameraPosition({ x: 0, y: 0, z: GLOBE_RADIUS * 3 });
  }
}

export function update3D(g: EconGraph): void {
  if (!instance) return;
  instance.graphData(toForceData(g, { layout: currentLayout }));
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
