# EconGraph — Session Handoff Document
*Written 2026-05-25. Covers everything built, the current codebase state, and what's next.*

---

## 1. What EconGraph Is

A single, queryable, directed, typed graph of the global economy.

- **Nodes**: Company, Commodity, Region, Regulator (+ Provisional for non-filers not yet verified)
- **Edge types**: `supplies` (A→B directed), `competes_with` (undirected), `regulated_by` (Company→Regulator)
- **`customer_of` is derived**: never stored. It's the reversal of `supplies` computed at query time.
- **Every edge has provenance**: filing accession, URL, verbatim snippet, extracted_by tag.

Current scale: **5,328 nodes**, **18,528 edges** (13,435 core + 5,093 audit/inferred).

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

### Phase F: Exchange-Index Global Expansion (complete 2026-05-25)
- **1,449 new companies** from 9 major stock exchanges (NSE India 187, TSE Japan 329,
  LSE UK 276, FSE Germany 60, KRX Korea 195, ASX Australia 230, SSE China 170,
  HKEX 65, TWSE Taiwan 150)
- Strategy: Wikidata SPARQL P:17 (HQ country) + P:414 (any exchange listing)
- Wikipedia LLM extraction: ~73 new edges from Phase F companies
- Wikidata P1830 competitor enrichment: +1,088 competes_with edges
- **Final graph: 5,328 nodes, 18,528 edges, largest component 5,223 nodes**
- Supply layer: 6,090 supplies edges (up from 4,194)
- New file: `pipeline/ingest_phase_f.py`
- Config: 36 new overrides in `company_sub_industry_overrides.yaml`; 4 new entries in
  `gics_subindustry_to_industry.yaml`
- TypeScript fix: `render3d.ts` TS2448 (apiNode used before declaration)
- Acceptance tests: all 6 geography tests PASS (India ≥100, UK ≥100, Germany ≥30,
  Japan ≥80, Korea ≥100, Australia ≥100)

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

## 8. Phase F (Complete) — Exchange-Index Global Expansion

See Phase History above for full details. Key stats: 2,862 companies in companies.jsonl
(up from 1,253 pre-Phase F), 5,328 graph nodes, 18,528 edges, 6,090 supply edges.

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

## 9. Phase E (Backlog) — Geography-Aware Impact Reasoning

Root cause: LLM assigns benefit to Tyson Foods when "Chic-fil-A enters India" because the `supplies` edge is US-scoped but has no geographic metadata.

- **Track A** (prompt fix, ~1 day): add geography-reasoning step to `api/impact.py`; instruct LLM to check supplier country vs. event geography before assigning a positive verdict.
- **Track B** (data fix, ~3-4 days): add `supply_geography` field to Edge schema + SQLite; extract from filing context (default "US" for 10-K).

---

## 9. Running the Stack

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

## 10. Open Questions / Decisions Needed

1. **Phase D**: should the country-default markets be additive (base markets UNION country markets) or restrictive (base markets INTERSECTION country markets)? Current plan: additive union capped by the industry base list. If a Korean telco's industry_to_retail is [us, eu], it should stay [us, eu, kr, jp, cn, sea] not lose US.
2. **Phase B follow-up**: 6 companies in `foreign_filers.yaml` commented out (Tata Motors, CBD, Westpac, MercadoLibre, Yum China, BeiGene) — all addressable via Phase B Wikidata path. Do now or defer?
3. **Phase E Track A**: prompt is a 1-day change. Recommended to deploy before Track B.
