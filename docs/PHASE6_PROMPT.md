# Phase 6 — Frontend (Claude Code prompt)

The finish line. A Sigma.js web app that calls the Phase 5 API, renders the graph, and lets you explore by clicking. Its acceptance test is the one this whole build has aimed at: **reproduce, live and by clicking, the three diagrams drawn by hand at the very start — Costco-centered, P&G-centered, and the two joined into one value chain.** Run from the repo root in a **fresh** session. Phases 0–5 green; the Phase 5 API must be runnable. **No LLM.** This is a browser app over the existing API — it never touches the pipeline or the DB directly.

**Decisions locked with the user:**
1. **Open on the full graph (high-confidence core), then click-to-focus.** Render the whole core on load, labels suppressed except on high-degree nodes + on hover; clicking any node collapses the view to that node's ego-graph. The initial mode is a single config constant (`OPEN_MODE = "full" | "search"`) so it can flip to search-first in one line if the full view gets crowded at larger scale.
2. **Click a neighbor = expand in place; double-click = re-center.** Single-click fans the neighbor's neighbors out and grows the current graph; double-click makes it the new center and resets the view to its ego-graph. (These two gestures are what make the three-diagram reproduction possible.)
3. **Provisional/audit layer OFF by default, prominent toggle to reveal** the grey halo (Nestlé/Unilever/etc.).
4. **Click an edge → provenance panel** (build this regardless — it's the payoff of the verify gate): show the filing snippet, source company, filing URL, and extracted_by, so every relationship is auditable from the UI.

---

```
Read CLAUDE.md, docs/PRD.md, the Phase 6 notes, AND /mnt/skills/public/frontend-design/SKILL.md before doing anything. This is Phase 6: the Sigma.js frontend. It is a browser app that calls the Phase 5 API (default http://localhost:8001) — do NOT call the DB or pipeline directly, do NOT use any LLM. Build in web/ with Vite + TypeScript.

Stack: Vite + TypeScript + Sigma.js v3 + graphology + graphology-layout-forceatlas2. Load graph data exclusively via the Phase 5 API endpoints (/search, /node/{id}/ego, /subgraph, /edge/{id}, /health). API base URL in one config constant.

=== Core behaviors ===

OPEN (config OPEN_MODE, default "full"):
   - "full": on load, GET the full core graph (use /subgraph with the highest-degree node as seed at max hops, or add nothing new — simplest: fetch ego of a few hubs, OR have the app call a full-core fetch; if no single endpoint returns everything, assemble from /subgraph seeded at the top-degree node with hops=3, include_provisional=false). Render with ForceAtlas2 (run to a stable layout before/at first paint). Suppress node labels except for high-degree nodes; show any label on hover.
   - "search": start near-empty with the search box focused.
   - Either way the search box is always present.

SEARCH: a search box calls GET /search?q=...; selecting a result loads that node's ego-graph (re-center on it).

CLICK a node (single): GET /node/{id}/ego for that node and MERGE its neighbors into the current graph (expand in place — existing nodes stay, new ones fan out, layout re-settles). Respect the current edge-type filters and provisional toggle.

DOUBLE-CLICK a node: re-center — clear the canvas and load just that node's ego-graph as the new focus.

EDGE-TYPE FILTERS: four toggles — supplies (teal), customer_of (purple), competes_with (coral), regulated_by (blue). These pass through as the `types` query param. customer_of is the derived reverse-supplies view from the API; toggling it on shows the buyer→seller direction. Toggling changes what's fetched/shown.

PROVISIONAL TOGGLE: off by default; passes include_provisional to the API. On = grey halo of slug nodes + their dimmed edges appear; off = clean core. Make it a prominent, clearly-labelled control.

CLICK an edge: GET /edge/{id} and open a provenance panel showing the snippet (verbatim filing quote), the source filing + URL, and extracted_by. For a derived customer_of edge, show the underlying supplies edge's provenance, labelled as a derived view. This is the auditability payoff — make the snippet readable and the filing link clickable.

NODE DETAIL: clicking/hovering a node also shows its attributes (name, type, sector, industry, ticker, provisional flag) in a side panel.

=== Visual design (consult the frontend-design skill) ===
   - Commit to a clear, refined aesthetic — this is an analyst's instrument, not a toy. Favor a confident, legible, slightly editorial data-tool look over generic dashboard styling. Distinctive type, restrained palette built around the four relationship colors (teal/purple/coral/blue) on a neutral canvas, considered spacing.
   - Node styling: size by degree; color by type (real Company / Regulator / provisional slug visually distinct — slugs lighter/outlined). Edge color by relationship type; provisional edges dimmed/dashed.
   - Labels legible, not overprinted (hover reveal for low-degree nodes; persistent for hubs). Smooth layout, zoom, pan.
   - A clear legend (node types + the four edge colors). A small /health-backed status indicator. Loading states on fetches.
   - Match implementation complexity to a refined-minimal vision: precision and restraint, not maximalist effects. It should feel like a tool a strategist would trust.

=== Run + tests ===
   - Provide the run sequence: start the API (uvicorn api.main:app --port 8001) AND the Vite dev server (npm run dev), with the frontend pointed at the API. Document both.
   - Add a few lightweight checks: a smoke test that the app builds (vite build succeeds) and that the API client functions parse graphology responses correctly (unit-test the client against recorded/sample API JSON).
   - CORS: the API already allows localhost:5173 — keep the Vite dev server on that port or update the API's allowed origin.

Acceptance test (must pass before you stop) — THE THREE DIAGRAMS:
   1. COSTCO-CENTERED: search/navigate to Costco (or a present consumer-staples retailer if Costco isn't a node — note: Costco may be a provisional/non-core node; if so use a real filer hub and say so), re-center on it, and show its four-player neighborhood (suppliers in, customers via customer_of, competitors, regulators) by toggling the edge types. Screenshot.
   2. P&G-CENTERED: re-center on P&G (cik:0000080424); show its Walmart supplies edge, its regulators, its competitors. Toggle customer_of to see P&G as a supplier reversed into the buyer's view. Screenshot.
   3. THE CONNECTED CHAIN: starting from P&G, single-click to expand into Walmart/retailers, showing P&G→retailer supplies edges joining into one value chain across two hubs — the hand-drawn "connected" diagram, live. Screenshot.
   - Also verify: provisional toggle reveals the grey halo; clicking an edge opens its provenance snippet; double-click re-centers; the /health status shows connected.
   - Capture the three screenshots, confirm each behavior, print the run instructions, and STOP. This is the MVP — summarize what's built and what the post-MVP roadmap (commodity/region nodes, Wikidata enrichment, news-driven highlighting) would add next.
```

---

## Notes for you (not part of the prompt)

- **This is the MVP done.** When the three screenshots reproduce the diagrams we drew by hand in this conversation, you've closed the loop: the napkin sketch became a typed graph became a queryable API became a thing you navigate by clicking. That's the whole arc.
- **One honest caveat baked into the test:** Costco may not be a *core* node — it surfaced in our hand diagrams, but whether it's in the high-confidence consumer-staples graph depends on whether a filer named it as a >10% customer. If it's only a provisional/peripheral node, the prompt tells the agent to use a real filer hub instead and say so. Don't read that as a failure — it's the data being honest about what the filings support. The *interaction* (re-center, see four players) is what's being proven, and that works on any well-connected node.
- **The provenance panel is the soul of the thing.** Anyone can draw a graph; what makes yours trustworthy is that every edge opens to the sentence in a real 10-K that justifies it. When you click the P&G→Walmart line and read the actual filing quote, that's the verify gate's whole purpose made visible. Lean on that in any demo.
- **`OPEN_MODE` is your scaling escape hatch.** You chose full-graph-on-open; it'll be handsome at 166 core nodes. The day you load three sectors and it's a hairball, flip the constant to "search" and the app opens lean instead. One line, no rewrite — which is why I had it built as a constant rather than hardcoded.
- **After this:** the post-MVP roadmap is where your original bold vision lives — commodity/region nodes (the schema headroom is already there), Wikidata enrichment to promote the grey-halo slugs to verified entities, and then the news-driven highlighting that lights up affected value chains. None of that is a rewrite; it's additive on the substrate you've now built. But the MVP — the thing that proves the concept and lets you check the shape against what was in your head — is this phase.

When the three screenshots land, send them over. I'd like to see the diagrams we sketched in week one rendered from real SEC filings — that's a satisfying place to close the build.
