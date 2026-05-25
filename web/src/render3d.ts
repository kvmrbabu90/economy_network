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

// Country -> approximate geographic center, used as a fallback HQ
// position for companies that have a `country` attribute but never
// got Wikidata HQ coords (about 41 of the 500 S&P filers fall through
// the SPARQL net). The MVP corpus is 100% US, so only "US" matters
// today -- add countries here as the corpus expands.
const COUNTRY_CENTROID: Record<string, { lat: number; lon: number }> = {
  US: { lat: 39.83, lon: -98.58 }, // Lebanon, KS -- contiguous-US centroid
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
      let p: { x: number; y: number; z: number } | null = null;
      if (wd && typeof wd.lat === "number" && typeof wd.lon === "number") {
        // Defensive: a small number of Wikidata records come back with
        // lat/lon swapped (the field stores longitude where it should
        // store latitude). Detect the swap by checking that lat is in
        // valid range; if not but lon is, flip them.
        let { lat, lon } = wd as { lat: number; lon: number };
        const latOK = lat >= -90 && lat <= 90;
        const lonOK = lon >= -180 && lon <= 180;
        if (!latOK && lonOK) {
          const tmp = lat; lat = lon; lon = tmp;
        }
        p = latLonToXYZ(lat, lon);
      } else if (apiNode.attributes.type === "Regulator") {
        // All but one of our regulators are US federal -- pin at
        // Washington DC. NAIC is the only multistate body; nudge it to
        // Kansas City where the NAIC central office actually sits.
        const isNAIC = apiNode.key === "regulator:naic";
        p = isNAIC
          ? latLonToXYZ(39.0997, -94.5786)   // Kansas City, MO
          : latLonToXYZ(38.8951, -77.0364);  // Washington, DC
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
          p = latLonToXYZ(centroid.lat + jLat, centroid.lon + jLon);
        } else {
          // Truly unknown -- stash south of Antarctica so the node
          // doesn't pretend to sit on a real continent.
          const lon = (hash % 360) - 180;
          const lat = -82 - ((hash >> 8) & 7);  // -82..-89
          p = latLonToXYZ(lat, lon);
        }
      }
      node.fx = p.x; node.fy = p.y; node.fz = p.z;
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
    // Arc heights -- moderate bowl. Earlier 0.15/0.70 swung so high that
    // far-side arc midpoints sat behind the globe relative to the camera,
    // so users saw only the near-endpoint stub climbing up like a radial
    // antenna. 0.08/0.30 keeps the apex hugging the surface (peak radius
    // 220-260 against a 200-unit globe) so arcs read as ribbons over
    // the planet rather than spikes. Paired with depthTest=false on the
    // arc material so the back half stays visible.
    const ARC_BASE_HEIGHT = GLOBE_RADIUS * 0.08;
    const ARC_MAX_HEIGHT = GLOBE_RADIUS * 0.30;
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
        // zoom. Geometry is built lazily in buildArcs() once node
        // positions resolve.
        //
        // depthTest=false so the back half of each great-circle arc still
        // renders through the globe sphere -- otherwise far-side arcs
        // look like radial spikes climbing up from near-side endpoints
        // before disappearing behind the horizon. depthWrite=false keeps
        // overlapping arcs from punching holes in each other's alpha.
        // Lower opacity (0.45 / 0.18) because depthTest off means arcs
        // pile on top of nodes + each other; thinner alpha prevents the
        // network from washing out the surface beneath.
        const mat = new THREE.MeshBasicMaterial({
          color: link.color,
          transparent: true,
          opacity: link.below ? 0.18 : 0.45,
          depthTest: false,
          depthWrite: false,
        });
        const mesh = new THREE.Mesh(new THREE.BufferGeometry(), mat);
        // Render arcs AFTER the globe sphere (renderOrder default 0) so
        // alpha-blending stacks them correctly against the dark surface.
        mesh.renderOrder = 5;
        return mesh;
      });

    const TUBE_RADIUS_CORE = 0.6;
    const TUBE_RADIUS_AUDIT = 0.3;
    const TUBE_RADIAL_SEGMENTS = 6;
    const buildArcs = () => {
      if (!instance) return;
      // three-forcegraph binds the rendered THREE object onto each link as
      // `__lineObj` (see node_modules/three-forcegraph/dist/.../objBindAttr).
      // Scene-walking via __graphObjType missed everything (built=0,
      // skipped=0 in console), so iterate the link list directly.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data = (instance as any).graphData ? (instance as any).graphData() : null;
      const links: ForceLink[] = (data && data.links) || [];
      let built = 0, skipped = 0;
      for (const link of links) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const obj: any = (link as any).__lineObj || (link as any).__threeObj;
        if (!obj) { skipped++; continue; }
        // If three-forcegraph wrapped our custom mesh in a Group (extend
        // mode) or attached the mesh as a child, find the mesh inside.
        const mesh: any = obj.isMesh
          ? obj
          : (obj.children && obj.children.find((c: any) => c.isMesh));
        if (!mesh) { skipped++; continue; }
        const src = (link as any).source;
        const tgt = (link as any).target;
        if (!src || !tgt || typeof src.x !== "number" || typeof tgt.x !== "number") {
          skipped++;
          continue;
        }
        const s = new THREE.Vector3(src.x, src.y, src.z);
        const e = new THREE.Vector3(tgt.x, tgt.y, tgt.z);
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
          (link as any).below ? TUBE_RADIUS_AUDIT : TUBE_RADIUS_CORE,
          TUBE_RADIAL_SEGMENTS,
          false,
        );
        if (mesh.geometry) mesh.geometry.dispose();
        mesh.geometry = tubeGeom;
        mesh.frustumCulled = false;
        built++;
      }
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).__arcBuilt = { built, skipped };
    };
    // First pass: positions are set as soon as 3d-force-graph copies the
    // graphData into its internal sim. A single rAF is usually enough;
    // a 100ms safety net catches any later resolution.
    requestAnimationFrame(buildArcs);
    setTimeout(buildArcs, 100);
    setTimeout(buildArcs, 600);
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
  const cameraRef = instance.camera();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const ctrlRef: any = instance.controls();
  const refCamDist = cameraRef.position.length() || 1;
  const defaultRotateSpeed = ctrlRef.rotateSpeed ?? 1.0;
  const defaultPanSpeed = ctrlRef.panSpeed ?? 1.0;
  const tuneControls = () => {
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
      } else if (obj.__graphObjType === "link" && obj.scale) {
        // Scale X/Y only -- shrinks the tube radius while keeping it
        // attached to its endpoints (Z is the tube's length axis in
        // 3d-force-graph's local frame).
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
      } else if (obj.__graphObjType === "link" && obj.scale) {
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

  // Expose for live debugging / preview-tool inspection.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).__force3D = instance;

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
