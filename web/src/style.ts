// Single source of truth for Sigma node/edge attribute computation. Kept in
// lockstep with styles.css and CLAUDE.md's project palette.

import type { ApiEdge, ApiNode, EdgeType } from "./api";

// Dark-mode palette. Kept in lockstep with styles.css custom properties.
// Edge colors are the brightened relationship hues; node colors are tuned
// so real Companies read as cool gray, Regulators echo the regulated_by
// blue, and provisional slugs sit darker so they recede behind the core.
export const EDGE_COLOR: Record<EdgeType, string> = {
  supplies: "#2ec5b4",
  customer_of: "#b07ee0",
  competes_with: "#ff8a6b",
  regulated_by: "#6ea8f0",
  part_of: "#7a7268",
};

export const NODE_COLOR = {
  Company: "#aab2bc",
  Regulator: "#6ea8f0",
  Provisional: "#4a4e54",
  Default: "#8d8e94",
} as const;

export function nodeColor(n: ApiNode): string {
  if (n.attributes.provisional) return NODE_COLOR.Provisional;
  if (n.attributes.type === "Regulator") return NODE_COLOR.Regulator;
  if (n.attributes.type === "Company") return NODE_COLOR.Company;
  return NODE_COLOR.Default;
}

export function nodeSizeFromDegree(degree: number): number {
  // Logarithmic scaling so a degree-36 hub doesn't dwarf the rest.
  return Math.max(3, Math.min(18, 3 + Math.sqrt(degree) * 1.6));
}

export function edgeAttributes(e: ApiEdge) {
  const base = EDGE_COLOR[e.attributes.type] ?? "#9e9e9e";
  const dim = e.attributes.below_threshold;
  // Sigma edge "type" controls the renderer program. Directed relationships
  // (supplies / customer_of / regulated_by / part_of) draw with an arrowhead
  // so the reading direction is unambiguous on the canvas; competes_with is
  // undirected and stays a plain line.
  const directed = e.attributes.directed && e.attributes.type !== "competes_with";
  return {
    color: dim ? toRgba(base, 0.22) : base,
    size: dim ? 0.5 : Math.max(0.8, 0.8 + (e.attributes.confidence ?? 0.5) * 0.8),
    type: directed ? "arrow" : "line",
  };
}

function toRgba(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
