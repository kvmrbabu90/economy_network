// Standalone Signals dashboard renderer. Two entry MODELS share one layout:
//   daily  — enter at the next session open after the announcement (what So What's tab showed),
//   hourly — enter at the next hourly bar after (news time + processing latency); +0 = same-session close.
// For each: order x horizon heatmap of the selected metric (net-of-cost / gross / direction accuracy),
// net-first KPI cards, and coverage. n + Wilson CI everywhere; low-n cells dimmed, not hidden.
import type { ReliabilitySummary, RelStat, RelHopHorizon } from "../api";

export type RelModel = "daily" | "hourly";
export type RelMetric = "net" | "gross" | "diracc";

const HOPS = [0, 1, 2];
const HOP_LABEL: Record<number, string> = { 0: "1st · direct", 1: "2nd · supplier/peer", 2: "3rd+" };
const METRIC_LABEL: Record<RelMetric, string> = { net: "net edge", gross: "gross hit", diracc: "direction acc" };
const LOW_N = 10;

const pct = (x: number) => `${(x * 100).toFixed(0)}%`;
const pct1 = (x: number) => `${(x * 100).toFixed(1)}%`;
const hlabel = (h: number) => (h === 0 ? "close" : `+${h}d`);

function mix(a: number[], b: number[], t: number): string {
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}
const GRAY = [58, 63, 68], TEAL = [45, 212, 191], CORAL = [244, 114, 128];
function hitColor(hr: number): string {
  const d = Math.max(-0.25, Math.min(0.25, hr - 0.5)) / 0.25;
  return d >= 0 ? mix(GRAY, TEAL, d) : mix(GRAY, CORAL, -d);
}
function icColor(ic: number): string {
  const d = Math.max(-0.4, Math.min(0.4, ic)) / 0.4;
  return d >= 0 ? mix(GRAY, TEAL, d) : mix(GRAY, CORAL, -d);
}

function hasNet(s: RelStat | null | undefined): boolean {
  return !!s && s.net_hit_rate !== undefined && s.net_hit_rate !== null;
}
function rateOf(s: RelStat, m: RelMetric): { rate: number | null; lo?: number; hi?: number } {
  if (m === "net") return { rate: s.net_hit_rate ?? null, lo: s.net_ci_low, hi: s.net_ci_high };
  if (m === "diracc") return { rate: s.dir_acc ?? null };
  return { rate: s.hit_rate, lo: s.ci_low, hi: s.ci_high };
}
function statFor(cells: RelHopHorizon[], hop: number, horizon: number, clean: boolean): RelStat | null {
  const cell = cells.find((c) => c.hop === hop && c.horizon_days === horizon);
  if (!cell) return null;
  return clean ? cell.clean : cell.all;
}

function heatCell(stat: RelStat | null, kind: "hit" | "ic", metric: RelMetric): string {
  if (!stat || stat.n === 0) return `<div class="rel-cell rel-cell-empty" title="no mature signals yet">·</div>`;
  const lowN = stat.n < LOW_N;
  if (kind === "ic") {
    if (stat.ic === null) return `<div class="rel-cell rel-cell-empty" title="n=${stat.n}, too few for IC">n=${stat.n}</div>`;
    return `<div class="rel-cell${lowN ? " rel-lown" : ""}" style="background:${icColor(stat.ic)}" title="IC ${stat.ic.toFixed(2)} · n=${stat.n}">${stat.ic.toFixed(2)}<span class="rel-cell-n">n=${stat.n}</span></div>`;
  }
  const { rate, lo, hi } = rateOf(stat, metric);
  if (rate === null) return `<div class="rel-cell rel-cell-empty" title="n=${stat.n}, no ${METRIC_LABEL[metric]}">n=${stat.n}</div>`;
  const ci = lo !== undefined && hi !== undefined ? ` · 95% CI ${pct(lo)}–${pct(hi)}` : "";
  return `<div class="rel-cell${lowN ? " rel-lown" : ""}" style="background:${hitColor(rate)}" title="${METRIC_LABEL[metric]} ${pct1(rate)}${ci} · n=${stat.n}">${pct(rate)}<span class="rel-cell-n">n=${stat.n}</span></div>`;
}

function heatmap(cells: RelHopHorizon[], horizons: number[], kind: "hit" | "ic", clean: boolean, metric: RelMetric): string {
  const head = `<div class="rel-grid-head"></div>` + horizons.map((h) => `<div class="rel-grid-head">${hlabel(h)}</div>`).join("");
  const rows = HOPS.map((hop) => {
    const label = `<div class="rel-row-label">${HOP_LABEL[hop] ?? `hop ${hop}`}</div>`;
    return label + horizons.map((h) => heatCell(statFor(cells, hop, h, clean), kind, metric)).join("");
  }).join("");
  return `<div class="rel-grid" style="grid-template-columns: 1.6fr repeat(${horizons.length}, 1fr)">${head}${rows}</div>`;
}

function overallCards(overall: Record<string, RelStat>, horizons: number[]): string {
  const cards = horizons.map((h) => {
    const s = overall[String(h)];
    if (!s) return `<div class="rel-ov-card rel-ov-pending"><div class="rel-ov-h">${hlabel(h)}</div><div class="rel-ov-pct">pending</div></div>`;
    const net = hasNet(s);
    const lead = net ? (s.net_hit_rate as number) : s.hit_rate;
    const lo = net ? (s.net_ci_low ?? s.ci_low) : s.ci_low, hi = net ? (s.net_ci_high ?? s.ci_high) : s.ci_high;
    return `<div class="rel-ov-card"><div class="rel-ov-h">${hlabel(h)} ${net ? "net" : "hit"}</div>`
      + `<div class="rel-ov-pct" style="color:${hitColor(lead)}">${pct(lead)}</div>`
      + `<div class="rel-ov-ci">CI ${pct(lo)}–${pct(hi)}</div>`
      + `<div class="rel-ov-n">${net ? `gross ${pct(s.hit_rate)} · ` : ""}n=${s.n}</div></div>`;
  }).join("");
  return `<div class="rel-overall">${cards}</div>`;
}

function calibration(sc: ReliabilitySummary): string {
  const horizons = [...new Set(sc.by_magband.map((b) => b.horizon_days))].sort((a, b) => a - b);
  if (!horizons.length) return `<p class="rel-sub">No calibration data yet.</p>`;
  const h = horizons[0];
  const rows = ["low", "med", "high"].map((band) => {
    const b = sc.by_magband.find((x) => x.mag_band === band && x.horizon_days === h);
    if (!b) return `<div class="rel-bar-row"><span class="rel-bar-lab">${band}</span><div class="rel-bar-track"></div><span class="rel-bar-val">—</span></div>`;
    const lowN = b.n < LOW_N;
    return `<div class="rel-bar-row"><span class="rel-bar-lab">${band}</span>`
      + `<div class="rel-bar-track"><div class="rel-bar-fill${lowN ? " rel-lown" : ""}" style="width:${pct(b.hit_rate)};background:${hitColor(b.hit_rate)}"></div><span class="rel-bar-coin"></span></div>`
      + `<span class="rel-bar-val">${pct(b.hit_rate)} <em>n=${b.n}</em></span></div>`;
  }).join("");
  const mb = sc.meta?.mag_bands;
  return `<div class="rel-bars">${rows}</div><div class="rel-sub">at +${h}d · dashed line = 50% coin-flip`
    + (mb ? ` · low ${mb.low} · med ${mb.med} · high ${mb.high}` : "") + `</div>`;
}

export interface RelViewOpts { model: RelModel; metric: RelMetric; clean: boolean }

export function renderDashboard(el: HTMLElement, sc: ReliabilitySummary, opts: RelViewOpts): void {
  if (sc.status === "not_computed") {
    el.innerHTML = `<div class="rel-card"><p class="rel-sub">${sc.message ?? "No scorecard yet."} It is produced by <code>pipeline/run_validation.py</code> (no LLM) and matures as prices accrue.</p></div>`;
    return;
  }
  const cost = sc.meta?.cost_bps ?? 10;
  const intraday = sc.intraday ?? null;
  const metricBtns: [RelMetric, string][] = opts.model === "hourly"
    ? [["net", "net"], ["gross", "gross"]] : [["net", "net"], ["gross", "gross"], ["diracc", "dir acc"]];

  // Resolve the selected model's source.
  const src = opts.model === "hourly" && intraday
    ? { overall: intraday.overall, cells: intraday.by_hop_horizon, horizons: intraday.horizons }
    : { overall: sc.overall, cells: sc.by_hop_horizon, horizons: sc.meta?.horizons ?? [1, 5, 20, 60] };

  const netReady = Object.values(src.overall).some(hasNet) || src.cells.some((c) => hasNet(c.all));
  let metric: RelMetric = (!netReady && opts.metric === "net") ? "gross" : opts.metric;
  // The hourly model has no announcement-day direction-accuracy — coerce it to net so the heatmap
  // isn't blank and a metric button stays active when switching models with "dir acc" selected.
  if (opts.model === "hourly" && metric === "diracc") metric = "net";

  const header = `<header class="rel-head">
      <div class="rel-title">EconGraph <b>Signals</b></div>
      <div class="rel-sub2">Does each propagation signal pay off in the market — for a retail trader?</div>
    </header>`;

  const framing = `<div class="rel-framing">
      <b>1st-order</b> news is priced in before you can act — judge it by <b>direction accuracy</b>.
      The edge lives in the slow <b>2nd/3rd-order</b> drift. <b>Net edge</b> = % profitable after a ${cost}bps round-trip.
      <b>Daily</b> enters at the next session open; <b>Hourly</b> enters at the next bar after the news
      ${intraday?.coverage.latency_min != null ? `(+${intraday.coverage.latency_min}min modeled latency)` : ""} — what the live cycle could actually get.
    </div>`;

  const toggles = `<div class="rel-controls">
      <div class="rel-seg" data-group="model">
        <button class="rel-btn${opts.model === "daily" ? " is-active" : ""}" data-model="daily">Daily entry</button>
        <button class="rel-btn${opts.model === "hourly" ? " is-active" : ""}" data-model="hourly">Hourly entry</button>
      </div>
      <div class="rel-seg" data-group="metric">${metricBtns.map(([m, l]) => `<button class="rel-btn${metric === m ? " is-active" : ""}" data-metric="${m}">${l}</button>`).join("")}</div>
      <div class="rel-seg" data-group="clean">
        <button class="rel-btn${!opts.clean ? " is-active" : ""}" data-clean="0">all</button>
        <button class="rel-btn${opts.clean ? " is-active" : ""}" data-clean="1">clean</button>
      </div>
    </div>`;

  const hourlyEmpty = opts.model === "hourly" && (!intraday || intraday.coverage.outcome_rows === 0);
  const body = hourlyEmpty
    ? `<div class="rel-card"><p class="rel-sub">No hourly-entry outcomes yet. These need events ingested with a precise news timestamp (GKG), then a validation run — the scorecard fills in as the live cycle catches news in real time.</p></div>`
    : overallCards(src.overall, src.horizons)
      + `<div class="rel-card"><div class="rel-card-title">${opts.model === "hourly" ? "Hourly-entry" : "Daily-entry"} ${METRIC_LABEL[metric]} · order × horizon</div>`
      + heatmap(src.cells, src.horizons, "hit", opts.clean, metric)
      + `<div class="rel-legend"><span>worse</span><i class="rel-leg-grad"></i><span>better</span><em>${pct(0.5)} = ${metric === "net" ? "break-even after cost" : "coin-flip"}</em></div></div>`
      + (opts.model === "daily"
        ? `<div class="rel-two"><div class="rel-card"><div class="rel-card-title">Magnitude information coefficient</div>`
          + heatmap(src.cells, src.horizons, "ic", opts.clean, metric)
          + `<div class="rel-sub">Spearman(predicted magnitude, favorable capturable return).</div></div>`
          + `<div class="rel-card"><div class="rel-card-title">Confidence calibration</div>${calibration(sc)}</div></div>`
        : "");

  const cov = opts.model === "hourly" && intraday ? intraday.coverage : null;
  const dcov = sc.coverage;
  const coverage = `<div class="rel-card"><div class="rel-card-title">Coverage &amp; maturity</div>
      <div class="rel-cov-grid">
        <div><span>Daily outcomes</span><b>${dcov.outcome_rows}</b></div>
        <div><span>Hourly outcomes</span><b>${intraday?.coverage.outcome_rows ?? 0}</b></div>
        <div><span>Tickered signals</span><b>${dcov.directional_signals}</b></div>
        <div><span>Events · nodes (daily)</span><b>${dcov.distinct_events} · ${dcov.distinct_nodes}</b></div>
      </div>
      <div class="rel-cov-foot">benchmark: ${sc.meta?.benchmark ?? "sector-excess"} · cost ${cost}bps round-trip`
    + (cov?.latency_min != null ? ` · hourly latency ${cov.latency_min}min` : "")
    + ` · computed ${sc.generated_at ? new Date(sc.generated_at).toLocaleString() : "—"}</div></div>`;

  el.innerHTML = header + framing + toggles + body + coverage;
}
