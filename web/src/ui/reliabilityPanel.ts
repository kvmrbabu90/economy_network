// Dependency-free renderer for the Reliability tab. Turns the signal-reliability
// scorecard into: an overall hit-rate readout per horizon, a hit-rate heatmap
// (order x horizon), a magnitude information-coefficient heatmap, confidence-
// calibration bars (hit-rate by magnitude band), and a coverage/maturity block.
// Every number carries n + a Wilson CI; low-n cells are dimmed, not hidden
// (statistical honesty — reliability is a property of a signal CLASS).
import type { ReliabilitySummary, RelStat, RelHopHorizon } from "../api";

// Canonical axes so immature cells render as "pending" and the grid fills in
// over time as prices mature, rather than silently shrinking.
const HORIZONS = [1, 5, 20, 60];
const HOPS = [0, 1, 2];
const HOP_LABEL: Record<number, string> = { 0: "1st · direct", 1: "2nd · supplier/peer", 2: "3rd+" };
const LOW_N = 10; // below this a class is low-power: shown, but dimmed + flagged

const pct = (x: number) => `${(x * 100).toFixed(0)}%`;
const pct1 = (x: number) => `${(x * 100).toFixed(1)}%`;

function mix(a: number[], b: number[], t: number): string {
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}
const GRAY = [58, 63, 68], TEAL = [45, 212, 191], CORAL = [244, 114, 128];

// Hit-rate color: 50% (coin-flip) = gray, higher = teal, lower = coral.
function hitColor(hr: number): string {
  const d = Math.max(-0.25, Math.min(0.25, hr - 0.5)) / 0.25;
  return d >= 0 ? mix(GRAY, TEAL, d) : mix(GRAY, CORAL, -d);
}
// IC color: 0 = gray, positive = teal, negative = coral.
function icColor(ic: number): string {
  const d = Math.max(-0.4, Math.min(0.4, ic)) / 0.4;
  return d >= 0 ? mix(GRAY, TEAL, d) : mix(GRAY, CORAL, -d);
}

function statFor(sc: ReliabilitySummary, hop: number, horizon: number, clean: boolean): RelStat | null {
  const cell = sc.by_hop_horizon.find((c: RelHopHorizon) => c.hop === hop && c.horizon_days === horizon);
  if (!cell) return null;
  return clean ? cell.clean : cell.all;
}

function heatCell(stat: RelStat | null, kind: "hit" | "ic"): string {
  if (!stat || stat.n === 0) return `<div class="rel-cell rel-cell-empty" title="no mature signals yet">·</div>`;
  const lowN = stat.n < LOW_N;
  if (kind === "ic") {
    if (stat.ic === null) return `<div class="rel-cell rel-cell-empty" title="n=${stat.n}, too few for IC">n=${stat.n}</div>`;
    const bg = icColor(stat.ic);
    return `<div class="rel-cell${lowN ? " rel-lown" : ""}" style="background:${bg}" `
      + `title="IC ${stat.ic.toFixed(2)} · n=${stat.n}">${stat.ic.toFixed(2)}<span class="rel-cell-n">n=${stat.n}</span></div>`;
  }
  const bg = hitColor(stat.hit_rate);
  return `<div class="rel-cell${lowN ? " rel-lown" : ""}" style="background:${bg}" `
    + `title="hit ${pct1(stat.hit_rate)} · 95% CI ${pct(stat.ci_low)}–${pct(stat.ci_high)} · n=${stat.n}">`
    + `${pct(stat.hit_rate)}<span class="rel-cell-n">n=${stat.n}</span></div>`;
}

function heatmap(sc: ReliabilitySummary, kind: "hit" | "ic", clean: boolean): string {
  const head = `<div class="rel-grid-head"></div>` +
    HORIZONS.map((h) => `<div class="rel-grid-head">+${h}d</div>`).join("");
  const rows = HOPS.map((hop) => {
    const label = `<div class="rel-row-label" title="hop ${hop}">${HOP_LABEL[hop] ?? `hop ${hop}`}</div>`;
    const cells = HORIZONS.map((h) => heatCell(statFor(sc, hop, h, clean), kind)).join("");
    return label + cells;
  }).join("");
  return `<div class="rel-grid">${head}${rows}</div>`;
}

function calibration(sc: ReliabilitySummary): string {
  // Calibration = does a higher predicted magnitude band hit more often? Show at the
  // shortest horizon that has data (the most-matured slice).
  const horizons = [...new Set(sc.by_magband.map((b) => b.horizon_days))].sort((a, b) => a - b);
  if (!horizons.length) return `<p class="rel-empty">No calibration data yet.</p>`;
  const h = horizons[0];
  const bands = ["low", "med", "high"];
  const rows = bands.map((band) => {
    const b = sc.by_magband.find((x) => x.mag_band === band && x.horizon_days === h);
    if (!b) return `<div class="rel-bar-row"><span class="rel-bar-lab">${band}</span><div class="rel-bar-track"></div><span class="rel-bar-val">—</span></div>`;
    const lowN = b.n < LOW_N;
    return `<div class="rel-bar-row">`
      + `<span class="rel-bar-lab">${band}</span>`
      + `<div class="rel-bar-track"><div class="rel-bar-fill${lowN ? " rel-lown" : ""}" style="width:${pct(b.hit_rate)};background:${hitColor(b.hit_rate)}"></div>`
      + `<span class="rel-bar-coin"></span></div>`
      + `<span class="rel-bar-val">${pct(b.hit_rate)} <em>n=${b.n}</em></span>`
      + `</div>`;
  }).join("");
  const mb = sc.meta?.mag_bands;
  const legend = mb ? `<div class="rel-bands-legend">low ${mb.low} · med ${mb.med} · high ${mb.high}</div>` : "";
  return `<div class="rel-bars" data-horizon="${h}">${rows}</div>`
    + `<div class="rel-sub">at +${h}d · dashed line = 50% coin-flip</div>${legend}`;
}

function overall(sc: ReliabilitySummary): string {
  const cards = HORIZONS.map((h) => {
    const s = sc.overall[String(h)];
    if (!s) return `<div class="rel-ov-card rel-ov-pending"><div class="rel-ov-h">+${h}d</div><div class="rel-ov-pct">pending</div></div>`;
    return `<div class="rel-ov-card">`
      + `<div class="rel-ov-h">+${h}d</div>`
      + `<div class="rel-ov-pct" style="color:${hitColor(s.hit_rate)}">${pct(s.hit_rate)}</div>`
      + `<div class="rel-ov-ci">CI ${pct(s.ci_low)}–${pct(s.ci_high)}</div>`
      + `<div class="rel-ov-n">n=${s.n}</div>`
      + `</div>`;
  }).join("");
  return `<div class="rel-overall">${cards}</div>`;
}

function coverageBlock(sc: ReliabilitySummary): string {
  const c = sc.coverage;
  const matured = HORIZONS.map((h) => `+${h}d: ${c.mature_by_horizon[String(h)] ?? 0}`).join(" · ");
  const gen = sc.generated_at ? new Date(sc.generated_at).toLocaleString() : "—";
  return `<div class="rel-coverage">`
    + `<div class="rel-cov-row"><span>Directional signals (tickered)</span><b>${c.directional_signals}</b></div>`
    + `<div class="rel-cov-row"><span>Signals with a mature outcome</span><b>${c.signals_with_mature_outcome}</b></div>`
    + `<div class="rel-cov-row"><span>Events · nodes covered</span><b>${c.distinct_events} · ${c.distinct_nodes}</b></div>`
    + `<div class="rel-cov-row"><span>Mature outcomes by horizon</span><b>${matured}</b></div>`
    + `<div class="rel-cov-foot">benchmark: ${sc.meta?.benchmark ?? "sector-excess"} · computed ${gen}</div>`
    + `</div>`;
}

export interface RelViewOpts { clean: boolean }

/** Render the whole Reliability panel into `el`. Pure wrt the scorecard + opts. */
export function renderReliability(el: HTMLElement, sc: ReliabilitySummary, opts: RelViewOpts): void {
  if (sc.status === "not_computed") {
    el.innerHTML = `<p class="rel-empty">${sc.message ?? "No reliability scorecard yet."}</p>`
      + `<p class="rel-sub">It is produced by <code>pipeline/run_validation.py</code> (no LLM) and matures as prices accrue.</p>`;
    return;
  }
  const total = sc.coverage.outcome_rows;
  const horizonsMature = sc.meta?.horizons?.length ?? 0;
  const sparse = (total < 120 || horizonsMature < HORIZONS.length)
    ? `<div class="rel-banner">Early data — ${total} mature outcome${total === 1 ? "" : "s"} across ${horizonsMature} of ${HORIZONS.length} horizons. Intervals are wide and fill in over days (short horizons) to ~a quarter (+60d).</div>`
    : "";
  el.innerHTML =
    sparse +
    overall(sc) +
    `<div class="rel-section">`
    + `<div class="filter-title">Directional hit-rate · order × horizon`
    + `<span class="rel-toggle">`
    + `<button class="rel-clean-btn${!opts.clean ? " is-active" : ""}" data-clean="0">all</button>`
    + `<button class="rel-clean-btn${opts.clean ? " is-active" : ""}" data-clean="1">clean</button>`
    + `</span></div>`
    + heatmap(sc, "hit", opts.clean)
    + `<div class="rel-legend"><span>worse</span><i class="rel-leg-grad"></i><span>better</span><em>coin-flip = gray</em></div>`
    + `</div>`
    + `<div class="rel-section"><div class="filter-title">Magnitude information coefficient</div>`
    + heatmap(sc, "ic", opts.clean)
    + `<div class="rel-sub">Spearman(predicted magnitude, favorable abnormal return). Positive = stronger calls move more.</div>`
    + `</div>`
    + `<div class="rel-section"><div class="filter-title">Confidence calibration</div>`
    + calibration(sc)
    + `</div>`
    + `<div class="rel-section"><div class="filter-title">Coverage &amp; maturity</div>`
    + coverageBlock(sc)
    + `</div>`;
}
