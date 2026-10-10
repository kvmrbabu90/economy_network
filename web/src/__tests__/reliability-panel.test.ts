// @vitest-environment jsdom
//
// Renderer tests for the Reliability tab: retail framing, metric toggle (net/gross/dir-acc),
// heatmap cells, calibration bars, coverage, clean toggle, and the not-computed empty state.
import { describe, expect, it } from "vitest";
import { renderReliability } from "../ui/reliabilityPanel";
import type { ReliabilitySummary, RelStat } from "../api";

const stat = (n: number, hit: number, ic: number | null = 0.2, net = hit): RelStat => ({
  n, hits: Math.round(hit * n), hit_rate: hit, ci_low: Math.max(0, hit - 0.15),
  ci_high: Math.min(1, hit + 0.15), mean_abn: 0.001, median_abn: 0.001, ic,
  net_hit_rate: net, net_ci_low: Math.max(0, net - 0.15), net_ci_high: Math.min(1, net + 0.15),
  mean_net: 0.0005, dir_acc: 0.71, mean_gap: 0.002,
});

const scorecard = (): ReliabilitySummary => ({
  status: "ok",
  generated_at: "2026-10-10T00:00:00+00:00",
  meta: { horizons: [1], mag_bands: { low: "<0.05", med: "0.05-0.15", high: ">=0.15" }, z: 1.96,
          ic_min_n: 5, benchmark: "sector-ETF excess", entry: "next open", cost_bps: 10 },
  overall: { "1": stat(76, 0.62, 0.2, 0.58) },
  by_hop_horizon: [
    { hop: 1, horizon_days: 1, all: stat(40, 0.575, 0.35, 0.54), clean: stat(30, 0.6, 0.3, 0.56) },
    { hop: 2, horizon_days: 1, all: stat(34, 0.68, -0.1, 0.64), clean: stat(20, 0.7, -0.05, 0.66) },
  ],
  by_magband: [
    { mag_band: "low", horizon_days: 1, ...stat(5, 1.0) },
    { mag_band: "med", horizon_days: 1, ...stat(19, 0.37) },
    { mag_band: "high", horizon_days: 1, ...stat(52, 0.67) },
  ],
  coverage: {
    directional_signals: 169, signals_with_mature_outcome: 76, outcome_rows: 76,
    mature_by_horizon: { "1": 76 }, distinct_events: 3, distinct_nodes: 75,
  },
  classes: [],
});

describe("renderReliability", () => {
  it("leads with the NET edge, a retail framing note, heatmaps, calibration, coverage", () => {
    const el = document.createElement("div");
    renderReliability(el, scorecard(), { clean: false, metric: "net" });
    expect(el.querySelector(".rel-framing")).not.toBeNull();         // the retail explainer
    expect(el.querySelectorAll(".rel-ov-card").length).toBe(4);      // 1 real + 3 pending
    expect(el.querySelectorAll(".rel-ov-pending").length).toBe(3);
    expect(el.textContent).toContain("58%");                         // overall shows NET (0.58), not gross
    expect(el.textContent).toContain("gross 62%");                   // gross shown beneath
    expect(el.querySelectorAll(".rel-grid").length).toBe(2);         // hit + IC heatmaps
    expect(el.querySelectorAll(".rel-bar-row").length).toBe(3);      // calibration bands
    expect(el.textContent).toContain("169");
    expect(el.textContent).toContain("10bps");                       // cost surfaced
  });

  it("renders the metric toggle with net active by default", () => {
    const el = document.createElement("div");
    renderReliability(el, scorecard(), { clean: false, metric: "net" });
    expect(el.querySelectorAll(".rel-metric-btn").length).toBe(3);
    const active = el.querySelector(".rel-metric-btn.is-active") as HTMLElement;
    expect(active.dataset.metric).toBe("net");
  });

  it("switches the heatmap metric to direction accuracy", () => {
    const el = document.createElement("div");
    renderReliability(el, scorecard(), { clean: false, metric: "diracc" });
    const active = el.querySelector(".rel-metric-btn.is-active") as HTMLElement;
    expect(active.dataset.metric).toBe("diracc");
    // hop-1 +1d cell now shows dir_acc (0.71 -> "71%")
    expect(el.textContent).toContain("71%");
  });

  it("flags low-n cells (n<10) as low-power, not hidden", () => {
    const el = document.createElement("div");
    renderReliability(el, scorecard(), { clean: false, metric: "net" });
    expect(el.querySelector(".rel-bar-fill.rel-lown")).not.toBeNull();
  });

  it("clean toggle reflects the opts and future horizons render as pending", () => {
    const el = document.createElement("div");
    renderReliability(el, scorecard(), { clean: true, metric: "net" });
    const active = el.querySelector(".rel-clean-btn.is-active") as HTMLElement;
    expect(active.dataset.clean).toBe("1");
    expect(el.querySelectorAll(".rel-cell-empty").length).toBeGreaterThan(0);
  });

  it("shows an early-data banner while sparse", () => {
    const el = document.createElement("div");
    renderReliability(el, scorecard(), { clean: false, metric: "net" });
    expect(el.querySelector(".rel-banner")).not.toBeNull();
  });

  it("renders a friendly empty state when not computed", () => {
    const el = document.createElement("div");
    renderReliability(el, { status: "not_computed", message: "No scorecard yet.",
      overall: {}, by_hop_horizon: [], by_magband: [], classes: [],
      coverage: { directional_signals: 0, signals_with_mature_outcome: 0, outcome_rows: 0,
        mature_by_horizon: {}, distinct_events: 0, distinct_nodes: 0 } }, { clean: false, metric: "net" });
    expect(el.querySelector(".rel-empty")).not.toBeNull();
    expect(el.textContent).toContain("No scorecard yet.");
    expect(el.querySelectorAll(".rel-grid").length).toBe(0);
  });
});
