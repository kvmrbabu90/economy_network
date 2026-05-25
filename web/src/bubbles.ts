// Sector "bubble" view: each GICS sector renders as a single clickable hub
// when collapsed, and explodes into its individual companies when expanded.
// Cross-sector relationships are summed into aggregated bubble->bubble edges
// so the macro-view stays legible.

import type { ApiEdge, ApiNode, EdgeType } from "./api";
import type { EconGraph } from "./graph";
import { EDGE_COLOR } from "./style";

export const SECTORS = [
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
] as const;

export type Sector = (typeof SECTORS)[number];

const BUBBLE_PREFIX = "bubble:";
const BUBBLE_EDGE_PREFIX = "bubble-edge:";

// Same anchor ring as layoutBySector(), so bubbles sit where the company
// clusters would have been -- expanding feels like the cluster "exploding"
// out of its bubble.
const ANCHOR_RADIUS = 110;

export function bubbleIdFor(sector: string): string {
  return BUBBLE_PREFIX + sector;
}

export function isBubble(id: string): boolean {
  return id.startsWith(BUBBLE_PREFIX);
}

export function sectorOfBubble(id: string): string {
  return id.slice(BUBBLE_PREFIX.length);
}

function sectorAnchor(sectorIdx: number): { x: number; y: number } {
  const angle = (sectorIdx / SECTORS.length) * 2 * Math.PI;
  return { x: Math.cos(angle) * ANCHOR_RADIUS, y: Math.sin(angle) * ANCHOR_RADIUS };
}

/**
 * Ensure 11 sector-bubble virtual nodes exist on the graph. Idempotent.
 * Counts (companies per sector) are computed from the live graph and
 * stored on the bubble's apiNode for label/sizing in the renderer.
 */
export function ensureBubbleNodes(g: EconGraph): void {
  const counts = new Map<string, number>();
  g.forEachNode((_id, attrs) => {
    if (attrs.apiNode.attributes.type === "Regulator") return;
    if (attrs.apiNode.attributes.provisional) return;
    if (isBubble(_id)) return;
    const s = attrs.apiNode.attributes.sector || "";
    counts.set(s, (counts.get(s) ?? 0) + 1);
  });
  SECTORS.forEach((sector, i) => {
    const id = bubbleIdFor(sector);
    if (g.hasNode(id)) return;
    const anchor = sectorAnchor(i);
    const n = counts.get(sector) ?? 0;
    const bubbleApiNode: ApiNode = {
      key: id,
      attributes: {
        label: `${sector} (${n})`,
        type: "Company",  // schema doesn't need a new type; reducer styles it
        sector,
        industry: null,
        country: null,
        tickers: [],
        identifiers: {},
        aliases: [],
        provisional: false,
        identity_unverified: false,
        metadata: { bubble: true, sectorCount: n },
      },
    };
    g.addNode(id, {
      apiNode: bubbleApiNode,
      label: bubbleApiNode.attributes.label,
      color: "#6ea8f0",  // distinct neutral blue
      // Sized large so it reads as a hub even at fit-to-view.
      size: Math.max(12, 8 + Math.sqrt(n) * 1.6),
      x: anchor.x,
      y: anchor.y,
      displayLabel: bubbleApiNode.attributes.label,
    });
  });
}

/**
 * Add aggregated bubble<->bubble edges for cross-sector relationships.
 * One edge per (sectorA, sectorB, edgeType) unordered pair, sized by count.
 * Idempotent: drops existing virtual edges and recomputes.
 */
export function refreshBubbleEdges(g: EconGraph): void {
  // Remove existing virtual bubble edges first.
  const toDrop: string[] = [];
  g.forEachEdge((id) => { if (id.startsWith(BUBBLE_EDGE_PREFIX)) toDrop.push(id); });
  toDrop.forEach((id) => g.dropEdge(id));

  // Tally counts per (sectorA, sectorB, type).
  const tally = new Map<string, { count: number; type: EdgeType; sA: string; sB: string }>();
  g.forEachEdge((eid, attrs, src, tgt) => {
    if (eid.startsWith(BUBBLE_EDGE_PREFIX)) return;
    const sA = sectorOfNode(g, src);
    const sB = sectorOfNode(g, tgt);
    if (!sA || !sB || sA === sB) return; // intra-sector or unattributable
    const key = [attrs.edgeType, sA, sB].sort().join("|");
    const existing = tally.get(key);
    if (existing) existing.count += 1;
    else tally.set(key, { count: 1, type: attrs.edgeType, sA, sB });
  });

  tally.forEach((v) => {
    const sourceBubble = bubbleIdFor(v.sA);
    const targetBubble = bubbleIdFor(v.sB);
    if (!g.hasNode(sourceBubble) || !g.hasNode(targetBubble)) return;
    const eid = `${BUBBLE_EDGE_PREFIX}${v.type}:${v.sA}:${v.sB}`;
    if (g.hasEdge(eid)) return;
    const color = EDGE_COLOR[v.type] ?? "#8a8e94";
    const apiEdge: ApiEdge = {
      key: eid,
      source: sourceBubble,
      target: targetBubble,
      attributes: {
        type: v.type,
        directed: v.type !== "competes_with",
        confidence: 1,
        weight: v.count,
        below_threshold: false,
        provenance: {
          filing: "aggregated",
          url: "",
          snippet: `${v.count} ${v.type} edges between ${v.sA} and ${v.sB} filers`,
          extracted_by: "rule",
        },
        additional_provenance: [],
        derived: false,
      },
      undirected: v.type === "competes_with",
    };
    const attrs = {
      apiEdge,
      edgeType: v.type,
      color,
      size: Math.max(0.8, Math.log2(1 + v.count) * 1.2),
    };
    if (v.type === "competes_with") {
      g.addUndirectedEdgeWithKey(eid, sourceBubble, targetBubble, attrs);
    } else {
      g.addDirectedEdgeWithKey(eid, sourceBubble, targetBubble, attrs);
    }
  });
}

function sectorOfNode(g: EconGraph, id: string): string | null {
  if (isBubble(id)) return sectorOfBubble(id);
  if (!g.hasNode(id)) return null;
  const a = g.getNodeAttributes(id).apiNode.attributes;
  if (a.type === "Regulator") return null;  // regulators don't belong to a sector
  if (a.provisional) return null;            // slugs likewise
  return a.sector || null;
}

/**
 * Compute which nodes should be hidden given an expansion set. A node is
 * visible iff its sector is in `expanded`; a bubble is visible iff its
 * sector is NOT in `expanded`. Regulators and provisional slugs are always
 * visible (subject to the existing filter toggles).
 */
export function bubbleVisibility(
  g: EconGraph,
  expanded: Set<string>,
): { nodes: Set<string>; edges: Set<string> } {
  const hiddenNodes = new Set<string>();
  const hiddenEdges = new Set<string>();

  g.forEachNode((id, attrs) => {
    if (isBubble(id)) {
      // Bubbles stay visible ALWAYS: when collapsed they're the cluster
      // proxy; when expanded they're the smaller "click to collapse"
      // affordance pinned at the sector anchor. The label switches
      // dynamically (see updateBubbleLabels()).
      return;
    }
    const a = attrs.apiNode.attributes;
    if (a.type === "Regulator" || a.provisional) return;
    const sector = a.sector || "";
    if (!expanded.has(sector)) hiddenNodes.add(id);
  });

  // Bubble<->bubble aggregated edges: hide a virtual edge when EITHER side
  // has been expanded (its companies' real edges are what's interesting
  // in that case).
  g.forEachEdge((eid, _attrs, src, tgt) => {
    const isVirtualEdge = eid.startsWith(BUBBLE_EDGE_PREFIX);
    if (isVirtualEdge) {
      const sA = isBubble(src) ? sectorOfBubble(src) : "";
      const sB = isBubble(tgt) ? sectorOfBubble(tgt) : "";
      if (expanded.has(sA) || expanded.has(sB)) hiddenEdges.add(eid);
      return;
    }
    // Hide a real edge if either endpoint is hidden (collapsed sector member).
    if (hiddenNodes.has(src) || hiddenNodes.has(tgt)) hiddenEdges.add(eid);
  });

  return { nodes: hiddenNodes, edges: hiddenEdges };
}

/**
 * Update bubble labels + sizes to reflect expansion state. Collapsed bubbles
 * read "Industrials (79)"; expanded bubbles shrink and read "Industrials ✕"
 * so they're recognizable as a collapse target without dominating the
 * visual.
 */
export function updateBubbleAppearance(g: EconGraph, expanded: Set<string>): void {
  g.forEachNode((id, attrs) => {
    if (!isBubble(id)) return;
    const sector = sectorOfBubble(id);
    const md = attrs.apiNode.attributes.metadata as { sectorCount?: number };
    const n = md.sectorCount ?? 0;
    const isExpanded = expanded.has(sector);
    if (isExpanded) {
      g.setNodeAttribute(id, "label", `${sector} ✕`);
      g.setNodeAttribute(id, "size", 6);
      g.setNodeAttribute(id, "color", "#4a4e54"); // recede when expanded
      g.setNodeAttribute(id, "displayLabel", `${sector} ✕`);
    } else {
      g.setNodeAttribute(id, "label", `${sector} (${n})`);
      g.setNodeAttribute(id, "size", Math.max(12, 8 + Math.sqrt(n) * 1.6));
      g.setNodeAttribute(id, "color", "#8a8f96"); // medium-grey hub
      g.setNodeAttribute(id, "displayLabel", `${sector} (${n})`);
    }
  });
}

/**
 * Position nodes for bubble view: bubbles at their sector anchors,
 * expanded-sector companies in a spiral cluster around that anchor,
 * regulators at the center, provisional slugs orbiting the outer rim.
 */
export function layoutBubbleView(g: EconGraph, expanded: Set<string>): void {
  const sectorClusterRadius = 28;
  const slugOuterRadius = 200;

  // Per-sector enumeration so spiral positions stay deterministic.
  const sectorOrder = new Map<string, number>();
  let slugCount = 0;
  g.forEachNode((id, attrs) => {
    if (isBubble(id)) return;
    if (attrs.apiNode.attributes.type === "Regulator") return;
    if (attrs.apiNode.attributes.provisional) { slugCount += 1; return; }
  });
  let slugIdx = 0;
  const sectorCounters = new Map<string, number>();

  g.forEachNode((id, attrs) => {
    if (isBubble(id)) {
      const idx = SECTORS.indexOf(sectorOfBubble(id) as Sector);
      const anchor = sectorAnchor(idx);
      g.setNodeAttribute(id, "x", anchor.x);
      g.setNodeAttribute(id, "y", anchor.y);
      return;
    }
    const a = attrs.apiNode.attributes;
    if (a.type === "Regulator") {
      // Regulators in a tight cluster at the center.
      const seed = id.charCodeAt(id.length - 1);
      const angle = (seed % 7) / 7 * 2 * Math.PI;
      g.setNodeAttribute(id, "x", Math.cos(angle) * 8);
      g.setNodeAttribute(id, "y", Math.sin(angle) * 8);
      return;
    }
    if (a.provisional) {
      const angle = (slugIdx++ / Math.max(1, slugCount)) * 2 * Math.PI;
      g.setNodeAttribute(id, "x", Math.cos(angle) * slugOuterRadius);
      g.setNodeAttribute(id, "y", Math.sin(angle) * slugOuterRadius);
      return;
    }
    const sector = a.sector || "";
    const idx = SECTORS.indexOf(sector as Sector);
    const anchor = sectorAnchor(idx);
    if (!expanded.has(sector)) {
      // Park the company on top of its bubble; the visibility filter
      // hides it but layout is set in case the user expands.
      g.setNodeAttribute(id, "x", anchor.x);
      g.setNodeAttribute(id, "y", anchor.y);
      sectorOrder.set(id, 0);
      return;
    }
    const k = sectorCounters.get(sector) ?? 0;
    sectorCounters.set(sector, k + 1);
    const r = sectorClusterRadius * Math.sqrt(k / Math.max(1, 10));
    const theta = k * 2.39996; // golden angle
    g.setNodeAttribute(id, "x", anchor.x + Math.cos(theta) * r);
    g.setNodeAttribute(id, "y", anchor.y + Math.sin(theta) * r);
  });
}
