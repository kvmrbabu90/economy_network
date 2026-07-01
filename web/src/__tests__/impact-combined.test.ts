import { describe, expect, it } from "vitest";
import { tintColorForCombined, buildLiveImpactMap } from "../impact";
import type { LiveImpact } from "../api";

const row = (p: Partial<LiveImpact>): LiveImpact =>
  ({ node_id: "x", direction: "positive", magnitude: 0.8, mixed_signals: 0, event_count: 1, ...p });

describe("tintColorForCombined", () => {
  it("high positive → a green rgb() string", () => {
    const c = tintColorForCombined(row({ direction: "positive", magnitude: 0.9 }))!;
    expect(c.startsWith("rgb(")).toBe(true);
  });
  it("mixed_signals selects amber even when net positive", () => {
    const mixed = tintColorForCombined(row({ direction: "positive", magnitude: 0.9, mixed_signals: 1 }));
    const plain = tintColorForCombined(row({ direction: "positive", magnitude: 0.9, mixed_signals: 0 }));
    expect(mixed).not.toBe(plain);
  });
  it("no_effect or ~0 magnitude → null (no tint)", () => {
    expect(tintColorForCombined(row({ direction: "no_effect", magnitude: 0 }))).toBeNull();
    expect(tintColorForCombined(row({ direction: "positive", magnitude: 0.02 }))).toBeNull();
  });
});

describe("buildLiveImpactMap", () => {
  it("keys rows by node_id; empty → empty", () => {
    const m = buildLiveImpactMap([row({ node_id: "a" }), row({ node_id: "b" })]);
    expect(m.size).toBe(2);
    expect(m.get("a")!.node_id).toBe("a");
    expect(buildLiveImpactMap([]).size).toBe(0);
  });
});
