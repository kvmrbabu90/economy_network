// Impact-overlay: tints nodes red/green by verdict and dims everything
// outside the impact subgraph. Works on both Sigma (2D) and the
// 3d-force-graph instance (3D / Globe). Apply / clear are pure functions
// over an ImpactResponse -- the bus that calls them lives in main.ts.

import type { ImpactResponse, ImpactVerdict } from "./api";
import type { EconGraph } from "./graph";

// Re-exported for the UI.
export type { ImpactResponse, ImpactVerdict };

// Hop -> opacity multiplier. Hop 0 (seed) is full; further hops fade.
const HOP_INTENSITY = [1.0, 0.85, 0.65, 0.45, 0.28, 0.15];

// Base colours for tinting. Hex; we mix toward these at the verdict
// magnitude * hop intensity.
const COLOR_POSITIVE = { r: 64, g: 200, b: 110 };   // soft green
const COLOR_NEGATIVE = { r: 226, g: 84, b: 84 };    // soft red
const COLOR_NEUTRAL = { r: 24, g: 28, b: 34 };      // dim grey (outside chain)

export interface ImpactState {
  byNode: Map<string, ImpactVerdict>;
  chainEdges: Set<string>;
}

/** Build a quick lookup + the set of edges that participate in the chain
 * (any edge whose source and target are both in the verdict set). */
export function buildImpactState(g: EconGraph, resp: ImpactResponse): ImpactState {
  const byNode = new Map<string, ImpactVerdict>();
  for (const v of resp.impacts) byNode.set(v.node_id, v);
  const chainEdges = new Set<string>();
  g.forEachEdge((eid, _attrs, src, tgt) => {
    if (byNode.has(src) && byNode.has(tgt)) {
      chainEdges.add(eid);
    }
  });
  return { byNode, chainEdges };
}

/** Mix a base node colour toward the verdict tint by intensity. */
export function tintColor(verdict: ImpactVerdict | undefined): string | null {
  if (!verdict) return null;
  if (verdict.direction === "no_effect" || verdict.magnitude <= 0.05) return null;
  const intensity =
    (HOP_INTENSITY[verdict.hop] ?? 0.15) *
    Math.min(1, Math.max(0.1, verdict.magnitude));
  const base = verdict.direction === "positive" ? COLOR_POSITIVE : COLOR_NEGATIVE;
  // Mix base towards white-ish? No -- return saturated tint; downstream
  // node renderers paint over the base grey.
  const r = Math.round(base.r * intensity + 90 * (1 - intensity));
  const g = Math.round(base.g * intensity + 90 * (1 - intensity));
  const b = Math.round(base.b * intensity + 90 * (1 - intensity));
  return `rgb(${r}, ${g}, ${b})`;
}

/** Hex / rgb helper used by 3D mesh material. */
export function tintColorRGB(verdict: ImpactVerdict | undefined): { r: number; g: number; b: number } | null {
  if (!verdict) return null;
  if (verdict.direction === "no_effect" || verdict.magnitude <= 0.05) return null;
  const intensity =
    (HOP_INTENSITY[verdict.hop] ?? 0.15) *
    Math.min(1, Math.max(0.1, verdict.magnitude));
  const base = verdict.direction === "positive" ? COLOR_POSITIVE : COLOR_NEGATIVE;
  const r = (base.r * intensity + 90 * (1 - intensity)) / 255;
  const g = (base.g * intensity + 90 * (1 - intensity)) / 255;
  const b = (base.b * intensity + 90 * (1 - intensity)) / 255;
  return { r, g, b };
}

export function dimColor(): { r: number; g: number; b: number } {
  return { r: COLOR_NEUTRAL.r / 255, g: COLOR_NEUTRAL.g / 255, b: COLOR_NEUTRAL.b / 255 };
}
