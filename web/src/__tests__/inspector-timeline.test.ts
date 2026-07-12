import { describe, expect, it } from "vitest";
import { buildTimelineRows } from "../ui/inspector";
import type { NodeImpact } from "../api";

const imp = (top: NodeImpact["top_events"]): NodeImpact =>
  ({ direction: "negative", magnitude: 0.6, mixed_signals: 0, event_count: top.length,
     driver_count: top.length, computed_at: "2026-06-30T00:00:00", top_events: top });

describe("buildTimelineRows", () => {
  it("linkable when url present; plain when null; order preserved", () => {
    const rows = buildTimelineRows(imp([
      { event_id: "a", headline: "First", direction: "negative", magnitude: 0.7, weighted: -0.7, hop: 1,
        published_at: "2026-06-29", url: "https://x/a", source: "SEC 8-K", gkg_context: null },
      { event_id: "b", headline: "Second", direction: "positive", magnitude: 0.2, weighted: 0.2, hop: 2,
        published_at: "2026-06-20", url: null, source: null, gkg_context: null },
    ]));
    expect(rows.map(r => r.headline)).toEqual(["First", "Second"]);
    expect(rows[0].linkUrl).toBe("https://x/a");
    expect(rows[0].sourceLabel).toBe("SEC 8-K");
    expect(rows[1].linkUrl).toBeNull();
  });
  it("empty top_events → empty rows", () => {
    expect(buildTimelineRows(imp([])).length).toBe(0);
  });
  it("rejects non-http(s) urls (no javascript:/data: href — XSS guard)", () => {
    const mk = (url: string) => imp([{ event_id: "x", headline: "H", direction: "negative",
      magnitude: 0.5, weighted: -0.5, hop: 1, published_at: null, url, source: "Feed", gkg_context: null }]);
    expect(buildTimelineRows(mk("javascript:alert(1)"))[0].linkUrl).toBeNull();
    expect(buildTimelineRows(mk("data:text/html,<script>1</script>"))[0].linkUrl).toBeNull();
    expect(buildTimelineRows(mk("not a url"))[0].linkUrl).toBeNull();
    expect(buildTimelineRows(mk("https://ok/1"))[0].linkUrl).toBe("https://ok/1");   // http(s) preserved
    expect(buildTimelineRows(mk("http://ok/2"))[0].linkUrl).toBe("http://ok/2");
  });
});
