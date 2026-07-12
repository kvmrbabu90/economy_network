// @vitest-environment jsdom
//
// Regression tests for the "tinted node, no combined-impact panel" bug.
// Root cause: the combined-impact box used to be appended to #inspector-body
// ONLY by an async fetch, AFTER showNode ran. Any later re-render of the inspector
// (hover→showNode, the post-click relayout sliding nodes under the cursor, a tab
// switch) called root().replaceChildren() and WIPED the box. The fix renders the
// box INLINE inside showNode from a per-node cache (via extras.combinedImpact), so
// it is part of the atomic render and survives every re-render.
import { beforeEach, describe, expect, it } from "vitest";
import { showNode, renderCombinedImpactInto } from "../ui/inspector";
import type { ApiNode, NodeImpact } from "../api";
import type { EconGraph } from "../graph";

// showNode only calls g.hasNode(key) (skips the connections block when false).
const gStub = { hasNode: () => false } as unknown as EconGraph;

const node = (key = "cik:0001067491"): ApiNode => ({
  key,
  attributes: {
    label: "Infosys", type: "Company", sector: "Information Technology",
    industry: "IT Consulting & Other Services", country: "IN", tickers: ["INFY"],
    identifiers: { cik: "0001067491" }, aliases: ["Infosys"],
    provisional: false, identity_unverified: false, metadata: {},
  },
});

const imp = (): NodeImpact => ({
  direction: "negative", magnitude: 0.52, mixed_signals: 0, event_count: 19, driver_count: 1,
  computed_at: "2026-07-12T14:32:52+00:00",
  top_events: [{
    event_id: "e1", headline: "Infosys shares fall on batch concerns", direction: "negative",
    magnitude: 0.6, weighted: -0.6, hop: 0, published_at: "2026-07-10",
    url: "https://x/1", source: "Alpha Vantage", gkg_context: null,
  }],
});

beforeEach(() => {
  document.body.innerHTML = '<section id="inspector"><div id="inspector-body"></div></section>';
});

describe("combined-impact box survives inspector re-renders", () => {
  it("showNode renders the box inline when extras.combinedImpact is present", () => {
    showNode(node(), gStub, { combinedImpact: imp() });
    const boxes = document.querySelectorAll(".combined-impact-box");
    expect(boxes.length).toBe(1);
    expect(boxes[0].textContent).toContain("NEGATIVE");
    expect(boxes[0].textContent).toContain("Infosys shares fall on batch concerns");
  });

  it("a re-render (hover / post-expand relayout) keeps EXACTLY ONE box — no wipe, no stacking", () => {
    showNode(node(), gStub, { combinedImpact: imp() });
    // Each of these simulates a subsequent inspector rebuild that, pre-fix, wiped the box.
    showNode(node(), gStub, { combinedImpact: imp() });
    showNode(node(), gStub, { combinedImpact: imp() });
    const boxes = document.querySelectorAll(".combined-impact-box");
    expect(boxes.length).toBe(1);
    expect(boxes[0].textContent).toContain("NEGATIVE");
  });

  it("combinedImpact undefined (not fetched yet) → no box (avoids premature 'No recent impact.')", () => {
    showNode(node(), gStub, {});
    expect(document.querySelector(".combined-impact-box")).toBeNull();
  });

  it("combinedImpact null (fetched, genuinely no impact) → box shows 'No recent impact.'", () => {
    showNode(node(), gStub, { combinedImpact: null });
    const box = document.querySelector(".combined-impact-box");
    expect(box).not.toBeNull();
    expect(box!.textContent).toContain("No recent impact");
  });

  it("wires a 'Sharpen with Claude' button when onSharpen + drivers are present", () => {
    let called = false;
    showNode(node(), gStub, { combinedImpact: imp(), onSharpen: () => { called = true; } });
    const btn = document.querySelector<HTMLButtonElement>(".sharpen-btn");
    expect(btn).not.toBeNull();
    btn!.click();
    expect(called).toBe(true);
  });
});

describe("describe text survives re-renders (fragile-append fix)", () => {
  it("describedText renders inline with NO Describe button", () => {
    showNode(node(), gStub, { describedText: "Infosys is an IT services firm." });
    expect(document.querySelector(".about-box")!.textContent).toContain("Infosys is an IT services firm.");
    expect(document.querySelector(".describe-btn")).toBeNull();
  });

  it("a hover re-render keeps EXACTLY ONE description, not a reverted button", () => {
    showNode(node(), gStub, { describedText: "cached description" });
    showNode(node(), gStub, { describedText: "cached description" });   // hover re-render
    expect(document.querySelectorAll(".about-body").length).toBe(1);
    expect(document.querySelector(".about-body")!.textContent).toBe("cached description");
    expect(document.querySelector(".describe-btn")).toBeNull();
  });

  it("shows the Describe button when only onDescribe is provided (no cached text)", () => {
    showNode(node(), gStub, { onDescribe: async () => "x" });
    expect(document.querySelector(".describe-btn")).not.toBeNull();
  });
});

describe("tint/panel floor + compact live-synth", () => {
  it("no_effect impact → 'No recent impact.' (mirrors the map's untinted floor)", () => {
    const root = document.getElementById("inspector-body")!;
    renderCombinedImpactInto(root, { ...imp(), direction: "no_effect", magnitude: 0 });
    expect(root.querySelector(".combined-impact-box")!.textContent).toContain("No recent impact");
  });

  it("sub-floor magnitude (<= 0.05) → 'No recent impact.', never a populated box", () => {
    const root = document.getElementById("inspector-body")!;
    renderCombinedImpactInto(root, { ...imp(), magnitude: 0.03 });
    const box = root.querySelector(".combined-impact-box")!;
    expect(box.textContent).toContain("No recent impact");
    expect(box.querySelector(".combined-timeline")).toBeNull();
  });

  it("compact live-synth (empty top_events + computed_at) → header only, no timeline/freshness/sharpen", () => {
    const root = document.getElementById("inspector-body")!;
    renderCombinedImpactInto(root, {
      direction: "negative", magnitude: 0.52, mixed_signals: 0, event_count: 19, driver_count: 0,
      computed_at: "", top_events: [],
    }, { onSharpen: () => {} });
    const box = root.querySelector(".combined-impact-box")!;
    expect(box.textContent).toContain("NEGATIVE");
    expect(box.textContent).toContain("19 events");                 // driver_count 0 → falls back to event count
    expect(box.textContent).not.toContain("as of");                 // empty computed_at → no freshness line
    expect(box.querySelector(".combined-timeline")).toBeNull();     // no drivers in the live map
    expect(box.querySelector(".sharpen-btn")).toBeNull();           // no sharpen without drivers
  });
});

describe("panel header: driver_count vs event_count", () => {
  it("shows driver_count as the primary figure with scanned event_count as secondary", () => {
    const root = document.getElementById("inspector-body")!;
    renderCombinedImpactInto(root, { ...imp(), driver_count: 3, event_count: 30 });
    const box = root.querySelector(".combined-impact-box")!;
    expect(box.textContent).toContain("3 drivers");
    expect(box.textContent).toContain("scanned 30");
  });

  it("falls back to 'N events' when driver_count is 0 (live-map synth)", () => {
    const root = document.getElementById("inspector-body")!;
    renderCombinedImpactInto(root, { ...imp(), driver_count: 0, event_count: 19 });
    const box = root.querySelector(".combined-impact-box")!;
    expect(box.textContent).toContain("19 events");
    expect(box.textContent).not.toContain("driver");
  });

  it("uses singular '1 driver' and omits 'scanned' when every scanned row was a driver", () => {
    const root = document.getElementById("inspector-body")!;
    renderCombinedImpactInto(root, { ...imp(), driver_count: 1, event_count: 1 });
    const box = root.querySelector(".combined-impact-box")!;
    expect(box.textContent).toContain("1 driver");
    expect(box.textContent).not.toContain("1 drivers");
    expect(box.textContent).not.toContain("scanned");
  });
});

describe("renderCombinedImpactInto", () => {
  it("is idempotent — repeated paints never stack", () => {
    const root = document.getElementById("inspector-body")!;
    renderCombinedImpactInto(root, imp());
    renderCombinedImpactInto(root, imp());
    expect(root.querySelectorAll(".combined-impact-box").length).toBe(1);
  });

  it("null impact → 'No recent impact.'", () => {
    const root = document.getElementById("inspector-body")!;
    renderCombinedImpactInto(root, null);
    expect(root.querySelector(".combined-impact-box")!.textContent).toContain("No recent impact");
  });
});
