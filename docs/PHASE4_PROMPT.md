# Phase 4 — Graph Build (Claude Code prompt)

Load the resolved nodes/edges into SQLite (the source of truth), emit a graphology-compatible `graph.json`, print graph stats, and — the part you've been waiting for — drop a throwaway static preview so you get your FIRST visual look at the real consumer-staples network, two phases before the Sigma.js renderer. Run from the repo root in a **fresh** session. Phases 0–3 green. Deterministic — **no LLM, no API, no network.**

**Lock this first:** the Phase 3 confidence cutoff is confirmed at **0.75** (clean separation, empty middle band). No change to Phase 3 needed.

---

```
Read CLAUDE.md, docs/PRD.md, and the Phase 4 notes fully before doing anything. This is Phase 4 (graph build). DETERMINISTIC: no LLM, no claude -p, no API, no network calls. Do NOT build the FastAPI service (that's Phase 5) or the real Sigma.js app (Phase 6). The preview.html in this phase is an intentionally throwaway static look, not the MVP renderer.

Inputs: data/nodes.jsonl (166 canonical Nodes), data/edges.jsonl (256 validated Edges), data/aliases.jsonl. (edges_below_threshold.jsonl and review_queue.jsonl exist but are NOT loaded into the graph.)
Outputs: econgraph.db (SQLite source of truth), data/graph.json (graphology format), scripts/preview.html (throwaway static viewer), and a printed stats report.

=== PART A — Load into SQLite (source of truth) ===

A1. Use the existing schema/store.py DDL (nodes, edges, aliases tables). Build pipeline/build_graph.py that:
   - init_db() fresh, then loads all 166 nodes, 256 edges, 226 aliases via the existing validated upsert helpers (everything routes through the Pydantic models — no raw inserts).
   - Is idempotent: running twice yields the same DB (upserts, not duplicate-inserts). The unique (source,target,type) index from Phase 0 already guards edge dupes.
   - Verifies on load: every edge's source_id and target_id exist in the nodes table (referential integrity). Fail loudly listing any orphan edge — there should be zero given Phase 3, but check, because a dangling edge is a silent corruption.

=== PART B — Emit graph.json (graphology format) ===

B1. Export a graphology-compatible JSON: { "nodes": [{ "key": id, "attributes": { label, type, sector, industry, provisional, ... } }], "edges": [{ "key", "source", "target", "attributes": { type, confidence, directed, ... } }] }.
   - competes_with edges: attributes.directed=false (undirected, already deduped on unordered pair in Phase 3). supplies and regulated_by: directed=true.
   - Carry node type + provisional flag into attributes so the renderer can style them.
   - This file is what Phase 5's API will serve and Phase 6's renderer will load — get the shape right.

=== PART C — Stats report (this is where you SEE the shape) ===

C1. Print:
   - node counts by type (Company / Regulator / provisional-slug Company / Segment)
   - edge counts by type (supplies / competes_with / regulated_by)
   - degree distribution: top 10 nodes by total degree, and the in/out breakdown
   - the most-connected non-filer slugs (expect Nestlé, Unilever near the top — confirms the "everyone names the big non-US players" finding)
   - connected-components count (is it one graph or several islands?)
   - a "supply layer" callout: how many supplies edges survive, and how many point at provisional vs. real nodes (expect this layer to be THIN — that's the honest data reality, not a bug)

=== PART D — Throwaway static preview (your first look) ===

D1. Create scripts/preview.html: a SINGLE self-contained HTML file that loads data/graph.json and force-renders it. Use a CDN-loaded graph lib (sigma + graphology from jsdelivr/unpkg, or vis-network — your call, simplest that works). This is throwaway scaffolding, NOT the Phase 6 app:
   - Color nodes by type and edges by relationship, matching the project palette: supplies=teal, competes_with=coral, regulated_by=blue, regulator nodes/neutral=blue-gray, provisional slugs visually distinct (e.g. lighter/dashed).
   - Force-directed layout, zoom + pan, node labels on hover.
   - A tiny legend. No API, no interactivity beyond zoom/pan/hover — it reads the static graph.json directly (open via a local file server if needed for fetch()).
   - Add a one-line note at top of the file marking it throwaway/Phase-4-preview so nobody mistakes it for the real frontend.

Acceptance test (must pass before you stop):
   - econcraph.db loads all 166 nodes + 256 edges + 226 aliases; referential-integrity check passes (0 orphan edges).
   - Re-running build_graph.py is idempotent (same DB, same counts).
   - graph.json validates: every edge source/target is a node key; competes_with edges are undirected, supplies/regulated_by directed.
   - The stats report prints all of Part C. In particular: confirm it's mostly one connected component, that Nestlé/Unilever are among the highest-degree nodes, and that the P&G→Walmart supplies edge and P&G's regulated_by edges are present and traceable.
   - scripts/preview.html opens and renders the network with the palette + legend.
   - Print the stats report, tell me how to open the preview (exact command for the local server), and STOP. Summarize what Phase 5 (API) will serve.
```

---

## Notes for you (not part of the prompt)

- **This is your alignment checkpoint.** When the preview renders, the question to ask yourself is the one you raised eight steps ago: *does this match the thing in my head?* You'll be looking at the real consumer-staples slice — P&G, Costco-adjacents, the Nestlé/Unilever hub, the regulator cluster. If the shape surprises you, now is the cheap moment to say so, before Phase 5/6 build real infrastructure on top of it.
- **Expect a lopsided graph, and that's correct.** Dense competitor mesh + a tidy regulator cluster + a *thin* supply layer. We predicted this back when we first discussed data reality: 10-Ks disclose competitors and customer-concentration richly, but full supplier lists aren't public. The preview will make that imbalance visual. It's the honest shape of free-data extraction, not a defect — and it's exactly why the post-MVP roadmap leans on commodity nodes and enrichment to thicken the value-chain story.
- **The Nestlé/Unilever hubs are the interesting story.** If they render as the highest-degree nodes despite being provisional slugs, that's your graph telling you the truth: in this sector, the most central competitors are non-US firms nobody in your S&P slice can "be," only name. That's a genuine insight the graph surfaced on its own — and a concrete argument for Wikidata enrichment being the first post-MVP move.
- **preview.html is deliberately disposable.** Don't polish it, don't let it accrete features — it dies when Phase 6's real renderer arrives. Its only job is to let you eyeball the network now. If you find yourself wanting to add filters or click-to-expand to it, that urge is the Phase 6 spec talking; note it down for then, don't build it here.

When Phase 4 is green and you've looked at the preview, Phase 5 is the API — FastAPI serving ego-graph and n-hop queries over econgraph.db, with `customer_of` derived (never stored) at query time per invariant #2. That derive-on-read step is the one bit of real logic in Phase 5, and it's where the "supplier and customer are the same edge seen from two ends" idea finally becomes a live query. Send me the Phase 4 stats report and your reaction to the preview, and I'll write it.
