import { describe, expect, it } from "vitest";
import { layoutBars } from "../ui/usageChart";
import type { UsageBucket } from "../api";

const bk = (i: number, o: number, c: number, cost: number): UsageBucket =>
  ({ bucket: "2026-07-07", input_tokens: i, output_tokens: o, cache_read_tokens: c, cost_usd: cost, calls: 1 });

describe("layoutBars", () => {
  it("produces one bar per bucket", () => {
    const g = layoutBars([bk(10, 5, 1, 0.1), bk(20, 10, 2, 0.2)], 320, 200);
    expect(g.bars.length).toBe(2);
    expect(g.costPoints.length).toBe(2);
  });

  it("stacked segment heights sum to the full plot height at the max bucket", () => {
    const g = layoutBars([bk(100, 0, 0, 0), bk(50, 30, 20, 0)], 320, 200);   // both total 100 = max
    const b0 = g.bars[0];
    expect(Math.round(b0.input.h)).toBe(Math.round(g.plot.h));               // all-input bar fills
    const b1 = g.bars[1];
    expect(Math.round(b1.input.h + b1.output.h + b1.cache.h)).toBe(Math.round(g.plot.h));
  });

  it("stacks input at the bottom, then output, then cache (ascending y)", () => {
    const g = layoutBars([bk(40, 30, 20, 0)], 320, 200);
    const b = g.bars[0];
    expect(b.cache.y).toBeLessThan(b.output.y);   // cache is highest (smallest y)
    expect(b.output.y).toBeLessThan(b.input.y);   // output above input
  });

  it("handles empty input", () => {
    const g = layoutBars([], 320, 200);
    expect(g.bars.length).toBe(0);
    expect(g.maxTokens).toBe(1);   // guarded against divide-by-zero
  });
});
