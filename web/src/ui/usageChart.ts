// Dependency-free SVG chart for the Usage tab: stacked input/output/cache token
// bars per time bucket, with an overlaid cost line on a secondary axis.
import type { UsageBucket, UsageResponse } from "../api";

export interface BarSeg { y: number; h: number; }
export interface Bar {
  x: number; w: number; total: number; bucket: UsageBucket;
  input: BarSeg; output: BarSeg; cache: BarSeg;
}
export interface ChartGeom {
  bars: Bar[];
  costPoints: { x: number; y: number }[];
  maxTokens: number;
  maxCost: number;
  plot: { x: number; y: number; w: number; h: number };
}

const PAD = { l: 34, r: 40, t: 12, b: 26 };

/** Pure layout: bucket data → bar/line geometry. No DOM. Unit-tested. */
export function layoutBars(buckets: UsageBucket[], W: number, H: number): ChartGeom {
  const plot = { x: PAD.l, y: PAD.t, w: W - PAD.l - PAD.r, h: H - PAD.t - PAD.b };
  const totals = buckets.map((b) => b.input_tokens + b.output_tokens + b.cache_read_tokens);
  const maxTokens = Math.max(1, ...totals);
  const maxCost = Math.max(1e-9, ...buckets.map((b) => b.cost_usd));
  const slot = buckets.length ? plot.w / buckets.length : plot.w;
  const bw = Math.max(1, slot * 0.7);
  const base = plot.y + plot.h;
  const scale = (v: number) => (v / maxTokens) * plot.h;

  const bars: Bar[] = buckets.map((b, i) => {
    const x = plot.x + i * slot + (slot - bw) / 2;
    const inputH = scale(b.input_tokens);
    const outputH = scale(b.output_tokens);
    const cacheH = scale(b.cache_read_tokens);
    const inputY = base - inputH;
    const outputY = inputY - outputH;
    const cacheY = outputY - cacheH;
    return {
      x, w: bw, total: b.input_tokens + b.output_tokens + b.cache_read_tokens, bucket: b,
      input: { y: inputY, h: inputH },
      output: { y: outputY, h: outputH },
      cache: { y: cacheY, h: cacheH },
    };
  });
  const costPoints = buckets.map((b, i) => ({
    x: plot.x + i * slot + slot / 2,
    y: base - (b.cost_usd / maxCost) * plot.h,
  }));
  return { bars, costPoints, maxTokens, maxCost, plot };
}

const COL = {
  input: "#2dd4bf", output: "#fb7185", cache: "#4b5563",
  cost: "#60a5fa", text: "#8b9096",
};

function fmtTokens(n: number): string {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(Math.round(n));
}

/** Render the stacked-bar + cost-line chart into `root`. */
export function renderUsageChart(root: HTMLElement, resp: UsageResponse): void {
  const buckets = resp.buckets;
  if (!buckets.length) {
    root.innerHTML =
      '<p class="combined-empty">No LLM usage recorded yet — the chart fills as cycles run.</p>';
    return;
  }
  const W = 320, H = 200;
  const g = layoutBars(buckets, W, H);
  const p: string[] = [];

  // baseline
  p.push(`<line x1="${g.plot.x}" y1="${g.plot.y + g.plot.h}" x2="${g.plot.x + g.plot.w}" `
       + `y2="${g.plot.y + g.plot.h}" stroke="${COL.text}" stroke-opacity="0.3"/>`);

  for (const bar of g.bars) {
    const b = bar.bucket;
    const tip = `${b.bucket}\nin ${b.input_tokens}  out ${b.output_tokens}  cache ${b.cache_read_tokens}`
              + `\n${b.calls} call${b.calls === 1 ? "" : "s"}  $${b.cost_usd.toFixed(4)}`;
    p.push(`<g><title>${tip}</title>`);
    if (bar.input.h > 0.2) p.push(`<rect x="${bar.x.toFixed(1)}" y="${bar.input.y.toFixed(1)}" width="${bar.w.toFixed(1)}" height="${bar.input.h.toFixed(1)}" fill="${COL.input}"/>`);
    if (bar.output.h > 0.2) p.push(`<rect x="${bar.x.toFixed(1)}" y="${bar.output.y.toFixed(1)}" width="${bar.w.toFixed(1)}" height="${bar.output.h.toFixed(1)}" fill="${COL.output}"/>`);
    if (bar.cache.h > 0.2) p.push(`<rect x="${bar.x.toFixed(1)}" y="${bar.cache.y.toFixed(1)}" width="${bar.w.toFixed(1)}" height="${bar.cache.h.toFixed(1)}" fill="${COL.cache}"/>`);
    p.push(`</g>`);
  }

  // cost line (secondary axis)
  const d = g.costPoints.map((pt, i) => `${i ? "L" : "M"}${pt.x.toFixed(1)},${pt.y.toFixed(1)}`).join("");
  p.push(`<path d="${d}" fill="none" stroke="${COL.cost}" stroke-width="1.4"/>`);

  // x-axis labels (thinned to ~6)
  const step = Math.max(1, Math.ceil(g.bars.length / 6));
  g.bars.forEach((bar, i) => {
    if (i % step) return;
    const label = bar.bucket.bucket.replace(/^\d{4}-/, "");   // drop leading year for width
    p.push(`<text x="${(bar.x + bar.w / 2).toFixed(1)}" y="${H - 8}" fill="${COL.text}" font-size="7" text-anchor="middle">${label}</text>`);
  });

  // axis maxima
  p.push(`<text x="2" y="${(g.plot.y + 8).toFixed(1)}" fill="${COL.text}" font-size="7">${fmtTokens(g.maxTokens)}</text>`);
  p.push(`<text x="${W - 2}" y="${(g.plot.y + 8).toFixed(1)}" fill="${COL.cost}" font-size="7" text-anchor="end">$${g.maxCost.toFixed(3)}</text>`);

  root.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block" role="img" `
    + `aria-label="LLM token usage per ${resp.granularity}">${p.join("")}</svg>`
    + `<div style="display:flex;gap:10px;font-size:10px;margin-top:4px;color:${COL.text}">`
    + `<span><span style="color:${COL.input}">■</span> input</span>`
    + `<span><span style="color:${COL.output}">■</span> output</span>`
    + `<span><span style="color:${COL.cache}">■</span> cache</span>`
    + `<span><span style="color:${COL.cost}">━</span> cost</span></div>`;
}
