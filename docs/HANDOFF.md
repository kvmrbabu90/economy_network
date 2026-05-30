# EconGraph — Session Handoff Document
*Last updated 2026-05-29. Covers everything built, the current codebase state, and what's next.*

---

## 1. What EconGraph Is

A single, queryable, directed, typed graph of the global economy.

- **Nodes**: Company, Commodity, Region, Regulator (+ Provisional for non-filers not yet verified)
- **Edge types**: `supplies` (A→B directed), `competes_with` (undirected), `regulated_by` (Company→Regulator)
- **`customer_of` is derived**: never stored. It's the reversal of `supplies` computed at query time.
- **Every edge has provenance**: filing accession, URL, verbatim snippet, extracted_by tag.

Current scale: **5,334 nodes**, **18,558 edges** (13,465 core + 5,093 audit/inferred).

---

## 2. Full Stack Architecture

```
data/companies.jsonl           ← canonical company list (567 entries after Phase A+B)
pipeline/ingest.py             ← CIK resolution, 10-K / 20-F fetch + cache
pipeline/extract.py            ← Rule + LLM extraction → data/edges_raw.jsonl
pipeline/extract_wikipedia.py  ← Phase B: Wikipedia Business/Operations LLM extraction
pipeline/wikidata_phase_b.py   ← Phase B: Wikidata SPARQL + P1830/P1071 edges
pipeline/regulators.py         ← Country-aware regulated_by rules (Phases A+C)
pipeline/commodities.py        ← Commodity nodes + retail-market sink nodes
pipeline/resolve.py            ← Canonicalize, alias-resolve, de-dupe, threshold
pipeline/build_graph.py        ← SQLite load → graph.json (graphology format)
api/main.py                    ← FastAPI: /ego, /subgraph, /search, /edge, /impact, /describe
web/src/                       ← Vite + TypeScript + Sigma.js (2D) + 3D-force-graph
```

### Data flow
```
ingest → extract/wikidata/regulators/commodities → resolve → build_graph → API → Frontend
```
Each stage reads the previous stage's JSONL files from `data/`. Re-runnable independently.

---

## 3. Phase History

### Phase 0–6: Core MVP
- GICS-aware extraction from 10-K filings for full S&P 500
- Rule-based `regulated_by` + `competes_with` (co-mention inference, Wikidata P1830)
- Sigma.js 2D renderer with FA2 layout, sector layout, bubble layout
- Inspector panel, search, edge provenance, node expand/recenter

### Phase 7: Full S&P 500 + LLM at Scale
- All 11 GICS sectors extracted
- Tier 2: co-mention closure inference for `competes_with`
- Tier 3: Wikidata P1830/P1071 enrichment
- Tier C: 8-K customer announcement scraper across all sectors

### Phase A: Foreign 20-F Filers (complete 2026-05-25)
- 67 foreign companies via 20-F filings (Toyota, Samsung, Shell, TSMC, Alibaba, ASML, ...)
- `data/foreign_filers.yaml` — curated 150-entry list; 6 commented out (no 20-F)
- `pipeline/wikidata.py` extended for foreign CIK→Wikidata enrichment
- Graph grew: 500→567 companies, 2,483→2,781 connected nodes

### Phase B: Wikidata Non-Filers (complete 2026-05-25)
- 686 non-US companies via Wikidata SPARQL (top by country/sector)
- `pipeline/wikidata_phase_b.py` + `pipeline/extract_wikipedia.py`
- Wikipedia Business/Operations → LLM extraction (300 verified edges)
- P1830 competitor edges: +399 `competes_with`
- Graph grew to: 3,743 nodes, 12,907 edges

### Phase C: Global Regulators (complete 2026-05-25)
- 60 new regulator nodes (93 total, was 33)
- `config/country_regulators.yaml` covering ~40 countries
- Two-tier routing: wikidata: companies → country-only regs; cik: foreign 20-F → US + country
- Acceptance: Toyota (JP), Toyota-Astra (ID), ASML (NL) all routing correctly

### Phase D: Country-Aware Retail Routing (complete 2026-05-25)
- 7 new region nodes (14 total): Korea, Australia, Canada, Mexico, Middle East, Sub-Saharan Africa, LatAm
- `config/country_default_retail_markets.yaml` for 40+ country codes
- Samsung/Sony/Sanofi acceptance tests pass; 4,194 supplies edges
- `company_sub_industry_overrides.yaml` for Wikidata companies misclassified as Industrial Conglomerates

### Phase E: Geography-Aware Impact Reasoning (complete 2026-05-25)
- `supply_geography` field added to `Edge` Pydantic model + SQLite DDL
- `build_graph.py` infers scope from provenance: LLM/10-K → "US", Wikidata/Wikipedia → "global", rule → null
- `api/impact.py` ring and refinement prompts extended with GEOGRAPHY RULE (country + edge_geo guards)
- DB: 316 US edges, 103 global, 5,671 null (correct by design)
- Acceptance: "Chick-fil-A enters India" → Tyson Foods `no_effect`, India Consumer Market `positive`, 155/187 filtered to `no_effect`
- Commit: f2d8a2a

### Phase F: Exchange-Index Global Expansion (complete 2026-05-25)
- **1,449 new companies** from 9 major stock exchanges (NSE India 187, TSE Japan 329,
  LSE UK 276, FSE Germany 60, KRX Korea 195, ASX Australia 230, SSE China 170,
  HKEX 65, TWSE Taiwan 150)
- Strategy: Wikidata SPARQL P:17 (HQ country) + P:414 (any exchange listing)
- Wikipedia LLM extraction: ~73 new edges from Phase F companies
- Wikidata P1830 competitor enrichment: +1,088 competes_with edges
- **Final graph after Phase F: 5,328 nodes, 18,528 edges, largest component 5,223 nodes**
- Supply layer: 6,090 supplies edges (up from 4,194)
- New file: `pipeline/ingest_phase_f.py`
- Config: 36 new overrides in `company_sub_industry_overrides.yaml`; 4 new entries in
  `gics_subindustry_to_industry.yaml`
- TypeScript fix: `render3d.ts` TS2448 (apiNode used before declaration)
- Acceptance tests: all 6 geography tests PASS (India ≥100, UK ≥100, Germany ≥30,
  Japan ≥80, Korea ≥100, Australia ≥100)

### Phase G: Market-Movers Curation + Impact Engine Hardening (complete 2026-05-28)
See §12 below for full detail.

### Phase H: Globe Load Performance (complete 2026-05-29)
See §13 below for full detail.

---

## 4. Frontend — What Was Built (This Session)

### Sigma 2D Renderer
- **Node sizing**: fixed per-type radii (Regulator=10, Region=8, Commodity=6, Company=4, Provisional=3). `[`/`]` keys scale all uniformly via `nodeScale` multiplier in nodeReducer.
- **Edge color**: monochrome `#7a7e84` (commodity grey). **Premultiplied alpha** encoding fixed a critical WebGL blending bug: Sigma uses `gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA)` (premultiplied), so `toRgba()` now writes `rgba(r*a, g*a, b*a, a)` — hub areas converge to base grey instead of blowing out to white.
- **Alpha values**: core=0.55, inferred=0.25, dim=0.08 (all premultiplied).
- **Impact overlay**: impacted nodes get tint color + 1.8× size + zIndex=10 + forceLabel (limited to hop=0 or magnitude≥0.6). Non-impacted: 0.45× size + color `#1e2228`. Chain edges: steel blue `#4a7a94`. Non-chain: dark `#1c2228` opaque.

### Inspector Panel
- Collapsed by default. Remembered in `localStorage("inspectorCollapsed")`.
- Expands automatically on node click or edge click.
- `setInspectorCollapsed()` at module scope (not inside IIFE) so click handlers can call it.

### Full-Graph Cache (3-tier)
1. Already in full view → `cameraReset()` only (instant)
2. Cache warm, different graph active → restore from `_fvResponse` + `_fvPositions` (skip FA2)
3. Cold → full network + FA2, then snapshot positions into `_fvPositions`
- Cache key: `${includeProvisional}:${includeInferred}` (filter flags change what API returns)

### Impact Archive
- `web/src/impact-archive.ts` — 24h TTL localStorage archive
- Archive tab in sidebar (sidebar tabs: Filters | Archive)
- Restore restores full graph first then re-applies tinting, no LLM call needed

### Layout Modes (2D)
- **Force**: FA2 at 220 iterations
- **By sector**: GICS concentric rings — regulators r=14, commodities r=38, regions r=60, companies r=115 in golden-angle spirals per sector, provisional slugs r=175
- **Bubbles**: sector hubs with expand/collapse toggle

### 3D / Globe View
- `render3d.ts` backed by `3d-force-graph` + three.js
- Globe: HQ lat/lon pinned via Wikidata metadata; great-circle arc tubes
- Bubble nodes filtered out of 3D scene (fixed orphan cluster bug)
- Edge color **un-premultiplied** before passing to three.js: `unpremultiply(attrs.color)` → RGB from `rgb(r/a, g/a, b/a)` — gives commodity grey instead of near-black

### Sidebar
- Relationship checkboxes removed (fallback: all 4 types enabled when no chips in DOM)
- Markets filter: 12 checkboxes (US, EU, UK, JP, CN, IN, KR, TW, SEA, LATAM, MEA, OTHER)
- Audit layers: provisional toggle, inferred toggle
- Layout segmented control: Force / By sector / Bubbles

---

## 5. Key Files & Their Roles

| File | Role |
|---|---|
| `web/src/main.ts` | App entry; Sigma wiring, loaders, interactions, impact, archive |
| `web/src/graph.ts` | Graphology instance; merge, layout, restyle |
| `web/src/style.ts` | Node/edge color + size computation (single source of truth) |
| `web/src/render3d.ts` | 3D/Globe renderer; `unpremultiply()` for edge colors |
| `web/src/impact.ts` | `buildImpactState`, `tintColor`, `dimColor` |
| `web/src/impact-archive.ts` | localStorage 24h archive |
| `web/src/ui/filters.ts` | Filter state; fallback to ALL_EDGE_TYPES when no chips |
| `web/src/bubbles.ts` | Bubble sector nodes; `isBubble()`, `ensureBubbleNodes()` |
| `config/retail_markets.yaml` | 7 consumer-market region nodes (US, EU, CN, IN, JP, BR, SEA) |
| `config/industry_to_retail.yaml` | GICS sub-industry → which markets a B2C company sells into |
| `config/country_regulators.yaml` | Country → regulator list mapping |
| `pipeline/commodities.py` | Commodity + region node builder + candidate edge generator |
| `api/impact.py` | FastAPI impact propagation endpoint (LLM-backed) |

---

## 6. Known Invariants (Do Not Break)

1. `customer_of` is never stored — always derived by reversing `supplies` at query time.
2. One node per entity — no duplicates for "different vantage points."
3. Every edge has `provenance` with `extracted_by`, `filing`, `snippet`.
4. LLM edges: snippet must literally contain the named target entity.
5. Canonical ID format: `cik:NNNN` | `wikidata:Qxxxx` | `slug:kebab` | `regulator:slug` | `commodity:slug` | `region:slug`.
6. Bubble nodes (`bubble:` prefix) must never appear in the 3D renderer.
7. Edge colors for Sigma must be premultiplied (`toRgba` in style.ts).
8. Edge colors for three.js must be un-premultiplied (`unpremultiply` in render3d.ts).
9. **`manual:curation` extracted_by**: hand-curated edges (no SEC filing) must use `extracted_by="manual:curation"` (not `"manual"`). The `"manual"` tag requires a non-empty `filing` + `url`; `"manual:curation"` does not. Both the Pydantic model (`schema/models.py`) and the SQLite CHECK constraint (`schema/store.py`) enumerate this value.
10. **`add_market_movers.py` must run after `build_graph.py`** — or its nodes must already be in the three JSONL pipeline files (`data/nodes.jsonl`, `data/edges.jsonl`, `data/aliases.jsonl`). `build_graph.py` wipes and rebuilds `econgraph.db` from those files; any DB-only inserts will be lost. The script now writes to all three JSONL files automatically.

---

## 7. Phase D — Country-Aware Retail Routing (complete 2026-05-25)

**Goal**: country-aware retail routing. Previously every B2C company routed to the same 7 global markets regardless of where it's headquartered. Phase D fixes this.

**What was built**:
1. **7 new region nodes** added to `config/retail_markets.yaml`: Korea, Australia, Canada, Mexico, Middle East, Sub-Saharan Africa, LatAm (14 total, up from 7)
2. **`config/country_default_retail_markets.yaml`** — 40+ country codes → ordered primary market lists (JP, KR, CN, TW, GB, DE, FR, NL, CH, IN, SG, AU, SA, BR, MX, CA, ...)
3. **Phase D routing in `pipeline/commodities.py`**:
   - US companies: industry_to_retail list unchanged (backward-compatible)
   - Non-US companies: INTERSECTION of industry base markets with country primary markets, plus country-primary additions not in the industry list
   - Fallback to country_primary if intersection is empty (prevents zero retail edges)
4. **`config/company_sub_industry_overrides.yaml`** — routing-only overrides for Wikidata companies misclassified as "Industrial Conglomerates": Samsung → Consumer Electronics, Inditex → Apparel Retail, ASUS → Technology Hardware, etc.
5. **Frontend**: `web/src/ui/filters.ts` expanded AU and NZ into the Asia-Pacific group (SEA chip); chip label updated to "Asia-Pac"

**Acceptance tests (all PASS)**:
- ✅ Samsung Electronics (KR) → Korea, US, EU, China, Japan, SEA (no Brazil, no India)
- ✅ Sony Group (JP) → US, EU, China, Japan, SEA, Korea (no Brazil, no India)
- ✅ Sanofi (FR) → US, EU, China, Japan (no Brazil, no India)
- ✅ Coca-Cola (US) → unchanged: US, EU, China, India, Japan, Brazil, SEA

**Graph stats after Phase D**:
- Supply layer: 4,194 `supplies` edges (up from 3,745)
- Retail edges: 1,356 filer→region (up from 832 at Phase C)
- New region nodes: 14 total (7 new)

**Known limitation**: LVMH (wikidata:Q161086) is not in the dataset (not an SEC filer and not ingested via Phase B Wikidata). The override for it exists in `company_sub_industry_overrides.yaml` so if it's added via Phase B in future, it will route correctly. See Phase E backlog.

---

## 8. Phase F (Complete 2026-05-25) — Exchange-Index Global Expansion

See Phase History above for full details. Key stats: 2,862 companies in companies.jsonl
(up from 1,253 pre-Phase F), 5,328 graph nodes post-Phase F, 18,528 edges, 6,090 supply edges.
*Graph has since grown to 5,334 nodes / 18,558 edges after Phase G curation (§12).*

Pipeline execution order for rebuild:
```bash
python -m pipeline.commodities       # generates commodity/retail nodes
python -m pipeline.extract           # TRUNCATES edges_raw.jsonl — run FIRST
python -m pipeline.extract_wikipedia # APPENDs wikipedia edges
python -m pipeline.wikidata_phase_b  # APPENDs P1830 competitor edges
python -m pipeline.resolve
python -m pipeline.build_graph
# restart uvicorn on port 8101
```

---

## 9. Phase E (Complete 2026-05-25) — Geography-Aware Impact Reasoning

Both tracks shipped simultaneously. See Phase History for full detail.

**Track A** (prompt engineering): Impact ring and refinement prompts now include a GEOGRAPHY RULE
section. The LLM is instructed to assign `no_effect` when a candidate's `country` and `edge_geo`
are both outside the event's geography. Candidate lines now show `country=<ISO-2> | edge_geo=<US|global|?>`.

**Track B** (schema + inference): `supply_geography: Optional[str]` added to `Edge` model and SQLite.
`build_graph.py` populates it via `_infer_supply_geography()` — no re-extraction needed.

**Acceptance test PASS**: "Chick-fil-A enters India" → Tyson Foods `no_effect` (0.0, "US-domestic
poultry supplier; no India operations"); India Consumer Market `positive` (0.85); 155/187 nodes
correctly filtered to `no_effect`.

---

## 10. Running the Stack

```bash
# Backend (from repo root)
uvicorn api.main:app --host 0.0.0.0 --port 8101 --reload

# Frontend (dev)
cd web && npm run dev

# Frontend (production build)
cd web && npm run build    # outputs to web/dist/

# Rebuild pipeline after data changes
python pipeline/commodities.py
python pipeline/resolve.py
python pipeline/build_graph.py
# then restart uvicorn
```

---

## 11. Open Questions / Decisions Needed

1. **Phase D**: should the country-default markets be additive (base markets UNION country markets) or restrictive (base markets INTERSECTION country markets)? Current plan: additive union capped by the industry base list. If a Korean telco's industry_to_retail is [us, eu], it should stay [us, eu, kr, jp, cn, sea] not lose US.
2. **Phase B follow-up**: 6 companies in `foreign_filers.yaml` commented out (Tata Motors, CBD, Westpac, MercadoLibre, Yum China, BeiGene) — all addressable via Phase B Wikidata path. Do now or defer?
3. **Phase E**: complete. No open questions remaining on this phase.
4. **Globe edge density (idle state)**: arcs are now hidden when no impact trace is active (Phase G fix). Consider whether a "show top-N supply edges" toggle would be useful for exploring the graph in globe mode without running a trace.
5. **IDB cache key does not include graph version**: if `build_graph.py` is re-run, the browser cache still serves old data for up to 24 h. No action needed for normal use, but if you need fresh data immediately after a pipeline rebuild, open DevTools → Application → IndexedDB → `econgraph-cache` → Clear.

---

## 12. Phase G (Complete 2026-05-28) — Market-Movers Curation + Impact Engine Hardening

### 12.1 Impact Engine Speed (api/impact.py)
- `RING_PARALLELISM` 3 → **8** (concurrent Claude CLI subprocesses per hop ring)
- `MAX_RING_CANDIDATES` 12 → **24** (entities packed per LLM call; fewer total subprocess launches)
- Safe because commit `b91320f` (CLAUDE.md tempdir fix) was already in place — confirmed in git history before applying.
- Same `cwd=tmpdir` guard applied to `api/news.py`'s `_claude_call()` (was missing, could block news filtering).

### 12.2 New Provisional Company Nodes (scripts/add_market_movers.py)
**6 new nodes** added as `manual:curation` edges, persisted to all three JSONL pipeline files:

| Company | ID | Key role |
|---|---|---|
| NIO Inc. | `wikidata:Q16957870` | Chinese premium EV (NYSE:NIO); aliases: "nio", "NIO", "NIO Inc." |
| Li Auto | `wikidata:Q78793803` | Chinese EV/EREV (NASDAQ:LI) |
| XPeng Inc. | `wikidata:Q66041928` | Chinese EV (NYSE:XPEV) |
| CATL | `wikidata:Q21751798` | World's largest EV battery maker (~37% global share); hub node |
| Novo Nordisk | `wikidata:Q386708` | GLP-1 leader (Ozempic/Wegovy); alias "novo nordisk" resolves |
| Roche Holding | `wikidata:Q212322` | Swiss pharma/diagnostics (Genentech parent) |

BMW Group (`wikidata:Q26678`) was already in the graph; 10 new edges added to it.

**30 new edges**:
- CATL → Tesla, VW Group, BMW, Mercedes, NIO, Li Auto, XPeng (`supplies`, global)
- Albemarle → CATL, SQM → CATL, Ganfeng → CATL (`supplies`, global)
- commodity:lithium/nickel/cobalt/graphite → CATL (`supplies`, global)
- NIO ↔ XPeng, Li Auto, BYD Auto, Tesla (`competes_with`)
- Li Auto ↔ XPeng, BYD Auto; XPeng ↔ BYD Auto (`competes_with`)
- Novo Nordisk ↔ Eli Lilly, AstraZeneca, Roche Holding (`competes_with`)
- Roche ↔ Pfizer, Novartis, AstraZeneca (`competes_with`)
- BMW ↔ Mercedes, VW Group, Tesla (`competes_with`)

**Graph after Phase G: 5,334 nodes (+6), 13,465 core edges (+30), 18,558 total.**

### 12.3 Schema: `manual:curation` Extracted-By Tag
- **`schema/models.py`**: `"manual:curation"` added to `extracted_by` Literal. NOT in `REQUIRES_SOURCE` — does not require a `filing` or `url`. Snippet (human justification) is still required.
- **`schema/store.py`**: SQLite CHECK constraint extended to include `'manual:curation'`.
- All 30 new edges use this tag.

### 12.4 Archive Restore Bug Fix (web/src/main.ts + render3d.ts)
Two bugs fixed on `restoreFromArchive()`:
1. **Globe washed out**: `resetGlobeCamera()` now called on restore (was missing), returning camera to lat=30, lon=−90 default view.
2. **Stale inspector**: `showEmpty()` called before re-applying the trace, so the inspector never shows data from a previous interaction.

### 12.5 Globe Cache Load Speed (web/src/main.ts + graph-cache.ts)
- **Fast mode** added to `_batchLoadGraph(nodes, edges, onProgress, fast=false)`:
  - `fast=true` (IDB cache hit): NODE_BATCH=2000, EDGE_BATCH=4000 → ~4 yields (~2–3 s)
  - `fast=false` (cold API fetch): NODE_BATCH=150, EDGE_BATCH=600 → ~67 yields, granular progress counter (unchanged)
- `showGraphOverlay(fixedMessage?)`: accepts an optional static message. Cache-hit paths show `"Restoring graph…"` instead of the rotating "Fetching nodes from economy network…" animation.
- Tiers 2, 2.25, and 2.5 all pass `fast=true`.

### 12.6 Globe Edge Visibility (web/src/render3d.ts)
Two bugs fixed:

**Bug A — market filter change un-tinted arcs**  
`update3D()` calls `instance.graphData()` which replaces every link object with a fresh THREE primitive — no tint applied. If an impact trace was active, switching market filters would make all edges re-appear at default colors. Fix: `update3D()` now schedules `_applyLinkTint(_currentImpactState)` at 150 ms / 350 ms / 750 ms (after arc-rebuild at rAF / 100 ms / 600 ms).

**Bug B — idle globe showed all 18k edges**  
`_applyLinkTint(null)` (called on trace clear or globe reset) previously made all edges visible. With 13k+ core + 5k audit tubes, the globe was unreadable under "All Markets". Fix: `state=null` now hides all arcs. Arcs are only visible during an active impact trace (both endpoints must be impacted).

---

## 13. Phase H (Complete 2026-05-29) — Globe Load Performance

### Problem
First open of the dashboard (globe mode) had 20–30 s of lag before the scene was interactive. Three root causes identified:

1. **8+ intermediate `update3D()` calls during batch load** — the RAF-debounced `nodeAdded`/`edgeAdded` listeners fire once per batch yield (one `setTimeout(0)` → one RAF tick → one `update3D()`). Each `update3D()` in globe mode: disposes all TubeGeometry arcs, replaces all link objects via `graphData()`, then schedules 3 more `buildArcs` passes. With ~8 batches at NODE_BATCH=2000 / EDGE_BATCH=4000, that's ~8 arc disposals + rebuilds of 13k TubeGeometry objects during load.

2. **FA2 layout (6+ s) running in globe mode** — globe nodes are pinned at HQ lat/lon via `fx/fy/fz`. The 220-iteration FA2 pass computes 2D `x/y` positions that the globe never reads. Pure wasted CPU on every globe load.

3. **13k TubeGeometry arcs built at `start3D()` mount** — `buildArcs` was scheduled at rAF/100ms/600ms unconditionally. In idle mode, `_applyLinkTint(null)` hides every arc immediately — so building them was pure overhead.

### Fix 1 — `_bulkLoadInProgress` flag (`web/src/main.ts`)
```typescript
let _bulkLoadInProgress = false;
// In _schedule3DUpdate:
if (!is3DRunning() || _3dUpdateScheduled || _bulkLoadInProgress) return;
// In g.on("cleared"):
if (is3DRunning() && !_bulkLoadInProgress) update3D(g, filters);
// In _batchLoadGraph:
_bulkLoadInProgress = true;
try { g.clear(); ...batches... }
finally {
  _bulkLoadInProgress = false;
  if (is3DRunning()) update3D(g, filters); // ONE final sync
}
```
Result: 8+ intermediate `update3D()` calls → **1** at load completion.

### Fix 2 — Skip FA2 in globe mode (`web/src/main.ts`)
All `applyLayout()` / `runLayout()` call sites (Tier 2.25, Tier 2.5, Tier 3, `recenterOn`, `expandFrom`) now guarded with `if (!is3DRunning())`. Globe loads skip the 6+ s FA2 pass entirely; nodes use lat/lon pins. FA2 runs normally when the user is in 2D mode.

Note: if the user opens in globe mode (always true in the current landing flow), 2D positions won't be pre-computed. Switching to 2D shows unordered nodes until the user clicks the "Force" layout tab. This is acceptable — the 2D path is secondary.

### Fix 3 — Defer arc builds until impact fires (`web/src/render3d.ts`)
`buildArcs` scheduling in both `start3D()` and `update3D()` is now guarded by `_currentImpactState !== null`:
- **`start3D`**: only schedules buildArcs at rAF/100ms/600ms if an impact trace is already active (e.g., 2D→3D switch while trace is running). In idle mode, skips all three passes.
- **`update3D`**: only schedules `__rebuildArcs` if `_currentImpactState` is non-null. When `applyImpact3D` fires, it sets `_currentImpactState` BEFORE calling `update3D`, so arc building proceeds correctly on first impact.

Result: 13k TubeGeometry objects are never built during idle globe loads. They're built on-demand the first time an impact trace fires.

### Combined effect
| Phase | Before | After |
|---|---|---|
| Batch load `update3D()` calls | ~8 | 1 |
| Arc builds per `update3D()` | 3 passes × 13k geoms | 0 (idle) / 3 passes (with impact) |
| FA2 on globe load | 6+ s synchronous | 0 ms (skipped) |
| **Total globe startup lag** | **20–30 s** | **~2–3 s** |

### Files changed
- `web/src/main.ts`: `_bulkLoadInProgress` flag; FA2 guards in Tiers 2.25/2.5/3, `recenterOn`, `expandFrom`
- `web/src/render3d.ts`: `buildArcs` scheduling guarded in `start3D()` and `update3D()`
