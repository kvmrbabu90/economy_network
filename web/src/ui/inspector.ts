// Right-side panel: node detail OR edge provenance. The provenance view is
// the auditability payoff -- every relationship surfaces the verbatim filing
// snippet that justified it.

import type { ApiEdge, ApiNode, EdgeType, ImpactVerdict } from "../api";
import type { EconGraph } from "../graph";
import { IMPACT_TINT_FLOOR } from "../impact";

export interface NodeExtras {
  impact?: ImpactVerdict;
  onDescribe?: (nodeId: string) => Promise<string>;
  describedText?: string;   // cached /describe result, restored so it survives inspector re-renders
  combinedImpact?: import("../api").NodeImpact | null;   // resolved verdict, or null = no recent impact
  onSharpen?: (headlines: string[], context: string | null) => void;   // run full V1 trace over all contributing events
}

// Point at the BODY div, not the outer <section>.  This keeps the toggle
// button and morning-brief panel alive when replaceChildren() runs.
const root = () => document.getElementById("inspector-body")!;

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K, attrs: Record<string, string> = {}, ...children: (Node | string)[]
): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  return e;
}

function escapeHTML(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));
}

function emptyState() {
  root().replaceChildren(
    el("div", { class: "inspector-empty" },
      el("div", { class: "inspector-empty-title" }, "Inspector"),
      el("p", {}, "Hover or click any node or edge to see details and provenance."),
    ),
  );
}

export function showEmpty() { emptyState(); }

// ---------------------------------------------------------------------------
// Node view
// ---------------------------------------------------------------------------

export function showNode(node: ApiNode, g: EconGraph, extras: NodeExtras = {}): void {
  const a = node.attributes;
  const r = root();
  r.replaceChildren();

  const pills: Node[] = [];
  pills.push(el("span", { class: "pill" }, a.type));
  if (a.provisional) pills.push(el("span", { class: "pill provisional" }, "provisional"));
  if (a.identity_unverified) pills.push(el("span", { class: "pill provisional" }, "identity unverified"));

  r.appendChild(el("h2", {}, a.label));
  r.appendChild(el("p", { class: "subtitle" }, node.key));
  const pillBox = el("div", { class: "pills" });
  pills.forEach((p) => pillBox.appendChild(p));
  r.appendChild(pillBox);

  // Impact verdict (most valuable info when impact run is active).
  if (extras.impact) {
    const v = extras.impact;
    const dirClass = v.direction === "positive" ? "impact-pos"
      : v.direction === "negative" ? "impact-neg"
      : v.direction === "unscored" ? "impact-unscored" : "impact-neutral";
    const dirLabel = v.direction === "positive" ? "POSITIVE"
      : v.direction === "negative" ? "NEGATIVE"
      : v.direction === "unscored" ? "UNSCORED" : "NO EFFECT";
    const box = el("div", { class: `impact-box ${dirClass}` });
    box.appendChild(el("div", { class: "impact-header" },
      el("span", { class: "impact-dir" }, dirLabel),
      el("span", { class: "impact-mag" }, `magnitude ${v.magnitude.toFixed(2)}`),
      el("span", { class: "impact-hop" }, `hop ${v.hop}`),
    ));
    if (typeof v.confidence === "number") {
      box.appendChild(el("div", { class: "impact-confidence" },
        el("span", { class: "conf-badge" }, `confidence ${Math.round(v.confidence * 100)}%`),
        el("span", { class: "conf-source" }, v.verified ? "· verified" : "· est."),
      ));
    }
    if (v.verification) {
      box.appendChild(el("p", { class: "impact-verify" },
        `Verifier: ${v.verification.verdict} — ${v.verification.reasoning}`));
    }
    // Phase K: financial grounding badge
    if (v.edge_weight != null) {
      const pct = Math.round(v.edge_weight * 100);
      const tierLabel =
        v.edge_source_tier === "sec_explicit" ? "SEC filing" :
        v.edge_source_tier === "sec_inferred" ? "SEC (inferred)" :
        v.edge_source_tier === "manual"        ? "manual" :
        v.edge_source_tier === "wikidata"      ? "Wikidata" :
        v.edge_source_tier === "wikipedia"     ? "Wikipedia" : "filing";
      box.appendChild(el("div", { class: "impact-exposure" },
        el("span", { class: "exposure-badge sec" }, `~${pct}% revenue exposure`),
        el("span", { class: "exposure-source" }, `· ${tierLabel}`),
      ));
    } else if (v.is_estimated) {
      box.appendChild(el("div", { class: "impact-exposure" },
        el("span", { class: "exposure-badge est" }, "est."),
        el("span", { class: "exposure-source" }, "· no SEC % on file"),
      ));
    }
    if (v.reasoning) {
      box.appendChild(el("p", { class: "impact-reasoning" }, v.reasoning));
    }
    if (v.via_parent && v.edge_type) {
      box.appendChild(el("p", { class: "impact-via" },
        `via ${v.via_parent} (${v.edge_type})`,
      ));
    }
    r.appendChild(box);
  }

  // Describe button -- LLM-generated 2-3 sentence business description.
  const md = (a.metadata as Record<string, unknown> | undefined) ?? {};
  const wd = (md.wikidata as Record<string, unknown> | undefined) ?? {};
  const hqLabel = typeof wd.hq === "string" ? wd.hq : null;
  if (extras.describedText || extras.onDescribe) {
    const aboutBox = el("div", { class: "about-box" });
    const descBody = el("p", { class: "about-body" });
    if (extras.describedText) {
      // Restored from cache — the description survives every inspector re-render
      // (hover, relayout) instead of vanishing back to a "Describe" button.
      descBody.textContent = extras.describedText;
    } else if (extras.onDescribe) {
      const descBtn = el("button", { class: "describe-btn", type: "button" },
        "Describe this " + (a.type === "Company" ? "business" : a.type.toLowerCase()),
      );
      descBtn.addEventListener("click", async () => {
        descBtn.setAttribute("disabled", "true");
        descBtn.textContent = "Asking Claude...";
        try {
          const text = await extras.onDescribe!(node.key);
          descBody.textContent = text || "(no description returned)";
          descBtn.remove();
        } catch (err) {
          descBody.textContent = `Error: ${String((err as Error).message || err)}`;
          descBtn.removeAttribute("disabled");
          descBtn.textContent = "Retry";
        }
      });
      aboutBox.appendChild(descBtn);
    }
    aboutBox.appendChild(descBody);
    r.appendChild(aboutBox);
  }

  const dl = el("dl");
  function row(label: string, value: string | null | undefined) {
    if (!value) return;
    dl.appendChild(el("dt", {}, label));
    dl.appendChild(el("dd", {}, value));
  }
  row("Type", a.type);
  if (a.tickers.length) row("Tickers", a.tickers.join(", "));
  row("Sector", a.sector);
  row("Industry", a.industry);
  row("Country", a.country);
  if (hqLabel) row("HQ", hqLabel);
  if (a.identifiers && Object.keys(a.identifiers).length) {
    const lines = Object.entries(a.identifiers).map(([k, v]) => `${k}: ${String(v)}`);
    row("Identifiers", lines.join(" · "));
  }
  if (a.aliases.length) row("Aliases", a.aliases.slice(0, 6).join(" · "));
  r.appendChild(dl);

  // Edge-type breakdown for the node.
  if (g.hasNode(node.key)) {
    const counts: Record<string, number> = {};
    g.forEachEdge(node.key, (_eid, attrs) => {
      counts[attrs.edgeType] = (counts[attrs.edgeType] ?? 0) + 1;
    });
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    if (total > 0) {
      r.appendChild(el("div", { class: "filter-title", style: "margin-bottom:8px" }, "Connections in view"));
      const list = el("ul", { class: "edges-summary" });
      const order: EdgeType[] = ["supplies", "customer_of", "competes_with", "regulated_by", "part_of"];
      for (const t of order) {
        if (!counts[t]) continue;
        const li = el("li");
        li.appendChild(el("span", { class: "pill t-" + t }, t));
        li.appendChild(el("span", { class: "e-label" }, ""));
        li.appendChild(el("span", { class: "e-count" }, String(counts[t])));
        list.appendChild(li);
      }
      r.appendChild(list);
    }
  }

  // Combined-impact ("why is this node tinted") section — rendered INLINE as part
  // of showNode so it survives EVERY inspector re-render (hover→showNode, the
  // post-click relayout sliding nodes under the cursor, tab switches). Previously
  // this box was only appended asynchronously after showNode ran, so any later
  // re-render wiped it — the "tinted node, no panel" bug. `undefined` = not fetched
  // yet (render nothing, the async fetch will paint it); `null` = fetched, no recent
  // impact (render "No recent impact."); object = the precomputed verdict + drivers.
  if (extras.combinedImpact !== undefined) {
    renderCombinedImpactInto(r, extras.combinedImpact, { onSharpen: extras.onSharpen });
  }
}

// ---------------------------------------------------------------------------
// Edge view -- the auditability payoff
// ---------------------------------------------------------------------------

export function showEdge(
  edge: ApiEdge,
  endpoints: { source: ApiNode | null; target: ApiNode | null } = { source: null, target: null },
): void {
  const a = edge.attributes;
  const r = root();
  r.replaceChildren();

  const sourceName = endpoints.source?.attributes.label ?? edge.source;
  const targetName = endpoints.target?.attributes.label ?? edge.target;

  r.appendChild(el("h2", {}, `${sourceName} → ${targetName}`));
  r.appendChild(el("p", { class: "subtitle" }, edge.key));

  const pills = el("div");
  pills.appendChild(el("span", { class: `pill t-${a.type}` }, a.type));
  if (a.derived) pills.appendChild(el("span", { class: "pill derived" }, "derived view"));
  if (a.below_threshold) pills.appendChild(el("span", { class: "pill below-threshold" }, "below threshold (audit)"));
  if (typeof a.confidence === "number") {
    pills.appendChild(el("span", { class: "pill" }, `confidence ${a.confidence.toFixed(2)}`));
  }
  // Phase K: source tier badge
  if (a.source_tier) {
    const tierLabel =
      a.source_tier === "sec_explicit"  ? "SEC %" :
      a.source_tier === "sec_inferred"  ? "SEC" :
      a.source_tier === "manual"        ? "manual" :
      a.source_tier === "wikidata"      ? "Wikidata" :
      a.source_tier === "wikipedia"     ? "Wikipedia" : a.source_tier;
    const tierClass =
      a.source_tier === "sec_explicit"  ? "pill-sec-explicit" :
      a.source_tier === "sec_inferred"  ? "pill-sec-inferred" :
      a.source_tier === "manual"        ? "pill-manual" : "pill-wiki";
    pills.appendChild(el("span", { class: `pill ${tierClass}` }, tierLabel));
  }
  r.appendChild(pills);

  // Phase K: financial exposure row (supplies edges with SEC %)
  if (a.weight != null && a.type === "supplies") {
    const pct = Math.round(a.weight * 100);
    r.appendChild(el("div", { class: "edge-exposure" },
      el("span", { class: "exposure-badge sec" }, `~${pct}% revenue exposure`),
      el("span", { class: "exposure-source" },
        a.source_tier === "sec_explicit" ? " · SEC 10-K (explicit)" :
        a.source_tier === "sec_inferred" ? " · SEC 10-K (inferred)" : " · filing"),
    ));
  }

  if (a.derived && a.underlying_edge_id) {
    r.appendChild(el("p", { class: "hint", style: "margin: 12px 0 4px" },
      "Derived from the underlying supplies edge. Same provenance, reversed view."));
  }

  const prov = a.provenance;
  if (prov.snippet) {
    const block = el("div", { class: `provenance t-${a.type}` });
    block.appendChild(el("p", { class: "snippet" }, prov.snippet));
    const meta = el("div", { class: "source" });
    const left = el("div");
    left.textContent = `${prov.extracted_by} · ${prov.filing || "—"}`;
    meta.appendChild(left);
    if (prov.url) {
      const link = el("a", { href: prov.url, target: "_blank", rel: "noopener noreferrer" }, "open filing →");
      meta.appendChild(link);
    }
    block.appendChild(meta);
    r.appendChild(block);
  }

  if (a.additional_provenance && a.additional_provenance.length) {
    r.appendChild(el("div", { class: "filter-title", style: "margin-top:8px" },
      `+ ${a.additional_provenance.length} additional source${a.additional_provenance.length === 1 ? "" : "s"} merged into this edge`));
    for (const p of a.additional_provenance) {
      const block = el("div", { class: `provenance t-${a.type}` });
      block.appendChild(el("p", { class: "snippet" }, p.snippet));
      const meta = el("div", { class: "source" });
      meta.appendChild(el("div", {}, `${p.extracted_by} · ${p.filing || "—"}`));
      if (p.url) meta.appendChild(el("a", { href: p.url, target: "_blank", rel: "noopener noreferrer" }, "open filing →"));
      block.appendChild(meta);
      r.appendChild(block);
    }
  }
}

// ---------------------------------------------------------------------------
// Combined impact (So What? V2) -- precomputed, netted verdict + event timeline
// ---------------------------------------------------------------------------

export interface TimelineRow {
  headline: string; direction: string; magnitude: number;
  publishedAt: string | null; linkUrl: string | null; sourceLabel: string | null;
}

/** Only http(s) URLs are safe to use as an <a href>. Event urls come from
 *  external news feeds, so a `javascript:`/`data:` URI must never become a
 *  clickable link (XSS in the app origin). Returns the url or null. */
function _safeHttpUrl(u: string | null | undefined): string | null {
  if (!u) return null;
  try {
    const p = new URL(u);   // absolute parse; throws on relative/garbage
    return p.protocol === "http:" || p.protocol === "https:" ? u : null;
  } catch {
    return null;
  }
}

/** Pure: the direction label to show for a combined-impact verdict.
 *  A magnitude-0 / no_effect row must NEVER show a MIXED badge — the map
 *  doesn't tint no_effect nodes, so the inspector must agree. Treat "mixed"
 *  as meaningful only when there is actually a signal (magnitude > 0). */
export function combinedDirLabel(
  imp: { direction: string; magnitude: number; mixed_signals: boolean | number },
): string {
  const mixed = !!imp.mixed_signals && imp.magnitude > 0;
  if (mixed) return "MIXED";
  return imp.direction === "positive" ? "POSITIVE"
    : imp.direction === "negative" ? "NEGATIVE" : "NO EFFECT";
}

/** Pure: has the precomputed impact data gone stale? True when `computedAt`
 *  is missing/unparseable or older than `maxAgeMs` (default 2 h). Used to drive
 *  the unobtrusive "impact data as of …" staleness note so a stalled hourly
 *  pipeline is visible without a manual reload. */
export function isStale(
  computedAt: string | null | undefined,
  now: number = Date.now(),
  maxAgeMs: number = 2 * 60 * 60 * 1000,
): boolean {
  if (!computedAt) return true;
  const t = Date.parse(computedAt);
  if (Number.isNaN(t)) return true;
  return now - t > maxAgeMs;
}

/** Pure view-model: flatten a NodeImpact's top_events into renderable rows. */
export function buildTimelineRows(impact: import("../api").NodeImpact): TimelineRow[] {
  return (impact.top_events ?? []).map(e => ({
    headline: e.headline, direction: e.direction, magnitude: e.magnitude,
    publishedAt: e.published_at, linkUrl: _safeHttpUrl(e.url), sourceLabel: e.source ?? null,
  }));
}

/** Render the combined-impact section into `root` (the #inspector-body element that
 *  showNode renders into). Removes any prior `.combined-impact-box` so re-clicks
 *  don't stack. Exported so main.ts can call it after the async /node/{id}/impact fetch. */
export function renderCombinedImpactInto(
  root: HTMLElement,
  imp: import("../api").NodeImpact | null,
  handlers: { onSharpen?: (headlines: string[], context: string | null) => void } = {},
): void {
  root.querySelector(".combined-impact-box")?.remove();
  const box = el("div", { class: "combined-impact-box" });
  box.appendChild(el("div", { class: "combined-impact-title" }, "Combined impact · precomputed"));
  // Mirror the map's tint floor: a node the map refuses to tint (no_effect, or
  // magnitude at/below the floor) must NOT present a populated impact box, or an
  // untinted node contradicts itself when clicked. Treat those as the empty case.
  if (!imp || imp.direction === "no_effect" || imp.magnitude <= IMPACT_TINT_FLOOR) {
    box.appendChild(el("p", { class: "combined-empty" }, "No recent impact."));
    root.appendChild(box);
    return;
  }
  // Count label: prefer the real driver_count ("3 drivers") as the primary figure so
  // the header stops overstating with event_count (which also counts unscored /
  // no_effect rows scanned for context). When more rows were scanned than drove the
  // verdict, append a muted "· scanned N". Falls back to "N events" when driver_count
  // is unavailable (0) — e.g. the compact live-map synth shown on hover-before-click.
  const drivers = imp.driver_count ?? 0;
  const countChildren: (Node | string)[] = [];
  if (drivers > 0) {
    countChildren.push(el("span", { class: "impact-count" }, `${drivers} driver${drivers === 1 ? "" : "s"}`));
    if (imp.event_count > drivers) {
      countChildren.push(el("span", { class: "impact-scanned" }, `· scanned ${imp.event_count}`));
    }
  } else {
    countChildren.push(el("span", { class: "impact-count" }, `${imp.event_count} event${imp.event_count === 1 ? "" : "s"}`));
  }
  box.appendChild(el("div", { class: "combined-header" },
    el("span", { class: "impact-dir" }, combinedDirLabel(imp)),
    el("span", { class: "impact-mag" }, `magnitude ${imp.magnitude.toFixed(2)}`),
    ...countChildren,
  ));
  // computed_at is empty for the compact live-map synth (hover before first click);
  // skip the freshness line rather than render a bare "as of ".
  if (imp.computed_at) {
    box.appendChild(el("div", { class: "combined-freshness" }, `as of ${imp.computed_at}`));
  }
  // Honesty flag: a verdict with drivers but NO direct (hop-0) news is entirely
  // propagated sector spillover — a bright tint on a node that had no news of its
  // own. Say so, so a "0.90 · no story" reads correctly. (direct_count may be absent
  // on the compact synth or pre-migration rows — only flag when we actually know it.)
  if (drivers > 0 && imp.direct_count === 0) {
    box.appendChild(el("div", { class: "combined-propagated" },
      "↳ no direct news — propagated from related companies"));
  }
  const rows = buildTimelineRows(imp);
  if (rows.length) {
    const tl = el("div", { class: "combined-timeline" });
    for (const rrow of rows) {
      const item = el("div", { class: "timeline-event" },
        el("span", { class: `event-dir ${rrow.direction}` }, rrow.direction.toUpperCase()),
        el("span", { class: "event-headline" }, rrow.headline),
        el("span", { class: "event-date" }, rrow.publishedAt ?? ""));
      if (rrow.linkUrl) {
        item.appendChild(el("a", { class: "event-source", href: rrow.linkUrl,
          target: "_blank", rel: "noopener noreferrer" }, rrow.sourceLabel ?? "source"));
      } else if (rrow.sourceLabel) {
        item.appendChild(el("span", { class: "event-source muted" }, rrow.sourceLabel));
      }
      tl.appendChild(item);
    }
    box.appendChild(tl);
  }
  if (handlers.onSharpen && rows.length) {
    const btn = el("button", { class: "sharpen-btn", type: "button" }, "Sharpen with Claude");
    // Pass every displayed event headline (not just the strongest) so the
    // full-strength re-trace covers the same event set as the precomputed verdict,
    // plus the first available GKG grounding capsule (other orgs + money + tone).
    const headlines = rows.map((r) => r.headline).filter(Boolean);
    const context = imp.top_events.map((e) => e.gkg_context).find((c) => c) ?? null;
    btn.addEventListener("click", () => handlers.onSharpen!(headlines, context));
    box.appendChild(btn);
  }
  root.appendChild(box);
}
