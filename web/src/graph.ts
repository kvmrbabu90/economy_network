// Graph state: a single graphology MultiGraph instance + idempotent merge
// from API responses + ForceAtlas2 layout management. Sigma is decoupled from
// this module so unit tests can exercise merge logic without a DOM.

import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";

import type { ApiEdge, ApiNode } from "./api";
import { LABEL_TOP_N } from "./config";
import { edgeAttributes, nodeColor, nodeSizeByType } from "./style";

// The full attribute payload we attach to each node/edge -- we store the
// original ApiNode/ApiEdge object alongside the rendered attributes so the
// inspector panel has access to provenance, identifiers, etc.
export interface NodeAttributes {
  apiNode: ApiNode;
  label: string;
  // Sigma renders this color/size/x/y; we (re)compute them after each merge.
  color: string;
  size: number;
  x: number;
  y: number;
  // Set to "" to suppress on Sigma's default rendering; the hover layer
  // injects a label for the focused node.
  displayLabel: string;
}

export interface EdgeAttributes {
  apiEdge: ApiEdge;
  edgeType: ApiEdge["attributes"]["type"];
  color: string;
  size: number;
}

export type EconGraph = Graph<NodeAttributes, EdgeAttributes>;

export function createGraph(): EconGraph {
  // MultiGraph because we want simultaneous edges of different types between
  // the same pair (e.g. supplies AND a derived customer_of view sharing
  // endpoints), and Sigma is fine with that.
  return new Graph({ type: "mixed", multi: true, allowSelfLoops: false });
}

/**
 * Merge API payloads into the live graph. Returns true if the topology
 * changed (nodes/edges added or removed), so callers can decide whether
 * to re-run the layout.
 */
export function mergeFromApi(
  g: EconGraph,
  nodes: ApiNode[],
  edges: ApiEdge[],
): boolean {
  let changed = false;

  for (const n of nodes) {
    if (g.hasNode(n.key)) continue;
    changed = true;
    g.addNode(n.key, {
      apiNode: n,
      label: n.attributes.label,
      color: nodeColor(n),
      size: 4, // re-sized after merge once we know final degrees
      // Seed positions on a unit ring around origin so FA2 has somewhere to
      // start from. New nodes added later inherit the same seed; the
      // attraction step pulls them to their connected component.
      x: Math.cos(Math.random() * Math.PI * 2) * 0.5,
      y: Math.sin(Math.random() * Math.PI * 2) * 0.5,
      displayLabel: "",
    });
  }

  for (const e of edges) {
    if (g.hasEdge(e.key)) continue;
    if (!g.hasNode(e.source) || !g.hasNode(e.target)) continue;
    changed = true;
    const attrs = edgeAttributes(e);
    if (e.undirected) {
      g.addUndirectedEdgeWithKey(e.key, e.source, e.target, {
        apiEdge: e, edgeType: e.attributes.type, ...attrs,
      });
    } else {
      g.addDirectedEdgeWithKey(e.key, e.source, e.target, {
        apiEdge: e, edgeType: e.attributes.type, ...attrs,
      });
    }
  }

  return changed;
}

/** Assign uniform per-type sizes and pick persistent labels for the top hubs. */
export function restyleAfterMerge(g: EconGraph): void {
  const degrees: Array<[string, number]> = [];
  g.forEachNode((id, attrs) => {
    const d = g.degree(id);
    degrees.push([id, d]);
    // Fixed radius per node type — all nodes of the same type are equal.
    // The global nodeScale in main.ts scales these uniformly via the reducer.
    g.setNodeAttribute(id, "size", nodeSizeByType(attrs.apiNode));
  });
  degrees.sort((a, b) => b[1] - a[1]);
  const showLabelFor = new Set(degrees.slice(0, LABEL_TOP_N).map(([id]) => id));
  g.forEachNode((id, attrs) => {
    g.setNodeAttribute(
      id,
      "displayLabel",
      showLabelFor.has(id) ? attrs.label : "",
    );
  });
}

/** Run FA2 to a settled-ish layout.
 *
 * Async — processes FA2 in BATCH-sized chunks and yields to the event loop
 * between each chunk via `setTimeout(0)`. For a full-core graph (2 600+ nodes,
 * 18 000+ edges) the total wall time is the same as the old synchronous call,
 * but the main thread is never blocked for more than a few hundred milliseconds
 * at a stretch, so:
 *   • CDP / console calls remain responsive during layout computation.
 *   • The browser can paint "Computing layout…" progress and respond to clicks.
 *   • Impact-trace results that arrive while layout is still running get applied
 *     correctly instead of being silently dropped behind a frozen event loop.
 */
export async function runLayout(g: EconGraph, iterations = 220): Promise<void> {
  // ForceAtlas2 requires at least 2 nodes: with 0 there is nothing to lay out,
  // and with exactly 1 the algorithm still runs fine but the gravity step can
  // produce NaN positions if the single node sits exactly at the origin.
  // Skip both degenerate cases.
  if (g.order < 2) return;
  // Guard: ForceAtlas2 fails silently if any node has NaN/undefined x or y.
  // Seed such nodes onto a unit ring so the simulation has valid starting positions.
  let i = 0;
  g.forEachNode((id, attrs) => {
    if (!isFinite(attrs.x) || !isFinite(attrs.y)) {
      const angle = (i / Math.max(1, g.order)) * Math.PI * 2;
      g.setNodeAttribute(id, "x", Math.cos(angle) * 0.5);
      g.setNodeAttribute(id, "y", Math.sin(angle) * 0.5);
    }
    i++;
  });
  const settings = {
    gravity: 1.0,
    scalingRatio: 8,
    slowDown: 1.5,
    strongGravityMode: false,
    barnesHutOptimize: g.order > 300,
    adjustSizes: false,
  };
  // Run FA2 in small batches, yielding to the event loop between each batch.
  // BATCH=5 keeps per-tick blocking < ~1 s even on a slow machine while
  // keeping total overhead (44 × ~4 ms setTimeout) well under a second.
  //
  // Yield strategy: setTimeout(0) when visible (allows repaints between
  // batches); microtask Promise.resolve() when hidden (Chrome throttles
  // setTimeout to ≥1 s in background/frozen tabs, but microtasks are exempt).
  const yieldFn = (): Promise<void> =>
    document.hidden
      ? Promise.resolve()
      : new Promise<void>(r => setTimeout(r, 0));
  const BATCH = 5;
  for (let done = 0; done < iterations; done += BATCH) {
    forceAtlas2.assign(g, {
      iterations: Math.min(BATCH, iterations - done),
      settings,
    });
    await yieldFn();
  }
}

/**
 * Cluster nodes by GICS sector: every Company sits in its sector's
 * pre-allocated arc around a center hub, regulators at the center.
 * Deterministic -- no force iteration -- so the clusters stay legible even
 * at full-graph scale. Best paired with a refresh() rather than re-running
 * FA2 (which would dissolve the clusters under edge attraction).
 */
const SECTOR_ORDER = [
  "Communication Services",
  "Consumer Discretionary",
  "Consumer Staples",
  "Energy",
  "Financials",
  "Health Care",
  "Industrials",
  "Information Technology",
  "Materials",
  "Real Estate",
  "Utilities",
];

export function layoutBySector(g: EconGraph): void {
  if (g.order === 0) return;
  const N = SECTOR_ORDER.length;
  // The anchor circle is large enough that 11 clusters don't overlap at
  // full S&P 500 scale; the scatter radius keeps each cluster legible.
  // ANCHOR_RADIUS bumped from 100 so the new central rings for
  // commodities (r=38) and retail regions (r=60) have breathing space
  // before the sector clusters begin.
  const ANCHOR_RADIUS = 115;
  const CLUSTER_RADIUS = 22;
  const SLUG_OUTER_RADIUS = 175;

  // For deterministic-looking layout, seed each cluster's intra-arrangement
  // with index-based positions rather than Math.random() so reloads land
  // the same place.
  const sectorCounts = new Map<string, number>();
  const sectorIndex = new Map<string, number>();
  let slugIndex = 0;
  let slugCount = 0;
  // Count central-hub nodes (regulators + commodities + retail regions)
  // separately so we can lay them out in their own concentric rings at
  // the center of the diagram instead of letting them collide with one
  // of the sector arcs.
  let regulatorCount = 0;
  let commodityCount = 0;
  let regionCount = 0;
  g.forEachNode((_id, attrs) => {
    const apiNode = attrs.apiNode;
    const t = apiNode.attributes.type;
    if (t === "Regulator") { regulatorCount += 1; return; }
    if (t === "Commodity") { commodityCount += 1; return; }
    if (t === "Region")    { regionCount    += 1; return; }
    if (apiNode.attributes.provisional) { slugCount += 1; return; }
    const s = apiNode.attributes.sector || "_unknown";
    sectorCounts.set(s, (sectorCounts.get(s) ?? 0) + 1);
  });

  let regulatorIdx = 0, commodityIdx = 0, regionIdx = 0;

  g.forEachNode((id, attrs) => {
    const apiNode = attrs.apiNode;
    const t = apiNode.attributes.type;
    let x = 0, y = 0;
    if (t === "Regulator") {
      // Regulators in the innermost ring -- they're hubs touching every
      // sector via regulated_by edges.
      const angle = (regulatorIdx++ / Math.max(1, regulatorCount)) * 2 * Math.PI;
      x = Math.cos(angle) * 14;
      y = Math.sin(angle) * 14;
    } else if (t === "Commodity") {
      // Commodities in a middle ring just outside regulators -- they
      // feed into many sectors via supplies edges.
      const angle = (commodityIdx++ / Math.max(1, commodityCount)) * 2 * Math.PI;
      x = Math.cos(angle) * 38;
      y = Math.sin(angle) * 38;
    } else if (t === "Region") {
      // Retail-consumer regions in the next ring -- every B2C-facing
      // sector points at them.
      const angle = (regionIdx++ / Math.max(1, regionCount)) * 2 * Math.PI;
      x = Math.cos(angle) * 60;
      y = Math.sin(angle) * 60;
    } else if (apiNode.attributes.provisional) {
      // Provisional slugs orbit at the outer rim, ordered around the
      // perimeter so click-expand fans out predictably.
      const angle = (slugIndex++ / Math.max(1, slugCount)) * 2 * Math.PI;
      x = Math.cos(angle) * SLUG_OUTER_RADIUS;
      y = Math.sin(angle) * SLUG_OUTER_RADIUS;
    } else {
      const sector = apiNode.attributes.sector || "_unknown";
      const sIdx = SECTOR_ORDER.indexOf(sector);
      const anchorAngle = ((sIdx >= 0 ? sIdx : N) / N) * 2 * Math.PI;
      const ax = Math.cos(anchorAngle) * ANCHOR_RADIUS;
      const ay = Math.sin(anchorAngle) * ANCHOR_RADIUS;
      // Place within the sector's arc -- spiral the nodes outward so dense
      // sectors (Industrials, Financials) stay readable.
      const k = sectorIndex.get(sector) ?? 0;
      sectorIndex.set(sector, k + 1);
      const total = sectorCounts.get(sector) ?? 1;
      // Golden-angle spiral inside the cluster gives even visual coverage.
      const r = CLUSTER_RADIUS * Math.sqrt(k / Math.max(1, total));
      const theta = k * 2.39996; // golden angle in radians
      x = ax + Math.cos(theta) * r;
      y = ay + Math.sin(theta) * r;
    }
    g.setNodeAttribute(id, "x", x);
    g.setNodeAttribute(id, "y", y);
  });
}

/** Clear and replace the graph contents in one shot. Used for re-center. */
export function replaceGraph(
  g: EconGraph,
  nodes: ApiNode[],
  edges: ApiEdge[],
): void {
  g.clear();
  mergeFromApi(g, nodes, edges);
}
