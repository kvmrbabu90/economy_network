import Graph from "graphology";
import { describe, expect, it } from "vitest";
import { tintColor, tintColorRGB, buildImpactState } from "../impact";
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
});
