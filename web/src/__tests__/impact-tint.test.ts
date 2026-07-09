import Graph from "graphology";
import { describe, expect, it } from "vitest";
import { tintColor, tintColorRGB, tintColorForCombined, buildImpactState } from "../impact";
import type { ImpactResponse, ImpactVerdict } from "../api";

function v(partial: Partial<ImpactVerdict>): ImpactVerdict {
  return {
    node_id: "x", name: "X", type: "Company",
    direction: "no_effect", magnitude: 0, hop: 1,
    reasoning: "", via_parent: null, edge_type: null, ...partial,
  } as ImpactVerdict;
}

describe("unscored tint", () => {
  it("tintColor returns a non-null, distinct colour for unscored (despite magnitude 0)", () => {
    const c = tintColor(v({ direction: "unscored", magnitude: 0 }));
    expect(c).not.toBeNull();
    // Not a positive (green) or negative (red) tier colour.
    expect(c).not.toContain("0, 224");   // #00e0.. positive high
    expect(c).not.toContain("255, 51");  // #ff33.. negative high
  });

  it("no_effect stays hidden (null)", () => {
    expect(tintColor(v({ direction: "no_effect", magnitude: 0 }))).toBeNull();
  });

  it("tintColorRGB returns rgb for unscored, null for no_effect", () => {
    expect(tintColorRGB(v({ direction: "unscored", magnitude: 0 }))).not.toBeNull();
    expect(tintColorRGB(v({ direction: "no_effect", magnitude: 0 }))).toBeNull();
  });

  it("buildImpactState includes an unscored node in byNode", () => {
    const g = new Graph() as any;
    g.addNode("u1", { apiNode: { attributes: { type: "Company" } } });
    const resp = { impacts: [v({ node_id: "u1", direction: "unscored", magnitude: 0 })] } as ImpactResponse;
    const state = buildImpactState(g, resp);
    expect(state.byNode.has("u1")).toBe(true);
    expect(state.byNode.get("u1")?.direction).toBe("unscored");
  });

  it("tintColor alpha rises with confidence", () => {
    const low = tintColor(v({ direction: "negative", magnitude: 0.6, hop: 1, confidence: 0.2 }))!;
    const high = tintColor(v({ direction: "negative", magnitude: 0.6, hop: 1, confidence: 0.95 }))!;
    expect(low.startsWith("rgba(")).toBe(true);            // faded
    // crude alpha extraction
    const lowA = parseFloat(low.slice(low.lastIndexOf(",") + 1));
    expect(lowA).toBeLessThan(0.6);
    // high confidence → effectively opaque (rgb or alpha ~1)
    const highOpaque = high.startsWith("rgb(") ||
      parseFloat(high.slice(high.lastIndexOf(",") + 1)) > 0.9;
    expect(highOpaque).toBe(true);
  });

  it("tintColor falls back to legacy is_estimated alpha when confidence absent", () => {
    const c = tintColor(v({ direction: "negative", magnitude: 0.6, hop: 1,
                            is_estimated: true, edge_weight: null }));
    expect(c).not.toBeNull();   // no crash; legacy 0.70 alpha path
  });

  it("tintColorRGB is greyer for low confidence than high", () => {
    const lo = tintColorRGB(v({ direction: "negative", magnitude: 0.6, confidence: 0.1 }))!;
    const hi = tintColorRGB(v({ direction: "negative", magnitude: 0.6, confidence: 0.95 }))!;
    // low-confidence blends toward grey → higher blue channel for the red palette
    expect(lo.b).toBeGreaterThan(hi.b);
  });
});

describe("mixed combined tint is golden (shifted toward yellow, not orange)", () => {
  it("mixed_signals high-magnitude node tints golden amber (green raised vs old #ff9900, blue ~0)", () => {
    const s = tintColorForCombined({ direction: "positive", magnitude: 0.8, mixed_signals: 1 })!;
    const m = s.match(/rgb\((\d+), (\d+), (\d+)\)/)!;
    const [r, g, b] = [Number(m[1]), Number(m[2]), Number(m[3])];
    expect(r).toBeGreaterThanOrEqual(0xe0);   // still red-warm
    expect(g).toBeGreaterThan(0x99);          // strictly more yellow than the old amber (g=153)
    expect(b).toBeLessThanOrEqual(0x10);      // no blue → warm gold, not grey
  });
});
