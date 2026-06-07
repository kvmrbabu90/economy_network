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
import { countryInMarkets, type FilterState } from "./ui/filters";
import type { ImpactState } from "./impact";
import { tintColor, tintColorRGB } from "./impact";

export interface View3DCallbacks {
  onNodeClick?: (id: string) => void;
  onNodeDoubleClick?: (id: string) => void;
  onEdgeClick?: (edgeId: string) => void;
}

export interface View3DOptions {
  /** "globe" pins nodes at their Wikidata HQ lat/lon on a sphere; "ball"
      uses 3d-force-graph's default force-directed layout. */
  layout?: "ball" | "globe";
  /** Current filter state — used to hide Company nodes that don't match the
      selected market chips, and to hide provisional nodes when the toggle is
      off. Mirrors the same logic as the Sigma 2D nodeReducer. */
  filterState?: FilterState | null;
}

const GLOBE_RADIUS = 200;

// Country -> approximate geographic center, used as a fallback HQ
// position for companies that have a `country` attribute but never
// got Wikidata HQ coords (about 41 of the 500 S&P filers fall through
// the SPARQL net). The MVP corpus is 100% US, so only "US" matters
// today -- add countries here as the corpus expands.
const COUNTRY_CENTROID: Record<string, { lat: number; lon: number }> = {
  // North America
  US: { lat: 39.83, lon: -98.58 },
  CA: { lat: 56.13, lon: -106.35 },
  MX: { lat: 23.63, lon: -102.55 },
  // Europe
  GB: { lat: 55.38, lon:   -3.44 },
  DE: { lat: 51.17, lon:   10.45 },
  FR: { lat: 46.23, lon:    2.21 },
  NL: { lat: 52.13, lon:    5.29 },
  IT: { lat: 41.87, lon:   12.57 },
  ES: { lat: 40.46, lon:   -3.75 },
  SE: { lat: 60.13, lon:   18.64 },
  DK: { lat: 56.26, lon:    9.50 },
  FI: { lat: 61.92, lon:   25.75 },
  AT: { lat: 47.52, lon:   14.55 },
  PT: { lat: 39.40, lon:   -8.22 },
  IE: { lat: 53.41, lon:   -8.24 },
  BE: { lat: 50.50, lon:    4.47 },
  NO: { lat: 60.47, lon:    8.47 },
  CH: { lat: 46.82, lon:    8.23 },
  PL: { lat: 51.92, lon:   19.15 },
  // Asia
  JP: { lat: 36.20, lon:  138.25 },
  CN: { lat: 35.86, lon:  104.20 },
  KR: { lat: 35.91, lon:  127.77 },
  TW: { lat: 23.70, lon:  121.00 },
  IN: { lat: 20.59, lon:   78.96 },
  SG: { lat:  1.35, lon:  103.82 },
  MY: { lat:  4.21, lon:  108.46 },
  TH: { lat: 15.87, lon:  100.99 },
  ID: { lat: -0.79, lon:  113.92 },
  // Oceania
  AU: { lat: -25.27, lon: 133.78 },
  NZ: { lat: -40.90, lon: 174.89 },
  // Middle East
  SA: { lat: 23.89, lon:  45.08 },
  AE: { lat: 23.42, lon:  53.85 },
  // Africa
  EG: { lat: 26.82, lon:  30.80 },
  ZA: { lat: -30.56, lon:  22.94 },
  NG: { lat:  9.08, lon:   8.68 },
  MA: { lat: 31.79, lon:  -7.09 },
  // Latin America
  BR: { lat: -14.24, lon: -51.93 },
  AR: { lat: -38.42, lon: -63.62 },
  CL: { lat: -35.68, lon: -71.54 },
  CO: { lat:   4.57, lon: -74.30 },
  PE: { lat:  -9.19, lon: -75.02 },
  VE: { lat:   6.42, lon: -66.59 },
  EC: { lat:  -1.83, lon: -78.18 },
  BO: { lat: -16.29, lon: -63.59 },
  PY: { lat: -23.44, lon: -58.44 },
  UY: { lat: -32.52, lon: -55.77 },
};

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
// Cleanup callbacks registered on start3D; called by stop3D.
let _cleanup3D: (() => void) | null = null;

/**
 * Current impact overlay state — read by the nodeColor / nodeVisibility /
 * nodeLabel closures registered in start3D(). Persists across stop3D/start3D
 * cycles so switching 2D→3D while an impact is active immediately renders
 * tint colors without a separate applyImpact3D() call.
 */
let _currentImpactState: ImpactState | null = null;

/**
 * Reverse the premultiplication applied by style.ts/toRgba so the 3D renderer
 * (three.js, standard blending) gets the actual base color rather than the
 * already-scaled-down premultiplied RGB. Without this, an edge stored as
 * rgba(67,69,73,0.55) — premultiplied from #7a7e84 — renders in THREE as
 * rgb(67,69,73) which is dark consumer-market grey, not commodity grey.
 */
function unpremultiply(color: string): string {
  const m = color.match(/rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)/);
  if (!m) return color;
  const a = parseFloat(m[4]);
  if (a <= 0) return color;
  const r = Math.min(255, Math.round(parseInt(m[1]) / a));
  const g = Math.min(255, Math.round(parseInt(m[2]) / a));
  const b = Math.min(255, Math.round(parseInt(m[3]) / a));
  return `rgb(${r},${g},${b})`;
}

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
  const fs = opts.filterState ?? null;
  const nodes: ForceNode[] = [];
  const links: ForceLink[] = [];
  // For globe mode: track each node's intended (lat, lon) separately so we
  // can detect coincident pins (e.g. every US federal regulator landing on
  // the same DC coordinate) and spread them into a small ring before
  // projecting to xyz. Without this pass the spheres stack on a single
  // pixel and become un-pickable.
  const globePos: Array<{ idx: number; lat: number; lon: number }> = [];
  // Track which node IDs are hidden so we can drop their edges too.
  const hiddenIds = new Set<string>();

  g.forEachNode((id, attrs) => {
    // Skip bubble-layout sector hubs — they only exist for the 2D "Bubbles"
    // mode and have no meaningful position or edges in 3D space.
    if (id.startsWith("bubble:")) return;
    const apiNode = attrs.apiNode;
    // --- Market filter (mirrors Sigma 2D nodeReducer) ---
    // Only Company nodes are subject to market filtering.
    // Commodity, Regulator, Region nodes are always visible.
    if (fs) {
      const nodeType    = apiNode.attributes.type as string;
      const nodeCountry = apiNode.attributes.country as string | undefined;
      const isProv      = !!apiNode.attributes.provisional;
      if (isProv && !fs.includeProvisional) { hiddenIds.add(id); return; }
      if (
        nodeType === "Company" &&
        nodeCountry &&
        fs.markets !== null &&
        fs.markets.length > 0 &&
        !countryInMarkets(nodeCountry, fs.markets)
      ) { hiddenIds.add(id); return; }
    }
    const node: ForceNode = {
      id,
      label: attrs.label,
      color: attrs.color,
      // 3d-force-graph uses node "val" for sphere volume (radius ∝ val^⅓).
      // Scale up from the Sigma base sizes so spheres are comfortably
      // clickable on the globe (Company=4→20, Regulator=10→50, etc.).
      size: Math.max(10, attrs.size * 5),
      apiType: apiNode.attributes.type,
      provisional: !!apiNode.attributes.provisional,
    };
    if (layout === "globe") {
      // Wikidata enrichment writes metadata.wikidata = { lat, lon, ... }.
      // Honor it when present so the node lands at its real HQ position.
      const wd = (apiNode.attributes.metadata?.wikidata as
        | { lat?: number; lon?: number }
        | undefined);
      let lat: number;
      let lon: number;
      if (wd && typeof wd.lat === "number" && typeof wd.lon === "number" &&
          isFinite(wd.lat) && isFinite(wd.lon)) {
        // Defensive: a small number of Wikidata records come back with
        // lat/lon swapped (the field stores longitude where it should
        // store latitude). Detect the swap by checking that lat is in
        // valid range; if not but lon is, flip them.
        let { lat: la, lon: lo } = wd as { lat: number; lon: number };
        const latOK = la >= -90 && la <= 90;
        const lonOK = lo >= -180 && lo <= 180;
        if (!latOK && lonOK) {
          const tmp = la; la = lo; lo = tmp;
        }
        // Clamp to valid ranges after potential swap.
        lat = Math.max(-90, Math.min(90, la));
        lon = Math.max(-180, Math.min(180, lo));
      } else if (apiNode.attributes.type === "Regulator") {
        // All but one of our regulators are US federal -- pin at
        // Washington DC. NAIC is the only multistate body; nudge it to
        // Kansas City where the NAIC central office actually sits.
        const isNAIC = apiNode.key === "regulator:naic";
        if (isNAIC) { lat = 39.0997; lon = -94.5786; }
        else        { lat = 38.8951; lon = -77.0364; }
      } else {
        // No HQ coords. Deterministic-by-id so the placement doesn't
        // reshuffle every paint.
        let hash = 0;
        for (let i = 0; i < apiNode.key.length; i++) {
          hash = (hash * 31 + apiNode.key.charCodeAt(i)) | 0;
        }
        const country = apiNode.attributes.country as string | undefined;
        const centroid = country ? COUNTRY_CENTROID[country] : null;
        if (centroid) {
          // Jitter within ~2° of the country's geographic center so
          // multiple coord-less companies in the same country don't
          // all stack on one pixel.
          const jLat = (((hash >> 4) & 0xff) / 255 - 0.5) * 4;
          const jLon = (((hash >> 12) & 0xff) / 255 - 0.5) * 4;
          lat = centroid.lat + jLat;
          lon = centroid.lon + jLon;
        } else {
          // Truly unknown -- stash south of Antarctica so the node
          // doesn't pretend to sit on a real continent.
          lon = (hash % 360) - 180;
          lat = -82 - ((hash >> 8) & 7);  // -82..-89
        }
      }
      globePos.push({ idx: nodes.length, lat, lon });
    }
    nodes.push(node);
  });

  if (layout === "globe") {
    // Detect coincident pins -- bucket by 0.01° (~1 km). Any bucket with
    // 2+ members gets spread into an evenly-spaced ring centered on the
    // original point. Ring radius scales with group size so large
    // clusters (e.g. ~10 US regulators in DC) get more breathing room.
    const buckets = new Map<string, number[]>();
    for (let i = 0; i < globePos.length; i++) {
      const gp = globePos[i];
      const key =
        Math.round(gp.lat * 100).toString() +
        "_" +
        Math.round(gp.lon * 100).toString();
      const arr = buckets.get(key) ?? [];
      arr.push(i);
      buckets.set(key, arr);
    }
    for (const idxs of buckets.values()) {
      if (idxs.length < 2) continue;
      // ~0.2° base + 0.02° per extra node -- keeps pins visually distinct
      // (~20-45 km radius) without scattering them across the region.
      const ringDeg = Math.min(0.6, 0.2 + 0.02 * idxs.length);
      for (let k = 0; k < idxs.length; k++) {
        const theta = (2 * Math.PI * k) / idxs.length;
        const gp = globePos[idxs[k]];
        // Compensate longitude for latitude convergence so the ring stays
        // visually circular instead of becoming an east-west oval near
        // the poles. Clamp cosLat away from 0 so polar nodes don't blow
        // up.
        const cosLat = Math.max(0.15, Math.cos((gp.lat * Math.PI) / 180));
        gp.lat += ringDeg * Math.cos(theta);
        gp.lon += (ringDeg * Math.sin(theta)) / cosLat;
      }
    }
    // Project every (lat, lon) to xyz and pin via fx/fy/fz.
    for (const gp of globePos) {
      const p = latLonToXYZ(gp.lat, gp.lon);
      const n = nodes[gp.idx];
      n.fx = p.x; n.fy = p.y; n.fz = p.z;
    }
  }
  g.forEachEdge((id, attrs, src, tgt) => {
    // Skip any edge that touches a bubble hub node.
    if (src.startsWith("bubble:") || tgt.startsWith("bubble:") || id.startsWith("bubble-edge:")) return;
    // Skip edges where either endpoint was filtered out by the market filter.
    if (hiddenIds.has(src) || hiddenIds.has(tgt)) return;
    // Guard against edges added with missing apiEdge (e.g. bubble aggregated edges).
    if (!attrs.apiEdge) return;
    links.push({
      source: src,
      target: tgt,
      edgeId: id,
      edgeType: attrs.edgeType,
      // Un-premultiply: style.ts stores premultiplied rgba for Sigma's
      // gl.ONE blending. three.js uses standard blending and needs the
      // original base color so linkOpacity() handles transparency itself.
      color: unpremultiply(attrs.color),
      below: attrs.apiEdge.attributes.below_threshold,
    });
  });
  return { nodes, links };
}

// Globe-mode flag remembered across update3D calls so click-expand keeps
// new nodes pinned at their HQ coords too.
let currentLayout: "ball" | "globe" = "ball";

// One-shot flag: set to true after the globe camera has been positioned for
// the first time following a real (non-empty) data load. Without this guard
// the 3d-force-graph library's internal zoomToFit() — fired via onEngineStop
// after graphData() settles — overrides the cameraPosition() set at the end
// of start3D(), zooming the camera into the dense North American cluster
// instead of framing the full globe.
let _globeCameraInitialized = false;

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
    .nodeLabel((n: ForceNode) => {
      if (_currentImpactState) {
        const tc = tintColor(_currentImpactState.byNode.get(n.id));
        return tc ? `<span style="color:#e8e3da">${n.label}</span>` : "";
      }
      return `<span style="color:#e8e3da">${n.label}</span>`;
    })
    .nodeColor((n: ForceNode) => {
      // When an impact is active, return the tint colour for impacted nodes
      // and the background colour for everything else. Each unique colour
      // string gets its OWN MeshLambertMaterial in three-forcegraph's
      // sphereMaterials cache, so nodes with different tints never share a
      // material — fixing the bug where direct material mutation caused the
      // last-applied tint to overwrite all earlier ones.
      if (_currentImpactState) {
        return tintColor(_currentImpactState.byNode.get(n.id)) ?? "#14171a";
      }
      return n.color;
    })
    .nodeVisibility((n: ForceNode) => {
      // Hide non-impacted nodes during an impact trace (removes their meshes
      // entirely, which is cleaner than setting obj.visible=false and avoids
      // the raycaster hitting invisible geometry).
      if (_currentImpactState) {
        return tintColor(_currentImpactState.byNode.get(n.id)) !== null;
      }
      return true;
    })
    .nodeVal((n: ForceNode) => n.size)
    .nodeRelSize(2)          // default is 4; 2 → 50% visual radius at launch
    .nodeResolution(12)
    .nodeOpacity(0.95)
    .linkColor((l: ForceLink) => l.color)
    .linkWidth((l: ForceLink) => (l.below ? 0.4 : 1.2))
    .linkOpacity((l: ForceLink) => (l.below ? 0.18 : 0.5))
    // Arrows disabled (monochrome theme, per user request).
    .linkDirectionalArrowLength(0)
    .showNavInfo(false);

  // Globe mode: replace 3d-force-graph's straight tube links with
  // great-circle-style arcs that bulge outward from the sphere center.
  // Chord-lines cutting through the globe interior obscure the surface
  // and the rest of the network; arcing them above the wireframe makes
  // long-haul edges legible.
  //
  // Why we don't use linkPositionUpdate: 3d-force-graph only calls it
  // when the force simulation moves a node. All globe nodes are pinned
  // (fx/fy/fz set), so the simulation never ticks and the accessor
  // never fires -- leaving the BufferGeometry empty and the link
  // invisible. Instead we register an empty Line via linkThreeObject
  // and then walk the scene once on the next animation frame, by
  // which point 3d-force-graph has resolved each link's .source /
  // .target string ids into real node objects with x/y/z positions.
  if (currentLayout === "globe") {
    // Arc heights tuned so even antipodal links stay close to the
    // surface -- previous max of 0.65*R made arcs visually larger than
    // the globe itself when zoomed out. 0.06 + 0.18 by angular sep
    // keeps every arc inside a thin shell hugging the wireframe.
    const ARC_BASE_HEIGHT = GLOBE_RADIUS * 0.03;
    const ARC_MAX_HEIGHT = GLOBE_RADIUS * 0.22;
    const ARC_SEGMENTS = 24;
    // Below this angular separation (in radians) two endpoints are
    // effectively co-located -- typically because they both fell back
    // to the same country centroid. Bezier through co-located points
    // builds a degenerate curve and pushes the apex in an arbitrary
    // direction; skip and draw a tiny straight segment instead.
    const MIN_ANGLE = 0.005;
    instance
      .linkThreeObjectExtend(false)
      .linkThreeObject((link: ForceLink) => {
        // Real 3D tubes (not 1px THREE.Line) so they stay visible at any
        // zoom and depth-test properly against node spheres. Geometry is
        // built lazily in buildArcs() once node positions resolve.
        const mat = new THREE.MeshBasicMaterial({
          color: link.color,
          transparent: true,
          opacity: link.below ? 0.30 : 0.75,
        });
        return new THREE.Mesh(new THREE.BufferGeometry(), mat);
      })
      // CRITICAL: suppress 3d-force-graph's default link-position update.
      // By default the library positions each link mesh at the midpoint
      // between source and target (applying a non-identity transform).
      // Our buildArcs() builds TubeGeometry with WORLD-SPACE coordinates,
      // so the mesh must stay at world-origin with an identity transform;
      // otherwise the tube vertices are offset twice (once by the mesh
      // position, once by the geometry coordinates) and the arcs shoot far
      // beyond the globe. Returning true tells the library "I'm handling it."
      .linkPositionUpdate((_obj: unknown) => true);

    const TUBE_RADIUS_CORE = 0.6;
    const TUBE_RADIUS_AUDIT = 0.3;
    const TUBE_RADIAL_SEGMENTS = 6;
    const buildArcs = () => {
      if (!instance) return;
      const scene = instance.scene();
      let built = 0, skipped = 0;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      scene.traverse((obj: any) => {
        if (obj.__graphObjType !== "link" || !obj.isMesh) return;
        // Skip links whose arc geometry was already built on a previous pass.
        // TubeGeometry has type === "TubeGeometry"; placeholder BufferGeometry does not.
        if (obj.geometry && obj.geometry.type === "TubeGeometry") return;
        const link = obj.__data;
        const src = link && link.source;
        const tgt = link && link.target;
        if (!src || !tgt) { skipped++; return; }
        // Prefer pinned positions (fx/fy/fz) over simulation positions (x/y/z).
        // In globe mode all nodes have fx/fy/fz set. d3-force-3d initialises
        // node.x/y/z to a random value and only overwrites it with fx/fy/fz on
        // the first simulation tick, so early buildArcs passes (rAF / 100 ms)
        // would read wrong positions if we used x/y/z. fx/fy/fz are correct
        // from the moment the data object is created.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const _src = src as any, _tgt = tgt as any;
        const sx: number = typeof _src.fx === "number" ? _src.fx : _src.x;
        const sy: number = typeof _src.fy === "number" ? _src.fy : _src.y;
        const sz: number = typeof _src.fz === "number" ? _src.fz : _src.z;
        const ex: number = typeof _tgt.fx === "number" ? _tgt.fx : _tgt.x;
        const ey: number = typeof _tgt.fy === "number" ? _tgt.fy : _tgt.y;
        const ez: number = typeof _tgt.fz === "number" ? _tgt.fz : _tgt.z;
        if (!isFinite(sx) || !isFinite(sy) || !isFinite(sz) ||
            !isFinite(ex) || !isFinite(ey) || !isFinite(ez)) { skipped++; return; }
        const s = new THREE.Vector3(sx, sy, sz);
        const e = new THREE.Vector3(ex, ey, ez);
        const angle = Math.min(Math.PI, s.angleTo(e) || 0);
        let points: THREE.Vector3[];
        if (angle < MIN_ANGLE) {
          // Degenerate endpoints -- one short stub. Two-point curves
          // make pathological TubeGeometry; nudge endpoint a hair so
          // the tube has length.
          const nudged = e.clone();
          if (s.distanceTo(e) < 1e-3) nudged.x += 0.5;
          points = [s, nudged];
        } else {
          // Great-circle slerp + sine bump. See aa5de4f for derivation.
          const rS = s.length();
          const rE = e.length();
          const sNorm = s.clone().multiplyScalar(1 / rS);
          const eNorm = e.clone().multiplyScalar(1 / rE);
          const sinA = Math.sin(angle);
          const heightPct =
            ARC_BASE_HEIGHT + (ARC_MAX_HEIGHT - ARC_BASE_HEIGHT) * (angle / Math.PI);
          points = new Array(ARC_SEGMENTS + 1);
          for (let i = 0; i <= ARC_SEGMENTS; i++) {
            const t = i / ARC_SEGMENTS;
            const a = Math.sin((1 - t) * angle) / sinA;
            const b = Math.sin(t * angle) / sinA;
            const dir = sNorm.clone().multiplyScalar(a).add(
              eNorm.clone().multiplyScalar(b),
            );
            const baseR = rS * (1 - t) + rE * t;
            const bump = Math.sin(Math.PI * t) * heightPct;
            points[i] = dir.multiplyScalar(baseR + bump);
          }
        }
        // Replace the placeholder BufferGeometry with a TubeGeometry
        // along a CatmullRom curve through the slerp samples. CatmullRom
        // smooths the polyline. Tube radius is small (0.3 - 0.6 units
        // against a 200-unit globe) -- thick enough to read at any
        // zoom, thin enough not to choke the GPU at 6.5k tubes.
        const curve = new THREE.CatmullRomCurve3(points);
        const tubeGeom = new THREE.TubeGeometry(
          curve,
          ARC_SEGMENTS,
          link.below ? TUBE_RADIUS_AUDIT : TUBE_RADIUS_CORE,
          TUBE_RADIAL_SEGMENTS,
          false,
        );
        if (obj.geometry) obj.geometry.dispose();
        obj.geometry = tubeGeom;
        obj.frustumCulled = false;
        built++;
      });
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).__arcBuilt = { built, skipped };
    };
    // Only schedule initial arc builds if an impact trace is already active
    // (e.g. the user switched 2D→3D while a trace was running). In idle mode
    // all arcs are hidden — building 13k TubeGeometry objects at mount time is
    // pure overhead and the dominant source of the initial globe lag.
    // Arc builds happen the first time applyImpact3D() fires via update3D().
    if (_currentImpactState) {
      requestAnimationFrame(buildArcs);
      setTimeout(buildArcs, 100);
      setTimeout(buildArcs, 600);
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__rebuildArcs = buildArcs;
  }

  // Zoom-aware node + link scaling. Piggybacks on the user's mouse-wheel
  // input -- OrbitControls handles the camera zoom natively, and in the
  // same handler we walk the scene and apply a matching scale to every
  // node sphere and link tube. The effect: as you zoom into a dense
  // cluster, the spheres and edges shrink in proportion so the internal
  // structure resolves instead of becoming a single overlapping blob.
  //
  // Why a wheel listener rather than camera-distance polling: tracking
  // camera distance per frame was unreliable (HMR + force-sim interaction
  // hung the renderer in earlier attempts). User-input-driven is simpler
  // and survives anything the simulation does to the camera.
  const SCALE_MIN = 0.15;
  const SCALE_MAX = 4.0;
  const clampScale = (s: number) => Math.min(SCALE_MAX, Math.max(SCALE_MIN, s));
  // Cache the reference camera distance + the controls' default rotate
  // speed so we can taper rotation/pan as the user zooms in. OrbitControls
  // moves in angular units; when the camera is close to the target, a
  // small angular delta translates to a large screen-space jump, which
  // reads as "drag is way too sensitive."
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const cameraRef = instance.camera?.();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const ctrlRef: any = instance.controls?.();
  const refCamDist = cameraRef?.position?.length?.() || 1;
  const defaultRotateSpeed = ctrlRef?.rotateSpeed ?? 1.0;
  const defaultPanSpeed = ctrlRef?.panSpeed ?? 1.0;
  const tuneControls = () => {
    if (!cameraRef || !ctrlRef) return;
    const d = cameraRef.position.length();
    // ratio in [0, 1]; clamp to a floor so drags still respond at deep zoom.
    const ratio = Math.max(0.08, Math.min(1.0, d / refCamDist));
    ctrlRef.rotateSpeed = defaultRotateSpeed * ratio;
    ctrlRef.panSpeed = defaultPanSpeed * ratio;
  };
  const onWheel = (ev: WheelEvent) => {
    if (!instance) return;
    // deltaY < 0 = scroll up = zoom in. We shrink so dense clusters
    // resolve into distinct nodes instead of an overlapping blob.
    const factor = ev.deltaY < 0 ? 1 / 1.12 : 1.12;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    instance.scene().traverse((obj: any) => {
      if (obj.__graphObjType === "node" && obj.scale) {
        const next = clampScale(obj.scale.x * factor);
        obj.scale.set(next, next, next);
      } else if (obj.__graphObjType === "link" && obj.scale && currentLayout !== "globe") {
        // Scale X/Y only (shrinks tube radius) -- valid in ball mode because
        // 3d-force-graph owns the tube geometry in local space. In globe mode
        // the TubeGeometry is built in WORLD space with the mesh at origin, so
        // scaling X/Y distorts the arc path geometry. Globe arcs must not be
        // scaled; their visual weight is fixed by the TUBE_RADIUS constants.
        const nx = clampScale(obj.scale.x * factor);
        const ny = clampScale(obj.scale.y * factor);
        obj.scale.x = nx;
        obj.scale.y = ny;
      }
    });
    // OrbitControls fires its zoom logic on the same wheel event; by the
    // time the browser dispatches it, the camera has already moved to its
    // new position, so reading position.length() here gives the post-zoom
    // distance. Re-tune drag sensitivity accordingly.
    tuneControls();
  };
  container.addEventListener("wheel", onWheel, { passive: true });
  tuneControls();

  // Keyboard shortcuts as a fallback / fine control:
  // '[' shrink, ']' grow, '0' reset.
  const sizeKeydown = (ev: KeyboardEvent) => {
    if (!instance) return;
    const focused = document.activeElement as HTMLElement | null;
    if (focused && (focused.tagName === "INPUT" || focused.tagName === "TEXTAREA")) return;
    let factor = 0;
    if (ev.key === "[") factor = 1 / 1.25;
    else if (ev.key === "]") factor = 1.25;
    else if (ev.key === "0") factor = -1;
    else return;
    ev.preventDefault();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    instance.scene().traverse((obj: any) => {
      if (obj.__graphObjType === "node" && obj.scale) {
        if (factor < 0) obj.scale.set(1, 1, 1);
        else obj.scale.multiplyScalar(factor);
      } else if (obj.__graphObjType === "link" && obj.scale && currentLayout !== "globe") {
        // Same globe-mode guard as onWheel: arc geometry lives in world space
        // so scaling the mesh distorts it. Only scale links in ball mode.
        if (factor < 0) {
          obj.scale.set(1, 1, 1);
          if (obj.material) obj.material.opacity = obj.userData?.__origOpacity ?? obj.material.opacity;
        } else {
          obj.scale.x *= factor;
          obj.scale.y *= factor;
        }
      }
    });
  };
  document.addEventListener("keydown", sizeKeydown);

  // Register cleanup so stop3D() removes both listeners and cancels any
  // pending single-click timer (prevents firing on a torn-down instance).
  _cleanup3D = () => {
    container.removeEventListener("wheel", onWheel);
    document.removeEventListener("keydown", sizeKeydown);
    if (pendingClick) {
      window.clearTimeout(pendingClick.timer);
      pendingClick = null;
    }
  };

  // Expose for live debugging / preview-tool inspection.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).__force3D = instance;

  // Single click distinguished from double-click via a small delay.
  // 3d-force-graph fires onNodeClick twice in quick succession for a
  // double-click. A single handler uses both a pending-timer approach and
  // a lastClickAt timestamp to distinguish the two gestures correctly.
  let pendingClick: { id: string; timer: number } | null = null;
  let lastClickAt = 0;
  const DOUBLE = 280;
  instance.onNodeClick((node: ForceNode) => {
    const id = node.id;
    const now = performance.now();
    // If two clicks arrive within DOUBLE ms, treat as double-click.
    if (now - lastClickAt < DOUBLE) {
      if (pendingClick) {
        window.clearTimeout(pendingClick.timer);
        pendingClick = null;
      }
      lastClickAt = 0; // reset so a third click isn't also treated as double
      cbs.onNodeDoubleClick?.(id);
      return;
    }
    lastClickAt = now;
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
    // orientation (so "north" reads at a glance). Use separate geometry +
    // material instances so the scene-traverse dispose loop doesn't attempt
    // to free the same GPU resource twice (Three.js is tolerant but it's
    // still incorrect ownership semantics).
    const ringGeomEq = new THREE.RingGeometry(GLOBE_RADIUS - 4, GLOBE_RADIUS - 3.5, 64);
    const ringMatEq = new THREE.MeshBasicMaterial({
      color: 0x9c978a,
      transparent: true,
      opacity: 0.35,
      side: THREE.DoubleSide,
    });
    const equator = new THREE.Mesh(ringGeomEq, ringMatEq);
    equator.name = "econgraph-equator";
    equator.rotation.x = Math.PI / 2;
    scene.add(equator);
    const ringGeomPm = new THREE.RingGeometry(GLOBE_RADIUS - 4, GLOBE_RADIUS - 3.5, 64);
    const ringMatPm = new THREE.MeshBasicMaterial({
      color: 0x9c978a,
      transparent: true,
      opacity: 0.35,
      side: THREE.DoubleSide,
    });
    const prime = new THREE.Mesh(ringGeomPm, ringMatPm);
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
    //
    // Two-stage approach to win against 3d-force-graph's internal zoomToFit():
    //   1. Set position immediately (covers first paint before any data loads).
    //   2. Re-assert via onEngineStop after the first real data load settles.
    //      The library fires onEngineStop → zoomToFit → overrides cameraPosition,
    //      so we re-assert once and then stop listening (_globeCameraInitialized).
    _globeCameraInitialized = false;
    const _assertGlobeCamera = () => {
      if (!instance || currentLayout !== "globe") return;
      // 3d-force-graph's underlying renderer sets scene.visible = false at
      // init when waitForLoadComplete=true (its default). It normally becomes
      // visible again when backgroundImageUrl changes, but we never set that
      // prop so the scene stays hidden on every re-entry into Globe mode.
      // We force it visible here because _assertGlobeCamera is only called
      // after the engine has confirmed data is loaded (onEngineStop guard).
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const s = instance.scene?.() as any;
      if (s) s.visible = true;
      const cf = latLonToXYZ(30, -90, GLOBE_RADIUS * 3);
      // Set camera position. The 2nd-argument lookAt is ignored by the
      // library in globe mode (it defers to OrbitControls), so after
      // positioning we orient the Three.js camera toward origin directly.
      instance.cameraPosition?.({ x: cf.x, y: cf.y, z: cf.z });
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (instance.camera?.() as any)?.lookAt(0, 0, 0);
    };
    instance.onEngineStop?.(() => {
      if (_globeCameraInitialized || currentLayout !== "globe") return;
      // Skip the empty-graph stop fired during initialization (g.order === 0
      // means start3D was called before loadFullCore pushed real nodes in).
      const fd = instance.graphData?.();
      if (!fd || (fd as { nodes?: unknown[] }).nodes?.length === 0) return;
      _globeCameraInitialized = true;
      _assertGlobeCamera();
    });
    _assertGlobeCamera(); // immediate fallback for first frame
  }
}

export function update3D(g: EconGraph, filterState?: FilterState | null): void {
  if (!instance) return;
  // In globe mode, dispose any custom TubeGeometry arc objects before
  // replacing graphData. 3d-force-graph handles teardown of its own internal
  // primitives but our linkThreeObject meshes are external — leaking them
  // causes GPU memory to accumulate on every update3D() call.
  if (currentLayout === "globe") {
    try {
      const scene = instance.scene?.();
      if (scene) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        scene.traverse((obj: any) => {
          if (obj.__graphObjType === "link" && obj.isMesh) {
            // Dispose TubeGeometry arcs created by linkThreeObject.
            if (obj.geometry && obj.geometry.type === "TubeGeometry") {
              try { obj.geometry.dispose(); } catch { /* ignore */ }
            }
            // Also dispose the material; 3d-force-graph replaces the entire
            // link object on graphData() refresh so these materials are orphaned.
            if (obj.material) {
              try { obj.material.dispose?.(); } catch { /* ignore */ }
            }
          }
        });
      }
    } catch { /* best-effort */ }
  }
  instance.graphData(toForceData(g, { layout: currentLayout, filterState }));
  // In globe mode, re-build arc geometry after the data update since
  // graphData() replaces link objects. Three passes mirror start3D's schedule:
  // rAF catches the common case where positions resolve in one tick;
  // 100 ms covers slower resolution; 600 ms is a safety net for the rare
  // case where the d3 simulation hasn't fully settled link.source / .target
  // references yet when update3D is triggered mid-session by replaceGraph.
  // Only rebuild arcs when an impact trace is active. In idle mode every arc
  // ends up hidden (_applyLinkTint(null) sets obj.visible = false), so building
  // 13k TubeGeometry objects on every update3D() call is pure GPU overhead.
  // Arc builds are deferred to the first applyImpact3D() call: that function
  // sets _currentImpactState BEFORE calling update3D, so by the time execution
  // reaches here the flag is already non-null and arc building proceeds.
  if (currentLayout === "globe" && (window as any).__rebuildArcs && _currentImpactState) {
    requestAnimationFrame((window as any).__rebuildArcs);
    setTimeout((window as any).__rebuildArcs, 100);
    setTimeout((window as any).__rebuildArcs, 600);
  }
  // graphData() replaces every link object with a fresh THREE primitive that
  // has never been tinted. If an impact trace is active, re-apply the link
  // tint after the new objects settle — otherwise all edges appear visible
  // at their default colors until the next explicit applyImpact3D() call.
  // Timing: arcs are rebuilt at rAF / 100 ms / 600 ms; tint runs AFTER
  // each arc-build pass (150 ms / 350 ms / 750 ms) so tubes exist first.
  if (_currentImpactState) {
    const snap = _currentImpactState;
    setTimeout(() => _applyLinkTint(snap), 150);
    setTimeout(() => _applyLinkTint(snap), 350);
    setTimeout(() => _applyLinkTint(snap), 750);
  }
}

export function resize3D(width: number, height: number): void {
  if (!instance) return;
  instance.width(width).height(height);
}

export function stop3D(): void {
  if (!instance) return;
  // Remove event listeners registered during start3D.
  if (_cleanup3D) { _cleanup3D(); _cleanup3D = null; }
  try {
    // Dispose cached continent geometry to free GPU buffer memory.
    if (cachedContinentGeometry) {
      cachedContinentGeometry.dispose();
      cachedContinentGeometry = null;
    }
    // Walk the scene and dispose all geometries + materials before teardown
    // so three.js doesn't leak GPU memory across view-mode switches.
    try {
      const scene = instance.scene?.();
      if (scene) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        scene.traverse((obj: any) => {
          if (obj.geometry) {
            try { obj.geometry.dispose(); } catch { /* ignore */ }
          }
          if (obj.material) {
            if (Array.isArray(obj.material)) {
              obj.material.forEach((m: { dispose?: () => void }) => { try { m.dispose?.(); } catch { /* ignore */ } });
            } else {
              try { obj.material.dispose?.(); } catch { /* ignore */ }
            }
          }
        });
      }
    } catch { /* best-effort */ }
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
  // Reset camera init flag so the next start3D() cycle gets fresh positioning.
  _globeCameraInitialized = false;
}

export function is3DRunning(): boolean {
  return instance !== null;
}

// ---------------------------------------------------------------------------
// Impact overlay — kapsule-aware tinting
// ---------------------------------------------------------------------------

/**
 * Apply (or clear) an impact overlay on the running 3D scene.
 *
 * **Why not `scene.traverse` + direct material mutation?**
 * three-forcegraph caches materials in `sphereMaterials[colorString]`. All
 * Company nodes share one `MeshLambertMaterial` instance (they all start
 * life as `#c8ccd2`). Calling `obj.material.color.setRGB(...)` modifies
 * that shared material, so the last node processed overwrites every earlier
 * tint — producing a monochromatic scene.
 *
 * **The fix:** set `_currentImpactState` before triggering a kapsule
 * re-digest via `update3D()`. The `nodeColor` closure in `start3D()` returns
 * the tint colour string for impacted nodes. Since `sphereMaterials` is keyed
 * by colour string, each distinct tint gets its OWN material — no sharing,
 * no overwriting.
 *
 * Link tinting still uses `scene.traverse` because link objects are per-link
 * (no shared-material problem there).
 */
export function applyImpact3D(
  g: EconGraph,
  state: ImpactState | null,
  filterState?: FilterState | null,
): void {
  _currentImpactState = state;
  if (!instance) return;
  // update3D() handles globe-arc disposal + graphData() re-trigger + arc rebuild.
  // The nodeColor / nodeVisibility / nodeLabel closures read _currentImpactState,
  // so the kapsule digest that follows will paint nodes with tint colours.
  update3D(g, filterState);
  // Link tinting — apply with delays so the kapsule digest (debounced 1 ms)
  // and arc construction (rAF + 100ms) settle before we walk the scene.
  const _snap = state;
  requestAnimationFrame(() => _applyLinkTint(_snap));
  setTimeout(() => _applyLinkTint(_snap), 300);
  setTimeout(() => _applyLinkTint(_snap), 900);
}

/** Reset the globe camera to the default whole-globe view (lat=30, lon=-90). */
export function resetGlobeCamera(): void {
  if (!instance || currentLayout !== "globe") return;
  const cf = latLonToXYZ(30, -90, GLOBE_RADIUS * 3);
  instance.cameraPosition?.({ x: cf.x, y: cf.y, z: cf.z });
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (instance.camera?.() as any)?.lookAt(0, 0, 0);
}

function _applyLinkTint(state: ImpactState | null): void {
  if (!instance) return;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const scene: THREE.Scene = instance.scene();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  scene.traverse((obj: any) => {
    // Links in ball mode are THREE.Line objects; in globe mode they are
    // THREE.Mesh (TubeGeometry). Both get __graphObjType === "link" and
    // have an obj.material with a .color property.
    if (obj.__graphObjType !== "link" || !obj.material?.color) return;
    const data = obj.__data;
    if (state) {
      const src = data?.source?.id as string | undefined;
      const tgt = data?.target?.id as string | undefined;
      const srcFired = src ? tintColorRGB(state.byNode.get(src)) !== null : false;
      const tgtFired = tgt ? tintColorRGB(state.byNode.get(tgt)) !== null : false;
      if (srcFired && tgtFired) {
        obj.visible = true;
        obj.material.color.setRGB(1.0, 1.0, 1.0);  // white chain edges
        obj.material.opacity = 0.9;
        obj.material.transparent = true;
      } else {
        obj.visible = false;
      }
    } else {
      // No active impact trace — hide all arcs so the idle globe isn't
      // cluttered with 13 000+ edges. Arcs only become visible when an
      // impact trace fires (srcFired && tgtFired path above). This keeps
      // the globe readable at every market-filter setting.
      obj.visible = false;
    }
    obj.material.needsUpdate = true;
  });
}
