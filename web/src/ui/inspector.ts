// Right-side panel: node detail OR edge provenance. The provenance view is
// the auditability payoff -- every relationship surfaces the verbatim filing
// snippet that justified it.

import type { ApiEdge, ApiNode, EdgeType, ImpactVerdict } from "../api";
import type { EconGraph } from "../graph";

export interface NodeExtras {
  impact?: ImpactVerdict;
  onDescribe?: (nodeId: string) => Promise<string>;
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
      : v.direction === "negative" ? "impact-neg" : "impact-neutral";
    const dirLabel = v.direction === "positive" ? "POSITIVE"
      : v.direction === "negative" ? "NEGATIVE" : "NO EFFECT";
    const box = el("div", { class: `impact-box ${dirClass}` });
    box.appendChild(el("div", { class: "impact-header" },
      el("span", { class: "impact-dir" }, dirLabel),
      el("span", { class: "impact-mag" }, `magnitude ${v.magnitude.toFixed(2)}`),
      el("span", { class: "impact-hop" }, `hop ${v.hop}`),
    ));
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
  if (extras.onDescribe) {
    const aboutBox = el("div", { class: "about-box" });
    const descBtn = el("button", { class: "describe-btn", type: "button" },
      "Describe this " + (a.type === "Company" ? "business" : a.type.toLowerCase()),
    );
    const descBody = el("p", { class: "about-body" });
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
