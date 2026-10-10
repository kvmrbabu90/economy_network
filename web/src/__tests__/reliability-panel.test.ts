// @vitest-environment jsdom
//
// Renderer tests for the Reliability tab: heatmap cells, calibration bars,
// coverage, the all/clean toggle, and the not-computed empty state.
import { describe, expect, it } from "vitest";
import { renderReliability } from "../ui/reliabilityPanel";
import type { ReliabilitySummary, RelStat } from "../api";

const stat = (n: number, hit: number, ic: number | null = 0.2): RelStat => ({
  n, hits: Math.round(hit * n), hit_rate: hit, ci_low: Math.max(0, hit - 0.15),
  ci_high: Math.min(1, hit + 0.15), mean_abn: 0.001, median_abn: 0.001, ic,
});

const scorecard = (): ReliabilitySummary => ({
  status: "ok",
  generated_at: "2026-10-10T00:00:00+00:00",
  meta: { horizons: [1], mag_bands: { low: "<0.05", med: "0.05-0.15", high: ">=0.15" }, z: 1.96, ic_min_n: 5, benchmark: "sector-ETF excess" },
  overall: { "1": stat(76, 0.62) },
  by_hop_horizon: [
    { hop: 1, horizon_days: 1, all: stat(40, 0.575, 0.35), clean: stat(30, 0.6, 0.3) },
    { hop: 2, horizon_days: 1, all: stat(34, 0.68, -0.1), clean: stat(20, 0.7, -0.05) },
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
  it("renders overall cards, a heatmap, calibration bars, and coverage", () => {
    const el = document.createElement("div");
    renderReliability(el, scorecard(), { clean: false });
    // overall: 4 canonical horizon cards (1 real + 3 pending)
    expect(el.querySelectorAll(".rel-ov-card").length).toBe(4);
    expect(el.querySelectorAll(".rel-ov-pending").length).toBe(3);
    expect(el.textContent).toContain("62%");
    // two heatmaps (hit-rate + IC), each with 3 hop rows x 4 horizon cols = 12 cells
    expect(el.querySelectorAll(".rel-grid").length).toBe(2);
    // calibration: 3 bands
    expect(el.querySelectorAll(".rel-bar-row").length).toBe(3);
    // coverage numbers present
    expect(el.textContent).toContain("169");
    expect(el.textContent).toContain("sector-ETF excess");
  });

  it("flags low-n cells (n<10) as low-power, not hidden", () => {
    const el = document.createElement("div");
    renderReliability(el, scorecard(), { clean: false });
    // the low mag-band has n=5 -> low-power class on its bar fill
    expect(el.querySelector(".rel-bar-fill.rel-lown")).not.toBeNull();
  });

  it("clean toggle reflects the opts and future horizons render as pending", () => {
    const el = document.createElement("div");
    renderReliability(el, scorecard(), { clean: true });
    const active = el.querySelector(".rel-clean-btn.is-active") as HTMLElement;
    expect(active.dataset.clean).toBe("1");
    // +5d/+20d/+60d have no data -> empty cells exist in the hit heatmap
    expect(el.querySelectorAll(".rel-cell-empty").length).toBeGreaterThan(0);
  });

  it("shows an early-data banner while sparse", () => {
    const el = document.createElement("div");
    renderReliability(el, scorecard(), { clean: false });
    expect(el.querySelector(".rel-banner")).not.toBeNull();
  });

  it("renders a friendly empty state when not computed", () => {
    const el = document.createElement("div");
    renderReliability(el, { status: "not_computed", message: "No scorecard yet.",
      overall: {}, by_hop_horizon: [], by_magband: [], classes: [],
      coverage: { directional_signals: 0, signals_with_mature_outcome: 0, outcome_rows: 0,
        mature_by_horizon: {}, distinct_events: 0, distinct_nodes: 0 } }, { clean: false });
    expect(el.querySelector(".rel-empty")).not.toBeNull();
    expect(el.textContent).toContain("No scorecard yet.");
    expect(el.querySelectorAll(".rel-grid").length).toBe(0);
  });
});
