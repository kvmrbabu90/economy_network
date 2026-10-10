// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { renderDashboard } from "../ui/scorecard";
import type { ReliabilitySummary, RelStat } from "../api";

const stat = (n: number, hit: number, ic: number | null = 0.2, net = hit): RelStat => ({
  n, hits: Math.round(hit * n), hit_rate: hit, ci_low: Math.max(0, hit - 0.15), ci_high: Math.min(1, hit + 0.15),
  mean_abn: 0.001, median_abn: 0.001, ic,
  net_hit_rate: net, net_ci_low: Math.max(0, net - 0.15), net_ci_high: Math.min(1, net + 0.15),
  mean_net: 0.0005, dir_acc: 0.71, mean_gap: 0.002,
});

const sc = (withIntraday = true): ReliabilitySummary => ({
  status: "ok", generated_at: "2026-10-10T00:00:00+00:00",
  meta: { horizons: [1, 5, 20, 60], mag_bands: { low: "<0.05", med: "0.05-0.15", high: ">=0.15" }, z: 1.96, ic_min_n: 5, benchmark: "sector-ETF excess", entry: "next open", cost_bps: 10 },
  overall: { "1": stat(76, 0.62, 0.2, 0.58) },
  by_hop_horizon: [{ hop: 2, horizon_days: 1, all: stat(34, 0.68, -0.1, 0.64), clean: stat(20, 0.7, -0.05, 0.66) }],
  by_magband: [{ mag_band: "high", horizon_days: 1, ...stat(52, 0.67) }],
  coverage: { directional_signals: 169, signals_with_mature_outcome: 76, outcome_rows: 76, mature_by_horizon: { "1": 76 }, distinct_events: 3, distinct_nodes: 75 },
  intraday: withIntraday ? {
    horizons: [0, 1, 5],
    overall: { "0": stat(12, 0.66, null, 0.6), "1": stat(10, 0.7, null, 0.64) },
    by_hop_horizon: [{ hop: 2, horizon_days: 0, all: stat(8, 0.75, null, 0.7), clean: stat(6, 0.8, null, 0.72) }],
    coverage: { outcome_rows: 22, distinct_events: 4, distinct_nodes: 9, latency_min: 60, mature_by_horizon: { "0": 12, "1": 10 } },
  } : null,
});

describe("renderDashboard", () => {
  it("renders the daily model: KPI cards, heatmaps (hit + IC), calibration, coverage", () => {
    const el = document.createElement("div");
    renderDashboard(el, sc(), { model: "daily", metric: "net", clean: false });
    expect(el.querySelector(".rel-framing")).not.toBeNull();
    expect(el.querySelectorAll(".rel-ov-card").length).toBe(4);     // 4 daily horizons
    expect(el.querySelectorAll(".rel-grid").length).toBe(2);        // hit + IC
    expect(el.querySelectorAll(".rel-bar-row").length).toBe(3);     // calibration
    expect(el.textContent).toContain("58%");                        // overall NET
    expect(el.textContent).toContain("10bps");
    // daily model offers the dir-acc metric
    expect([...el.querySelectorAll(".rel-btn")].some((b) => (b as HTMLElement).dataset.metric === "diracc")).toBe(true);
  });

  it("switches to the hourly model: intraday cards/heatmap, no IC/calibration, no dir-acc toggle", () => {
    const el = document.createElement("div");
    renderDashboard(el, sc(), { model: "hourly", metric: "net", clean: false });
    const activeModel = el.querySelector(".rel-btn.is-active[data-model]") as HTMLElement;
    expect(activeModel.dataset.model).toBe("hourly");
    expect(el.querySelectorAll(".rel-ov-card").length).toBe(3);     // 3 intraday horizons (0,1,5)
    expect(el.querySelectorAll(".rel-grid").length).toBe(1);        // only the hit heatmap (no IC)
    expect(el.querySelectorAll(".rel-bar-row").length).toBe(0);     // no calibration in hourly
    expect([...el.querySelectorAll(".rel-btn")].some((b) => (b as HTMLElement).dataset.metric === "diracc")).toBe(false);
    expect(el.textContent).toContain("60min");                      // latency surfaced
  });

  it("shows an accumulating note for hourly when there is no intraday data", () => {
    const el = document.createElement("div");
    renderDashboard(el, sc(false), { model: "hourly", metric: "net", clean: false });
    expect(el.textContent).toContain("No hourly-entry outcomes yet");
    expect(el.querySelectorAll(".rel-grid").length).toBe(0);
  });

  it("renders the not-computed empty state", () => {
    const el = document.createElement("div");
    renderDashboard(el, { status: "not_computed", message: "No scorecard yet.", overall: {}, by_hop_horizon: [], by_magband: [], coverage: { directional_signals: 0, signals_with_mature_outcome: 0, outcome_rows: 0, mature_by_horizon: {}, distinct_events: 0, distinct_nodes: 0 } }, { model: "daily", metric: "net", clean: false });
    expect(el.textContent).toContain("No scorecard yet.");
    expect(el.querySelectorAll(".rel-grid").length).toBe(0);
  });
});
