// Single source of truth for Sigma node/edge attribute computation. Kept in
// lockstep with styles.css and CLAUDE.md's project palette.

import type { ApiEdge, ApiNode, EdgeType } from "./api";

// Monochrome palette: all edges share the Commodity grey (#7a7e84) so
// connectors read as medium-grey plumbing rather than compositing
// toward white at high edge density.
export const EDGE_COLOR: Record<EdgeType, string> = {
  supplies: "#7a7e84",
  customer_of: "#7a7e84",
  competes_with: "#7a7e84",
  regulated_by: "#7a7e84",
  part_of: "#7a7e84",
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

// Fixed base radii per node type. All nodes of the same type render at
// the same size so the visual hierarchy comes from color + position, not
// from degree. The global nodeScale multiplier in main.ts scales these
// uniformly ([ = shrink, ] = grow).
export const NODE_BASE_SIZE: Record<string, number> = {
  Regulator: 10,
  Region:     8,  // consumer markets
  Commodity:  6,
  Company:    4,
  Provisional: 3,
  Default:    4,
};

export function nodeSizeByType(n: ApiNode): number {
  if (n.attributes.provisional) return NODE_BASE_SIZE.Provisional;
  return NODE_BASE_SIZE[n.attributes.type] ?? NODE_BASE_SIZE.Default;
}

/** @deprecated kept for call-sites that haven't been updated yet */
export function nodeSizeFromDegree(degree: number): number {
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
    // Core edges. Alpha=0.55 with premultiplied encoding (see toRgba below)
    // so individual edges appear at commodity-grey brightness and dense hub
    // areas converge toward the base color (#7a7e84) without blowing out.
    color = toRgba(base, 0.55);
    size = Math.max(0.8, 0.8 + (e.attributes.confidence ?? 0.5) * 0.5);
  } else if (inferred) {
    color = toRgba(base, 0.25);
    size = 0.6;
  } else {
    color = toRgba(base, 0.08);
    size = 0.4;
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
  // Sigma uses gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA) — premultiplied
  // alpha blending. With gl.ONE as source factor the shader outputs the FULL
  // rgb value regardless of alpha, causing any non-zero alpha to render at
  // full brightness. Fix: premultiply the RGB channels so the per-edge
  // contribution is (r·a, g·a, b·a). Sparse edges appear dark; dense hub
  // areas accumulate toward the base color (medium grey) instead of white.
  return `rgba(${Math.round(r * alpha)}, ${Math.round(g * alpha)}, ${Math.round(b * alpha)}, ${alpha})`;
}
