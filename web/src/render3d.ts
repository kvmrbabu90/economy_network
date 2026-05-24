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
// world-atlas ships a 1:110m TopoJSON of world land masses (~15 KB). The
// land object's MultiPolygon geometry has continent + island outlines
// (no political borders) -- exactly what makes a sphere "read as Earth"
// without cluttering the canvas with country lines.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
import landTopo from "world-atlas/land-110m.json";
// eslint-disable-next-line @typescript-eslint/no-explicit-any
import { feature as topoFeature } from "topojson-client";

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
 * radius. Maps Greenwich (lat=0, lon=0) to +Z (camera-facing front-center),
 * eastward longitudes to +X (right of screen), westward longitudes to -X
 * (left of screen), and the North Pole to +Y. Three.js camera defaults to
 * +Z looking at the origin, so this orientation puts the prime meridian
 * dead-centre on the initial view and east on the right -- matching how
 * world maps are conventionally drawn.
 */
function latLonToXYZ(lat: number, lon: number, radius = GLOBE_RADIUS) {
  const latR = (lat * Math.PI) / 180;
  const lonR = (lon * Math.PI) / 180;
  return {
    x: radius * Math.cos(latR) * Math.sin(lonR),
    y: radius * Math.sin(latR),
    z: radius * Math.cos(latR) * Math.cos(lonR),
  };
}

// Continent-outline geometry, cached on first build because the projection
// is the same for every globe-mount in a session.
let cachedContinentGeometry: THREE.BufferGeometry | null = null;

function buildContinentLines(): THREE.BufferGeometry {
  if (cachedContinentGeometry) return cachedContinentGeometry;
  // topojson-client.feature() decodes the land topology. When the source
  // object is a GeometryCollection (which world-atlas/land-110m.json IS),
  // feature() returns a GeoJSON FeatureCollection — not a single Feature.
  // We must walk .features[*].geometry to find the Polygons / MultiPolygons.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const decoded: any = topoFeature(landTopo as any, (landTopo as any).objects.land);
  const positions: number[] = [];
  // Sit the line geometry slightly OUTSIDE the wireframe sphere so it
  // doesn't z-fight with the wireframe pattern.
  const r = GLOBE_RADIUS + 0.6;

  const flushRing = (ring: number[][]) => {
    // ring is an array of [lon, lat]. Convert to xyz triples; emit as
    // LINE_SEGMENTS (pairs) so we get one segment per consecutive vertex
    // pair, with no spurious closing line back to the start.
    let prev: { x: number; y: number; z: number } | null = null;
    for (const [lon, lat] of ring) {
      const p = latLonToXYZ(lat, lon, r);
      if (prev) {
        positions.push(prev.x, prev.y, prev.z, p.x, p.y, p.z);
      }
      prev = p;
    }
  };

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const handleGeometry = (geom: any) => {
    if (!geom) return;
    if (geom.type === "MultiPolygon") {
      for (const poly of geom.coordinates) {
        for (const ring of poly) flushRing(ring);
      }
    } else if (geom.type === "Polygon") {
      for (const ring of geom.coordinates) flushRing(ring);
    } else if (geom.type === "GeometryCollection") {
      for (const sub of geom.geometries ?? []) handleGeometry(sub);
    }
  };

  if (decoded?.type === "FeatureCollection") {
    for (const f of decoded.features ?? []) handleGeometry(f.geometry);
  } else if (decoded?.type === "Feature") {
    handleGeometry(decoded.geometry);
  } else {
    // Fall back to treating it like a bare geometry.
    handleGeometry(decoded);
  }

  const buf = new THREE.BufferGeometry();
  buf.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  cachedContinentGeometry = buf;
  return buf;
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
    // Solid inner sphere -- background-coloured, opaque. Sits just inside
    // the wireframe and acts as a depth-occluder so we don't see the
    // far-side continent outlines bleed through the wireframe (which made
    // the globe look east-west "inverted" because back-side lines are
    // mirrored from the viewer's perspective).
    const occluder = new THREE.Mesh(
      new THREE.SphereGeometry(GLOBE_RADIUS - 5, 48, 32),
      new THREE.MeshBasicMaterial({
        color: 0x14171a,           // matches backgroundColor("#14171a") below
        side: THREE.FrontSide,
      }),
    );
    occluder.name = "econgraph-globe-occluder";
    scene.add(occluder);
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

    // Continent outlines (the "looks like Earth" detail). Thin warm-grey
    // lines projected from world-atlas 110m land geometry. Sits just
    // outside the wireframe sphere to avoid z-fighting.
    const continents = new THREE.LineSegments(
      buildContinentLines(),
      new THREE.LineBasicMaterial({
        color: 0xe8e3da,           // matches --text in styles.css
        transparent: true,
        opacity: 0.55,
        linewidth: 1,
      }),
    );
    continents.name = "econgraph-continents";
    scene.add(continents);

    // Step the camera back so the whole globe fits comfortably, and aim
    // it over central North America (lat ~30N, lon ~-90W) so the bulk of
    // the S&P 500 HQs land on the visible hemisphere by default. Without
    // this the user would see Europe/Africa on first paint and assume
    // the globe is empty.
    const camFocus = latLonToXYZ(30, -90, GLOBE_RADIUS * 3);
    instance.cameraPosition({ x: camFocus.x, y: camFocus.y, z: camFocus.z });
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
