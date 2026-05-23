// Graph state: a single graphology MultiGraph instance + idempotent merge
// from API responses + ForceAtlas2 layout management. Sigma is decoupled from
// this module so unit tests can exercise merge logic without a DOM.

import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";

import type { ApiEdge, ApiNode } from "./api";
import { LABEL_TOP_N } from "./config";
import { edgeAttributes, nodeColor, nodeSizeFromDegree } from "./style";

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

/** Re-size nodes by degree and pick persistent labels for the top hubs. */
export function restyleAfterMerge(g: EconGraph): void {
  const degrees: Array<[string, number]> = [];
  g.forEachNode((id) => {
    const d = g.degree(id);
    degrees.push([id, d]);
    g.setNodeAttribute(id, "size", nodeSizeFromDegree(d));
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

/** Run FA2 to a settled-ish layout. Synchronous; runs in <500ms for ~200 nodes. */
export function runLayout(g: EconGraph, iterations = 220): void {
  if (g.order === 0) return;
  forceAtlas2.assign(g, {
    iterations,
    settings: {
      gravity: 1.0,
      scalingRatio: 8,
      slowDown: 1.5,
      strongGravityMode: false,
      barnesHutOptimize: g.order > 300,
      adjustSizes: false,
    },
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
