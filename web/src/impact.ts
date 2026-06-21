// Impact-overlay: tints nodes red/green by verdict and dims everything
// outside the impact subgraph. Works on both Sigma (2D) and the
// 3d-force-graph instance (3D / Globe). Apply / clear are pure functions
// over an ImpactResponse -- the bus that calls them lives in main.ts.

import type { ImpactResponse, ImpactVerdict, MultiImpactResponse } from "./api";
import type { EconGraph } from "./graph";

// Re-exported for the UI.
export type { ImpactResponse, ImpactVerdict, MultiImpactResponse };

// Hop -> intensity multiplier. Hop 0 (seed) is full; further hops attenuate.
const HOP_INTENSITY = [1.0, 0.85, 0.65, 0.45, 0.28, 0.15];

// Four-tier palette — positive (green) and negative (red).
// Tier is chosen by combined intensity = hop_factor × magnitude.
// Colors are kept bright/vivid so they read clearly against the dark
// globe background (#14171a). Dark values were invisible before this fix.
const TIERS = {
  positive: {
    high:   { r: 0x00, g: 0xe0, b: 0x44 },  // #00e044 bright green
    medHi:  { r: 0x00, g: 0xb8, b: 0x33 },  // #00b833
    medLo:  { r: 0x00, g: 0x90, b: 0x22 },  // #009022
    low:    { r: 0x00, g: 0x68, b: 0x14 },  // #006814
  },
  negative: {
    high:   { r: 0xff, g: 0x33, b: 0x00 },  // #ff3300 bright red-orange
    medHi:  { r: 0xdd, g: 0x22, b: 0x00 },  // #dd2200
    medLo:  { r: 0xbb, g: 0x16, b: 0x00 },  // #bb1600
    low:    { r: 0x99, g: 0x0d, b: 0x00 },  // #990d00
  },
  // Mixed-signal nodes: amber/orange.
  mixed: {
    high:   { r: 0xff, g: 0x99, b: 0x00 },  // #ff9900 amber
    medHi:  { r: 0xdd, g: 0x7d, b: 0x00 },  // #dd7d00
    medLo:  { r: 0xbb, g: 0x66, b: 0x00 },  // #bb6600
    low:    { r: 0x99, g: 0x52, b: 0x00 },  // #995200
  },
} as const;

/** Pick the colour tier from a combined intensity score (0–1). */
function pickTier(intensity: number): "high" | "medHi" | "medLo" | "low" {
  if (intensity >= 0.60) return "high";
  if (intensity >= 0.35) return "medHi";
  if (intensity >= 0.15) return "medLo";
  return "low";
}

const COLOR_NEUTRAL = { r: 24, g: 28, b: 34 };      // dim grey (outside chain)

// Unscored: the engine reached this node but could not get a verdict for it
// (stream 2). Distinct from both the red/green impact tiers and the near-black
// dim of non-impacted nodes — a muted slate that reads as "reached, unknown".
const COLOR_UNSCORED = { r: 124, g: 134, b: 150 };

export interface ImpactState {
  byNode: Map<string, ImpactVerdict>;
  chainEdges: Set<string>;
}

/** Build a quick lookup + the set of edges that participate in the chain
 * (any edge whose source and target are both in the verdict set). */
export function buildImpactState(g: EconGraph, resp: ImpactResponse): ImpactState {
  const byNode = new Map<string, ImpactVerdict>();
  for (const v of (resp.impacts ?? [])) {
    // Regulators are not investable — skip them unless they are hop 0
    // (the explicit seed, e.g. "FDA approves a new drug"). Hop-0 seeds
    // are intentional; hop ≥ 1 regulators are BFS artefacts that pollute
    // the chain (the backend now blocks them too, but guard here for
    // cached archive entries that predate the fix).
    if (v.hop > 0 && g.hasNode(v.node_id) &&
        g.getNodeAttributes(v.node_id).apiNode?.attributes?.type === "Regulator") {
      continue;
    }
    byNode.set(v.node_id, v);
  }
  const chainEdges = new Set<string>();
  g.forEachEdge((eid, _attrs, src, tgt) => {
    if (byNode.has(src) && byNode.has(tgt)) {
      chainEdges.add(eid);
    }
  });
  return { byNode, chainEdges };
}

/** Return the discrete tier colour for a verdict (CSS string for Sigma 2D).
 *  Returns null for no_effect or below-threshold nodes (hidden in impact mode).
 *  Phase K: nodes without financial grounding (is_estimated=true) are rendered
 *  at 70% opacity so grounded nodes stand out. */
export function tintColor(verdict: ImpactVerdict | undefined): string | null {
  if (!verdict) return null;
  if (verdict.direction === "unscored") {
    const c = COLOR_UNSCORED;
    return `rgb(${c.r}, ${c.g}, ${c.b})`;
  }
  const mag = Number(verdict.magnitude);
  if (!isFinite(mag)) return null;
  if (verdict.direction === "no_effect" || mag <= 0.05) return null;
  const hop = Number.isInteger(verdict.hop) && verdict.hop >= 0 ? verdict.hop : 0;
  const intensity = (HOP_INTENSITY[hop] ?? 0.15) * Math.min(1, mag);
  const tier = pickTier(intensity);
  const palette = verdict.mixed_signals
    ? TIERS.mixed
    : verdict.direction === "positive" ? TIERS.positive : TIERS.negative;
  const c = palette[tier];
  // Phase K: dim nodes with no financial weight anchor
  const alpha = verdict.is_estimated !== false && verdict.edge_weight == null ? 0.70 : 1.0;
  return alpha < 1 ? `rgba(${c.r}, ${c.g}, ${c.b}, ${alpha})` : `rgb(${c.r}, ${c.g}, ${c.b})`;
}

/** Same tier lookup, returning 0–1 floats for three.js material colour. */
export function tintColorRGB(verdict: ImpactVerdict | undefined): { r: number; g: number; b: number } | null {
  if (!verdict) return null;
  if (verdict.direction === "unscored") {
    return { r: COLOR_UNSCORED.r / 255, g: COLOR_UNSCORED.g / 255, b: COLOR_UNSCORED.b / 255 };
  }
  const mag = Number(verdict.magnitude);
  if (!isFinite(mag)) return null;
  if (verdict.direction === "no_effect" || mag <= 0.05) return null;
  const hop = Number.isInteger(verdict.hop) && verdict.hop >= 0 ? verdict.hop : 0;
  const intensity = (HOP_INTENSITY[hop] ?? 0.15) * Math.min(1, mag);
  const tier = pickTier(intensity);
  const palette = verdict.mixed_signals
    ? TIERS.mixed
    : verdict.direction === "positive" ? TIERS.positive : TIERS.negative;
  const c = palette[tier];
  // Phase K: blend toward neutral grey for estimated (ungrounded) nodes
  if (verdict.is_estimated !== false && verdict.edge_weight == null) {
    const blend = 0.70;  // keep 70% of color, 30% neutral grey
    const grey = 0.18;
    return {
      r: c.r / 255 * blend + grey * (1 - blend),
      g: c.g / 255 * blend + grey * (1 - blend),
      b: c.b / 255 * blend + grey * (1 - blend),
    };
  }
  return { r: c.r / 255, g: c.g / 255, b: c.b / 255 };
}

export function dimColor(): { r: number; g: number; b: number } {
  return { r: COLOR_NEUTRAL.r / 255, g: COLOR_NEUTRAL.g / 255, b: COLOR_NEUTRAL.b / 255 };
}

/** Build ImpactState from a multi-news merged response.
 *  Uses the merged (netted) verdicts — each node appears once with its
 *  net direction and mixed_signals flag. Chain edges are any edge whose
 *  both endpoints have a non-null tint (i.e. both fired in some event). */
export function buildMultiImpactState(g: EconGraph, resp: MultiImpactResponse): ImpactState {
  const byNode = new Map<string, ImpactVerdict>();
  for (const v of (resp.merged ?? [])) {
    // Same regulator guard as buildImpactState — skip hop ≥ 1 regulators.
    if (v.hop > 0 && g.hasNode(v.node_id) &&
        g.getNodeAttributes(v.node_id).apiNode?.attributes?.type === "Regulator") {
      continue;
    }
    byNode.set(v.node_id, v);
  }
  const chainEdges = new Set<string>();
  g.forEachEdge((eid, _attrs, src, tgt) => {
    if (byNode.has(src) && byNode.has(tgt)) chainEdges.add(eid);
  });
  return { byNode, chainEdges };
}
