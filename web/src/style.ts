// Single source of truth for Sigma node/edge attribute computation. Kept in
// lockstep with styles.css and CLAUDE.md's project palette.

import type { ApiEdge, ApiNode, EdgeType } from "./api";

// Monochrome palette: all edges are a medium shade of grey regardless of
// relationship type, and node greys step from black (Regulator) -> dark
// (Region/consumer market) -> medium (Commodity) -> light (Company).
// Provisional / unknown stay dark so they recede behind the core data.
export const EDGE_COLOR: Record<EdgeType, string> = {
  supplies: "#8a8e94",
  customer_of: "#8a8e94",
  competes_with: "#8a8e94",
  regulated_by: "#8a8e94",
  part_of: "#8a8e94",
};

export const NODE_COLOR = {
  Regulator: "#0a0c10",   // black
  Region: "#3a3e44",      // dark grey -- consumer markets
  Commodity: "#7a7e84",   // medium grey
  Company: "#c8ccd2",     // light grey -- businesses
  Provisional: "#4a4e54", // recedes behind the core
  Default: "#8d8e94",
} as const;

export function nodeColor(n: ApiNode): string {
  if (n.attributes.provisional) return NODE_COLOR.Provisional;
  const t = n.attributes.type;
  if (t === "Regulator") return NODE_COLOR.Regulator;
  if (t === "Region")    return NODE_COLOR.Region;
  if (t === "Commodity") return NODE_COLOR.Commodity;
  if (t === "Company")   return NODE_COLOR.Company;
  return NODE_COLOR.Default;
}

export function nodeSizeFromDegree(degree: number): number {
  // Logarithmic scaling so a degree-36 hub doesn't dwarf the rest.
  return Math.max(3, Math.min(18, 3 + Math.sqrt(degree) * 1.6));
}

export function edgeAttributes(e: ApiEdge) {
  const base = EDGE_COLOR[e.attributes.type] ?? "#9e9e9e";
  const directed = e.attributes.directed && e.attributes.type !== "competes_with";

  // Three visual tiers driven by provenance, not just confidence:
  //   * core (above threshold)             -> full color, full size, arrowhead
  //   * inference layer (co-mention)       -> 55% alpha, thinner. Same color so
  //                                            the relationship still reads;
  //                                            inferred-ness is conveyed by
  //                                            translucency.
  //   * audit / provisional-slug (below)    -> 22% alpha, very thin. The
  //                                            background-halo treatment.
  const extractedBy = e.attributes.provenance?.extracted_by ?? "";
  const inferred = extractedBy.startsWith("inference:");
  const dim = e.attributes.below_threshold;
  let color: string;
  let size: number;
  if (!dim) {
    color = base;
    size = Math.max(0.8, 0.8 + (e.attributes.confidence ?? 0.5) * 0.8);
  } else if (inferred) {
    color = toRgba(base, 0.55);
    size = 0.7;
  } else {
    color = toRgba(base, 0.22);
    size = 0.5;
  }
  // `directed` is intentionally ignored -- monochrome theme has no
  // arrowheads (per user request). All edges render as plain lines.
  void directed;
  return {
    color,
    size,
    type: "line",
  };
}

function toRgba(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
