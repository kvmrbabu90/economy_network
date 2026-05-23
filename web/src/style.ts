// Single source of truth for Sigma node/edge attribute computation. Kept in
// lockstep with styles.css and CLAUDE.md's project palette.

import type { ApiEdge, ApiNode, EdgeType } from "./api";

export const EDGE_COLOR: Record<EdgeType, string> = {
  supplies: "#009688",
  customer_of: "#7b3fa0",
  competes_with: "#e0533d",
  regulated_by: "#2f6dbb",
  part_of: "#9c978a",
};

export const NODE_COLOR = {
  Company: "#4a5360",
  Regulator: "#2f6dbb",
  Provisional: "#c9c3b3",
  Default: "#7a7264",
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
  return {
    color: dim ? toRgba(base, 0.22) : base,
    size: dim ? 0.5 : Math.max(0.8, 0.8 + (e.attributes.confidence ?? 0.5) * 0.8),
    type: dim ? "line" : "line",
  };
}

function toRgba(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
