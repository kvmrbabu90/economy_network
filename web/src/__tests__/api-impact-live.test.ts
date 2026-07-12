import { afterEach, describe, expect, it, vi } from "vitest";
import { getImpactLive, getNodeImpact } from "../api";

function mockFetch(body: unknown) {
  const f = vi.fn().mockResolvedValue({
    ok: true, status: 200, statusText: "OK",
    json: async () => body, text: async () => JSON.stringify(body),
  } as unknown as Response);
  vi.stubGlobal("fetch", f);
  return f;
}
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("impact-live client", () => {
  it("getImpactLive hits /impact/live and parses", async () => {
    const f = mockFetch({ computed_at: "2026-06-30T00:00:00", count: 1,
      impacts: [{ node_id: "cik:1", direction: "negative", magnitude: 0.6, mixed_signals: 0, event_count: 2 }] });
    const r = await getImpactLive();
    expect(f.mock.calls[0][0]).toContain("/impact/live");
    expect(r.count).toBe(1);
    expect(r.impacts[0].node_id).toBe("cik:1");
  });

  it("getNodeImpact hits /node/{id}/impact and parses top_events", async () => {
    const f = mockFetch({ node_id: "cik:1", name: "Apple", type: "Company",
      impact: { direction: "negative", magnitude: 0.6, mixed_signals: 0, event_count: 1, driver_count: 1, computed_at: "2026-06-30T00:00:00",
        top_events: [{ event_id: "e1", headline: "H", direction: "negative", magnitude: 0.7, weighted: -0.7,
                       hop: 1, published_at: "2026-06-29", url: "https://x/e1", source: "SEC 8-K" }] } });
    const r = await getNodeImpact("cik:1");
    expect(f.mock.calls[0][0]).toContain("/node/cik:1/impact");
    expect(r.impact!.top_events[0].source).toBe("SEC 8-K");
  });

  it("getNodeImpact tolerates impact:null", async () => {
    mockFetch({ node_id: "slug:oil", name: "Crude Oil", type: "Commodity", impact: null });
    const r = await getNodeImpact("slug:oil");
    expect(r.impact).toBeNull();
  });
});
