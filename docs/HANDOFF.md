# EconGraph — Session Handoff Document
*Last updated 2026-06-08. Covers everything built, current state, and what's next.*

---

## 1. What EconGraph Is

A single, queryable, directed, typed graph of the global economy.

- **Nodes**: Company, Commodity, Region, Regulator (+ Provisional for unverified non-filers)
- **Edge types**: `supplies` (A→B directed), `competes_with` (undirected), `regulated_by` (Company→Regulator)
- **`customer_of` is derived**: never stored — computed at query time by reversing `supplies`
- **Every edge has provenance**: filing accession, URL, verbatim snippet, `extracted_by` tag

Current scale: **5,334 nodes · 18,558 edges** (13,465 core + 5,093 audit/inferred)

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
api/main.py                    ← FastAPI: /ego, /subgraph, /search, /edge, /impact, /describe, /news
api/impact.py                  ← BFS impact propagation engine (Claude CLI or Ollama)
api/news.py                    ← Daily headline filtering (Claude CLI only)
web/src/                       ← Vite + TypeScript + Sigma.js (2D) + three.js globe
```

**Data flow**: `ingest → extract/wikidata/regulators/commodities → resolve → build_graph → API → Frontend`

Each pipeline stage reads the previous stage's JSONL files from `data/`. Re-runnable independently.

---

## 3. Phase History

| Phase | Date | Summary |
|---|---|---|
| 0–6 | — | Core MVP: S&P 500 extraction, Sigma.js 2D, inspector, search, provenance |
| 7 | — | Full S&P 500 all 11 sectors; co-mention + Wikidata competitor enrichment; 8-K scraper |
| A | 2026-05-25 | 67 foreign 20-F filers (Toyota, Samsung, Shell, TSMC, Alibaba, ASML…) |
| B | 2026-05-25 | 686 Wikidata non-filers; Wikipedia LLM extraction; P1830 competitor edges |
| C | 2026-05-25 | 60 new regulator nodes (93 total); `config/country_regulators.yaml` for ~40 countries |
| D | 2026-05-25 | Country-aware retail routing; 7 new region nodes; `country_default_retail_markets.yaml` |
| E | 2026-05-25 | Geography-aware impact reasoning; `supply_geography` field; GEOGRAPHY RULE in LLM prompt |
| F | 2026-05-25 | +1,449 companies from 9 exchanges (NSE, TSE, LSE, FSE, KRX, ASX, SSE, HKEX, TWSE) |
| G | 2026-05-28 | Market-movers curation (CATL, Novo Nordisk, NIO…); impact engine ×3 speed; archive restore fixes |
| H | 2026-05-29 | Globe load 20–30 s → ~2 s; `_bulkLoadInProgress` flag; deferred arc builds; skip FA2 in globe mode |
| I | 2026-06-07 | Dev env stability; globe arc fixes; morning brief reliability; news filter hardening |
| J | 2026-06-08 | Repo shareability (README, .env.example, requirements.txt, start.sh, GitHub Release); news freshness |

---

## 4. Running the Stack

**Windows** — double-click `dev.bat`. Kills stale listeners on 8101/5180, opens backend and frontend in separate cmd windows.

**Mac / Linux** — `chmod +x start.sh && ./start.sh` (equivalent behavior).

```bash
# Backend — binds IPv4+IPv6 wildcard for Windows dual-stack
python -m uvicorn api.main:app --host :: --port 8101 --reload

# Frontend dev server
cd web && npm run dev -- --port 5180

# Frontend production build
cd web && npm run build   # outputs to web/dist/
```

**Ports**: backend `8101`, frontend dev `5180`.
`web/.env.development` sets `VITE_API_BASE_URL=http://localhost:8101`. A full Vite restart (not just HMR) is required when this file changes.

**New contributor setup**: see `README.md` at repo root — clone → `pip install -r requirements.txt` → `npm install` → download `econgraph.db` from the [v1.0.0 GitHub Release](https://github.com/kvmrbabu90/economy_network/releases/tag/v1.0.0) → `cp .env.example .env` → `./start.sh`.

---

## 5. Key Files & Their Roles

| File | Role |
|---|---|
| `web/src/main.ts` | App entry; Sigma wiring, loaders, interactions, impact, archive, morning brief |
| `web/src/graph.ts` | Graphology instance; merge, layout, restyle |
| `web/src/style.ts` | Node/edge color + size (single source of truth) |
| `web/src/render3d.ts` | 3D/Globe renderer; `unpremultiply()` for edge colors |
| `web/src/impact.ts` | `buildImpactState`, `tintColor`, `dimColor` |
| `web/src/impact-archive.ts` | localStorage 24h archive |
| `web/src/news.ts` | `fetchHeadlines()` — 60 s timeout, 3× retry with backoff |
| `web/src/ui/filters.ts` | Filter state; fallback to ALL_EDGE_TYPES when no chips |
| `web/src/bubbles.ts` | Bubble sector nodes; `isBubble()`, `ensureBubbleNodes()` |
| `api/impact.py` | BFS propagation; Claude CLI or Ollama; `RING_PARALLELISM=8`, `MAX_RING_CANDIDATES=24` |
| `api/news.py` | RSS fetch → Claude CLI filter → cached per calendar day |
| `config/retail_markets.yaml` | 14 consumer-market region nodes |
| `config/country_regulators.yaml` | Country → regulator list |
| `pipeline/commodities.py` | Commodity + region node builder + candidate edge generator |

---

## 6. Known Invariants (Do Not Break)

1. `customer_of` is never stored — derived by reversing `supplies` at query time.
2. One node per entity — no duplicates for "different vantage points."
3. Every edge has `provenance` with `extracted_by`, `filing`, `snippet`.
4. LLM edges: snippet must literally contain the named target entity.
5. Canonical ID format: `cik:NNNN` | `wikidata:Qxxxx` | `slug:kebab` | `regulator:slug` | `commodity:slug` | `region:slug`
6. Bubble nodes (`bubble:` prefix) must never appear in the 3D renderer.
7. Edge colors for Sigma must be premultiplied (`toRgba` in `style.ts`).
8. Edge colors for three.js must be un-premultiplied (`unpremultiply` in `render3d.ts`).
9. **`manual:curation` extracted_by**: hand-curated edges use `extracted_by="manual:curation"` (not `"manual"`). `"manual"` requires a non-empty `filing` + `url`; `"manual:curation"` does not. Both `schema/models.py` and the SQLite CHECK constraint enumerate this value.
10. **`add_market_movers.py` must run after `build_graph.py`** — or its nodes must already be in `data/nodes.jsonl`, `data/edges.jsonl`, `data/aliases.jsonl`. `build_graph.py` wipes and rebuilds `econgraph.db`; DB-only inserts are lost. The script writes to all three JSONL files automatically.
11. **LLM calls must use Claude CLI subprocess** (`claude -p ... --output-format json`), never the Anthropic Python SDK (`import anthropic`). The SDK creates separate pay-per-token billing outside the Claude Max plan.

---

## 7. Frontend Architecture Notes

### Sigma 2D Renderer
- **Node sizing**: fixed per-type radii (Regulator=10, Region=8, Commodity=6, Company=4, Provisional=3). `[`/`]` keys scale via `nodeScale` multiplier.
- **Edge color**: premultiplied alpha — `toRgba()` writes `rgba(r*a, g*a, b*a, a)` because Sigma uses `gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA)`. Alpha values: core=0.55, inferred=0.25, dim=0.08.
- **Impact overlay**: impacted nodes get tint + 1.8× size + `zIndex=10`. Non-impacted: 0.45× size + `#1e2228`.

### 3D Globe
- `render3d.ts` backed by `3d-force-graph` + three.js. Globe nodes pinned at HQ lat/lon via Wikidata.
- `.linkPositionUpdate((_obj) => true)` suppresses the library's default mesh transform (world-space tube geometry fix).
- Arc colors un-premultiplied before passing to three.js: `unpremultiply(attrs.color)`.
- Arcs hidden in idle state (`_applyLinkTint(null)` hides all). Arcs only visible during an active impact trace.
- Arc builds deferred until first impact fires — 13k TubeGeometry objects never built on idle globe load.

### Full-Graph Cache (3-tier)
1. Already in full view → `cameraReset()` only (instant)
2. Cache warm, different graph active → restore from `_fvResponse` + `_fvPositions` (skip FA2)
3. Cold → fetch + FA2, snapshot positions into `_fvPositions`
Cache key: `${includeProvisional}:${includeInferred}`

### Impact Archive
- `web/src/impact-archive.ts` — 24h TTL localStorage archive
- Restore: full graph first, then re-applies tinting. No LLM call needed.

---

## 8. Phase G (Complete 2026-05-28) — Market-Movers Curation + Impact Engine

### New Provisional Nodes (scripts/add_market_movers.py)
6 new `manual:curation` nodes: **CATL**, **Novo Nordisk**, **Roche Holding**, **NIO**, **Li Auto**, **XPeng**. 30 new edges covering lithium supply chain, EV competitor web, and GLP-1 competitor web. Persisted to all three JSONL pipeline files.

### Impact Engine Speed
`RING_PARALLELISM` 3→8, `MAX_RING_CANDIDATES` 12→24. Same `cwd=tmpdir` CLAUDE.md isolation guard applied to `api/news.py` (was missing).

### Other G Fixes
- Archive restore: `resetGlobeCamera()` + `showEmpty()` on restore (washed-out globe + stale inspector fixes)
- Globe cache fast mode: `fast=true` on IDB hit → NODE_BATCH=2000, EDGE_BATCH=4000 (~2–3 s)
- Market filter change during active impact trace: tint re-applied at 150/350/750 ms after `update3D()`

**Graph after Phase G: 5,334 nodes, 13,465 core edges, 18,558 total.**

---

## 9. Phase H (Complete 2026-05-29) — Globe Load Performance

**Problem**: first globe open took 20–30 s. Three root causes:
1. ~8 intermediate `update3D()` calls during batch load (each rebuilt 13k TubeGeometry arcs)
2. FA2 layout (6+ s) running unnecessarily in globe mode (nodes use lat/lon pins, not FA2 positions)
3. 13k arc objects built at `start3D()` even in idle state

**Fixes**:
- `_bulkLoadInProgress` flag → 8+ intermediate `update3D()` → 1 final
- FA2 guarded with `if (!is3DRunning())` at all call sites
- Arc builds guarded by `_currentImpactState !== null` → built on-demand at first impact

**Result**: globe startup 20–30 s → **~2–3 s**.

---

## 10. Phase I (Complete 2026-06-07) — Dev Environment Stability + Globe Rendering Fixes

### 10.1 API port fix
`web/.env.development` had `VITE_API_BASE_URL=http://localhost:8001` (stale). Updated to `8101`. Fallback in `web/src/config.ts` updated to match.

### 10.2 Windows IPv6 dual-stack
Uvicorn `--host 0.0.0.0` only binds IPv4; `localhost` on Windows resolves to `::1`. Fix: `--host ::` (IPv6 wildcard, also serves IPv4 on dual-stack).

### 10.3 Globe arc geometry (world-space tubes)
`3d-force-graph` was double-transforming link meshes (library default transform + TubeGeometry world-space coords). Fix: `.linkPositionUpdate((_obj) => true)` suppresses the library transform.

### 10.4 Arc zoom distortion
- **Bug A**: `src.x/y/z` are random before first d3 tick; globe nodes have `fx/fy/fz` set immediately. Fix: read `fx/fy/fz` first, fall back to `x/y/z` only if `typeof fx !== "number"`.
- **Bug B**: `onWheel` scaled link mesh X/Y to maintain tube thickness; distorts world-space arcs. Fix: `onWheel` link scaling guarded by `currentLayout !== "globe"`.

### 10.5 `Number.isFinite` guard
Switched from global `isFinite` to `Number.isFinite` via `.every()` for all 6 arc coordinates. `isFinite(null)` coerces null→0 (arc appears at globe center); `Number.isFinite(null)` returns false.
```typescript
if (![sx, sy, sz, ex, ey, ez].every(Number.isFinite)) { skipped++; return; }
```

### 10.6 Morning brief reliability
- **Startup warmup** (`api/main.py`): daemon thread pre-warms headlines cache at boot
- **60 s timeout** (`web/src/news.ts`): extended from 15 s for cold-cache Claude CLI calls
- **3× retry** with 8 s / 16 s / 32 s backoff; `AbortError` detected via `err?.name === "AbortError"` (not `instanceof DOMException` — fails cross-realm)
- **`_warmup_lock`**: module-level `threading.Lock()` prevents duplicate warmup threads within a process; each `--reload` spawns a fresh process so the lock resets cleanly

### 10.7 News filter prompt hardening
Added 5 new exclusion categories to `_FILTER_PROMPT` in `api/news.py`:
1. **Geopolitical without commodity**: requires explicit commodity + mechanism (e.g., "Hormuz closure cuts oil flow"). "Missiles fired" alone → exclude.
2. **Political/government behavior**: politician interviews, speeches, walk-outs, press conferences → exclude even if economic topic is mentioned.
3. **Analysis/explainer patterns**: "Inside the…", "What to know about…", "Why…", "How…", "explained" → exclude.
4. **Speculative language**: "may", "could", "might", "is expected to" with no confirmed action → exclude.
5. **Vague competitive posturing**: "X eyes Y's market" with no deal or contract named → exclude.

Extended loaded-word blocklist: `storms, fragile, in jeopardy, at risk, reportedly, allegedly, sources say, braces, reeling, scrambles`. Added QUALITY BAR: return `[]` rather than padding with borderline items.

### 10.8 Commits
| Commit | Summary |
|---|---|
| `4516282` | fix .env.development API port 8001 → 8101 |
| `ba9a0f6` | fix uvicorn --host :: for Windows dual-stack |
| `5f3e022` | suppress 3d-force-graph link transform (arc world-space fix) |
| `2015733` | eliminate cold-cache "Headlines unavailable" (warmup + retry) |
| `db4bc3e` | globe arc stable on zoom — fx/fy/fz priority + skip link scaling in globe mode |
| `78566ea` | AbortError name check + warmup lock comment + arc 6-coord guard |
| `a17a6b8` | Number.isFinite via .every() + corrected warmup-lock docstring |
| `9c704d3` | add dev.bat startup script |
| `4875e35` | tighten news filter: 5 new exclusion categories, extended blocklist, quality bar |
| `5bd11df` | docs: HANDOFF.md Phase I update |

---

## 11. Phase J (Complete 2026-06-08) — Repo Shareability + News Freshness

### 11.1 GitHub Release
`econgraph.db` (12.5 MB SQLite graph) published as a GitHub Release asset at:
`https://github.com/kvmrbabu90/economy_network/releases/tag/v1.0.0`

The DB is gitignored (too large for the repo). New contributors download it once from the Release and place it in the repo root.

### 11.2 New setup files
| File | Purpose |
|---|---|
| `README.md` | Complete rewrite: quick-start, LLM comparison table (Claude vs Ollama), architecture diagram, rebuild-from-scratch pipeline, key invariants |
| `.env.example` | All env vars with defaults and comments; copy to `.env` and pick provider |
| `requirements.txt` | Flat `pip install -r requirements.txt` (8 direct deps) |
| `start.sh` | Mac/Linux launcher; checks for econgraph.db, kills stale listeners on 8101/5180, opens two terminals |

Ollama path was already built in `api/impact.py` (`IMPACT_LLM_PROVIDER=ollama`). The README + `.env.example` make it discoverable. Morning brief is Claude-only (noted in comparison table).

### 11.3 News staleness fix
**Problem**: `_MAX_ARTICLE_AGE_DAYS = 5` let articles from earlier in the week (e.g., ADP payrolls released Wednesday) recirculate through Saturday — the brief appeared unchanged for multiple days.

**Fix**: reduced to `_MAX_ARTICLE_AGE_DAYS = 2`; matching recency rule in `_FILTER_PROMPT` also updated from "5 days" to "2 days". Force-refreshed immediately — verified new distinct headlines.

### 11.4 Commits
| Commit | Summary |
|---|---|
| `82042e2` | docs: add README, .env.example, requirements.txt, start.sh |
| `35160e8` | fix(news): tighten staleness window 5 days → 2 days |

---

## 12. Open Issues

### Active
1. **Impact BFS silent node drop** (`api/impact.py` ~line 1001): when a ring-scoring LLM call returns empty JSON, all nodes in that chunk are silently dropped with `continue` — no retry. Same pattern exists in the refinement path (~line 1469). Observed live: ~16 hop-2 nodes lost in one run. Fix: 1–2 retries when `ring_parsed` fails `isinstance(…, list)` check.

### Deferred / decided
2. **Globe idle edge density**: arcs hidden in idle state (Phase G fix). A "show top-N supply edges" toggle could be useful but is low priority.
3. **IDB cache key does not include graph version**: if `build_graph.py` is re-run, the browser cache still serves old data for up to 24 h. Workaround: DevTools → Application → IndexedDB → `econgraph-cache` → Clear.
4. **Phase B follow-up**: 6 companies in `foreign_filers.yaml` commented out (Tata Motors, Westpac, MercadoLibre, Yum China, BeiGene) — all addressable via Wikidata path. Deferred.

---

## 13. Pipeline Rebuild Order

```bash
python -m pipeline.commodities        # commodity + retail-market nodes
python -m pipeline.extract            # TRUNCATES edges_raw.jsonl — run FIRST
python -m pipeline.extract_wikipedia  # APPENDs wikipedia edges
python -m pipeline.wikidata_phase_b   # APPENDs P1830 competitor edges
python -m pipeline.resolve
python -m pipeline.build_graph
# restart uvicorn on port 8101
```

**Note**: `scripts/add_market_movers.py` persists to JSONL files — no need to re-run separately after `build_graph.py`.
