# Phase 7 — Multi-Sector Scale-Out (Claude Code prompt)

Scale the graph from one sector (~36 companies) to the full S&P 500 (~500). This is a **data expansion, not an architecture change** — the pipeline is already sector-agnostic. The risk lives in four specific places, each gated below. Run from the repo root; this one spans **multiple sessions** by design (the full extraction won't finish in one). Phases 0–6 green. The biggest rule: **build the full company registry BEFORE resolving any edge** (registry-first ordering).

**Locked decisions / constraints to carry in:**
- **Registry-first:** all ~500 Company nodes ingested and in the registry before Phase 3 resolution runs on anything. Non-negotiable — it's what makes cross-sector edges resolve to real `cik:` nodes instead of colliding slugs.
- **Batched + resumable:** run extraction in sector-group batches across sittings; the Phase 2 checkpoint carries progress. Respect the Max-plan "ordinary individual usage" weekly ceiling — do not attempt 1,000 `claude -p` calls in one burst.
- **`claude -p` stays the extractor** (no API key), same verify gate, same 0.75 cutoff, same single-node invariant.
- **Frontend `OPEN_MODE` flips to "search"** — 500 nodes on open is a hairball; the app should open lean and build outward.

---

```
Read CLAUDE.md, docs/PRD.md, and the Phase 7 notes fully before doing anything. This is Phase 7: scale to the full S&P 500. The pipeline is sector-agnostic — this is mostly running existing stages at scale plus four targeted fixes. Do NOT rewrite stages. Do NOT use an API key (claude -p only). Preserve every invariant: single-node, customer_of-derived-not-stored, grounded-and-verified edges, 0.75 cutoff. This phase spans multiple sessions; use the checkpoint and STOP at the gates.

=== PART A — Pre-flight: extend coverage maps (do this BEFORE any big run) ===

A1. GICS rollup expansion. config/gics_subindustry_to_industry.yaml currently covers only the ~11 Consumer Staples sub-industries. The full S&P 500 spans all 11 GICS sectors (~160 sub-industries). Pull the current S&P 500 list, enumerate every distinct GICS Sub-Industry present, and extend the rollup map to cover ALL of them. The map must still raise loudly on an unmapped sub-industry (don't silently default) — so this is "run, hit unmapped, add, repeat" until every present sub-industry maps to its GICS Industry.

A2. Regulator coverage check. config/regulators.yaml was authored across sectors but only the Consumer Staples branch was ever exercised. Dry-run the regulator join (Phase 2 Part A logic) against the full company roster WITHOUT calling any LLM, and report any GICS sector/industry that produces ZERO regulators beyond the _default SEC. For each gap, either confirm SEC-only is correct or add the right agencies to regulators.yaml. Print the coverage table (sector → regulator count) for review.

Gate A: rollup map covers 100% of present sub-industries (raises on none); regulator coverage table printed and gaps addressed. STOP and show the coverage table before the expensive parts.

=== PART B — Registry-first ingestion (the critical ordering fix) ===

B1. Build the FULL registry first. Run ingestion across ALL sectors (no --sector filter, or loop all sectors) so data/companies.jsonl contains all ~500 Company nodes BEFORE any resolution happens. Cache all ~500 latest 10-Ks (cache-first; reuse the 36 already cached). This is the ordering guarantee: the registry is complete before Phase 3 ever runs.

B2. Verify registry completeness: ~500 unique CIKs, share-class dedupe applied across the whole index (e.g. GOOGL/GOOG → one Alphabet node, BRK.A/BRK.B → one Berkshire node — these multi-class cases DO exist outside Staples, so confirm they collapse). Print count + any company with no locatable 10-K.

Gate B: ~500 companies in registry, all 10-Ks cached, multi-share-class collapses confirmed. STOP and report counts.

=== PART C — Batched extraction (multiple sessions) ===

C1. Run Phase 2 extraction over all cached filings IN BATCHES (e.g. by sector group), relying on the existing checkpoint so each session resumes where the last stopped. Same claude -p extractor, same verify gate (snippet must be a literal substring AND contain the target), same provenance + extracted_by tagging.

C2. Segment gating now matters: the conglomerate allowlist (config/conglomerates.yaml) was empty for Staples. The full index DOES contain true conglomerates (Berkshire, Alphabet, Amazon, the big diversified industrials). For this first full run, KEEP THE ALLOWLIST EMPTY (do not decompose) — keep MVP scope tight and get the company-level graph first. Note in the output which large multi-segment filers WOULD be candidates for a later segment pass, but do not mint seg: nodes yet.

C3. Track progress: after each batch, print filings processed / remaining, candidates by type, verify-gate accept/reject tally. Do NOT exceed a reasonable per-session volume — pause between batches. This is where the weekly usage ceiling lives.

Gate C: all ~500 filings extracted (across however many sessions it takes); edges_raw.jsonl holds the full candidate set; checkpoint shows 0 remaining. STOP and report totals.

=== PART D — Resolution at scale (single-node invariant under stress) ===

D1. Run Phase 3 resolution over the FULL edges_raw against the FULL registry. Because the registry is now complete (Part B), cross-sector targets must resolve to real cik: nodes: e.g. P&G naming Walmart → cik (Walmart now a filer), chemical suppliers → Materials filers, etc. Provisional slug: nodes should now be FEWER than before relative to graph size (many former slugs are now real filers) — report the slug count and the top remaining slugs (expect genuinely-foreign/private names: Nestlé, Unilever, Mars, Cargill, etc.).

D2. The single-node invariant check is under real stress now (500 companies, far more name fragmentation). It MUST still pass: no two canonical ids share a normalized alias; every fragmentation set collapses to one id. If it fails, STOP and surface the collision — do not write a corrupted graph. Watch especially for cross-sector name clashes (common words, holding-company names).

D3. Directed + undirected dedupe across the larger candidate set; strict 0.75 cutoff; same audit-layer handling.

Gate D: resolution passes the single-node invariant at 500-company scale; cross-sector edges resolve to cik: not slug:; report node/edge/slug counts and the invariant PASS.

=== PART E — Build, API, frontend flip ===

E1. Re-run Phase 4 build_graph: load full nodes/edges into econgraph.db, emit graph.json, print full stats. Report: total nodes/edges by type, connected-components (is the all-sector graph one component, or do sectors form loosely-connected clusters?), top-degree nodes across the whole economy (expect Walmart, the big regulators, maybe Amazon/Apple as hubs), and the cross-sector edge count (supplies/customer_of edges whose endpoints are in DIFFERENT GICS sectors — this is the value-chain payoff; quantify it).

E2. Re-run Phase 5 audit-layer load + confirm the API serves the full graph; /health shows the new counts; customer_of_rows_in_db still 0.

E3. Frontend: flip OPEN_MODE to "search" (500 nodes on open is illegible). The app now opens lean — search-focused — and builds outward via click-expand / double-click-recenter. Verify the full graph is navigable this way. Optionally add a sector filter/legend to the UI, but the core change is just the open mode.

Acceptance test (must pass before you stop):
   - ~500 companies in the graph across all 11 GICS sectors.
   - Single-node invariant PASSES at full scale (printed).
   - CROSS-SECTOR VALUE CHAIN proven: show at least one supplies/customer_of chain crossing sector boundaries — e.g. a Materials chemical supplier → P&G (Staples) → Walmart (Staples/Discretionary retail), or a Tech supplier → a manufacturer. Quantify total cross-sector edges.
   - Former provisional slugs that are real S&P filers now resolve to cik: (report the before/after slug reduction).
   - Frontend opens in search mode and navigates the 500-node graph fluidly; the three original behaviors (expand, re-center, provenance-on-edge-click) still work.
   - /health: full counts, customer_of_rows_in_db = 0.
   - Print the full stats (esp. cross-sector edge count + components), confirm the invariant, and STOP. Summarize the state of the full-economy graph and what the post-MVP roadmap (commodity/region nodes, Wikidata enrichment, news-driven highlighting) now unlocks on top of it.
```

---

## Notes for you (not part of the prompt)

- **The gates exist because this run is long and partly unattended.** Gate A (coverage maps) and Gate B (full registry) are cheap and must finish before the expensive extraction — getting them wrong means re-running 1,000 model calls. Don't skip ahead; the ordering is the whole point.
- **Registry-first, restated because it's everything:** if you extract+resolve sector-by-sector, a P&G→PepsiCo edge found while only Staples is loaded won't see Pepsi, mints `slug:pepsi`, and later collides with the real Pepsi filer — two PepsiCos, invariant violated, value chain broken. Building all 500 company nodes first means every cross-sector target has a real home to resolve to. This is the single change that makes multi-sector *work* rather than just *run*.
- **Expect the slug population to drop sharply relative to scale.** Much of today's grey halo becomes real `cik:` nodes once all sectors are present (Walmart, Pepsi, the suppliers). What remains slug should be genuinely foreign/private — Nestlé, Unilever, Mars, Cargill, Koch. That residual is your real Wikidata-enrichment target list, now clearly identified.
- **The cross-sector edge count is the number to celebrate.** It's the literal measure of your original vision starting to exist: relationships that cross industry boundaries are the value chains a single-sector graph couldn't show. When the stats print "N cross-sector supplies edges," that N is the MVP-to-vision bridge.
- **Conglomerates stay un-decomposed this run, on purpose.** Berkshire/Alphabet/Amazon will look slightly incoherent as single nodes (Berkshire "competing" with both insurers and railroads). That's the known cost of keeping scope tight; the segment machinery (allowlist + `seg:` nodes + `part_of`) is already built and waiting for a dedicated later pass. Don't turn it on mid-scale-out.
- **Usage pacing is real.** ~1,000 `claude -p` calls across all filings will span sessions and could brush your weekly limit. Batch by sector group, pause, resume via checkpoint. If you hit a limit, the checkpoint means you lose nothing — just continue next session. This is exactly why we built extraction resumable back in Phase 2.
- **After this:** you'll have a full-economy, single-sector-proven, cross-sector-connected graph — the substrate your original news-driven vision needs. The next real move is Wikidata enrichment (promote the residual slugs + add the commodity/region nodes the schema already supports), and then the event→subgraph highlighting. None of it is a rewrite. The MVP became the platform.

When the cross-sector stats land — especially that cross-sector edge count and the invariant holding at 500 companies — send them over. That's the moment the consumer-staples proof becomes a map of the whole index.
