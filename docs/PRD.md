# EconGraph — Product Requirements, Architecture & Build Plan

**Version:** 0.1 (MVP spec)
**Owner:** You
**Build tool:** Claude Code
**Last updated:** 2026-05-23

---

## 1. Vision

A single, queryable, directed graph of the economy. Every node is an economic entity — a company, a commodity, a material, or a region. Every edge is a typed, directed relationship: *supplies → , buys-from → , competes-with — , regulated-by → .* Any node can be viewed as the center of its own "four players" diagram (supplier / customer / regulator / competitor), and the same node simultaneously appears as a sub-node in other nodes' views. There is no duplication: P&G is one node that has a `supplies` edge to Costco and a `competes_with` edge to Kimberly-Clark.

**The end goal (NOT the MVP):** event-driven impact mapping. When news breaks — "new crude oil site discovered in Texas," "bird flu rising" — the system identifies the seed nodes (a commodity, a region) and traverses the typed edges to highlight every value chain that propagates from that shock. This is only reachable if the schema supports non-company nodes from day one, which is why the MVP schema below already includes `Commodity`, `Material`, and `Region` node types even though the MVP only densely populates `Company`.

**MVP definition of done:** an interactive, pannable graph of a hand-scoped slice (consumer staples, ~20–40 companies) where you can click any company node, see its four-category neighbors, expand a neighbor into its own ego-graph, and filter by edge type — all built from free public data (SEC EDGAR 10-Ks, the S&P 500 list, Wikidata).

---

## 2. Scope

### In scope (MVP)
- Canonical entity registry with stable IDs and de-duplication (the "node is also a sub-node" guarantee).
- Four edge types: `supplies`, `customer_of`, `competes_with`, `regulated_by`. (`supplies` and `customer_of` are inverse views of the same underlying relationship; store one, derive the other.)
- Free-source ingestion: S&P 500 constituent list, company metadata, 10-K filings.
- LLM-assisted extraction of relationships from 10-K free text.
- Rules-based regulator assignment (industry → agency).
- A graph API exposing ego-graphs and n-hop subgraph queries.
- A web front end that renders, expands, filters, and searches the graph.
- Schema headroom for `Commodity` / `Material` / `Region` nodes (defined, lightly populated).

### Out of scope (MVP — explicitly deferred)
- Truly exhaustive supplier/customer lists. We capture **material** relationships only (named customers >10% of revenue per SEC disclosure rules, named competitors, top disclosed suppliers). "Exhaustive" is a later, paid-data problem.
- Real-time news ingestion and automated impact propagation (this is the end-goal phase).
- Quantitative edge weights (revenue share, volume). Capture where trivially available; don't depend on them.
- Authentication, multi-user, persistence of user-created views.
- The full S&P 500. We prove the pipeline on one sector first.

### Honest data-reality note
The four categories are **not** equally available from free sources. Competitors and regulators are tractable (sector codes, 10-K "Competition" sections, industry→regulator rules). Suppliers and customers are hard — companies don't publish full vendor lists. The reliable free signal is the **inter-company B2B edge** reconstructed from 10-K customer-concentration disclosures. Build expecting sparse but high-confidence supplier/customer edges, and denser competitor/regulator edges.

---

## 3. Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| F1 | Resolve any company name/ticker/CIK to one canonical node | Must |
| F2 | Store typed, directed edges with a source citation (filing accession + URL) | Must |
| F3 | Return the ego-graph of any node (center + 4-category neighbors) via API | Must |
| F4 | Return an n-hop subgraph from a seed node, filterable by edge type | Must |
| F5 | Render a node's ego-graph in the browser; click a neighbor to expand it in place | Must |
| F6 | Filter the visible graph by edge type (toggle suppliers/customers/competitors/regulators) | Must |
| F7 | Full-text node search (by name/ticker) | Must |
| F8 | Every edge surfaces its provenance (which filing it came from) on hover/click | Should |
| F9 | Support `Commodity`/`Material`/`Region` nodes in schema and renderer | Should |
| F10 | Confidence score per extracted edge (LLM extraction certainty) | Could |

---

## 4. Data Model

The whole project lives or dies on this. Get the schema right before writing ingestion.

### Node
```jsonc
{
  "id": "cik:0000080424",        // canonical, prefixed by source authority
  "type": "Company",             // Company | Commodity | Material | Region | Regulator
  "name": "The Procter & Gamble Company",
  "aliases": ["P&G", "Procter & Gamble", "PG"],
  "tickers": ["PG"],
  "identifiers": {
    "cik": "0000080424",         // SEC
    "wikidata": "Q170298",
    "lei": "..."                 // optional
  },
  "sector": "Consumer Staples",  // GICS
  "industry": "Household Products",
  "country": "US",
  "metadata": { "...": "source-specific" }
}
```

### Edge
```jsonc
{
  "id": "uuid",
  "source": "cik:0000080424",    // P&G
  "target": "cik:0000909832",    // Costco
  "type": "supplies",            // supplies | competes_with | regulated_by
  "directed": true,
  "confidence": 0.82,            // 0–1, from extractor
  "provenance": {
    "filing": "0000080424-24-000123",
    "url": "https://www.sec.gov/...",
    "snippet": "short quote/locator, NOT full text",
    "extracted_by": "llm:claude" // or "rule" | "manual"
  },
  "weight": null                 // optional revenue share etc.
}
```

### Edge-type rules
- **`supplies`** (A → B): A sells goods/services to B. The inverse "B `customer_of` A" is *derived at query time*, never stored separately — this prevents the duplication that would break the "single node" guarantee.
- **`competes_with`** (A — B): symmetric in meaning, but store one directed row and treat as undirected in queries. De-dupe on the unordered pair.
- **`regulated_by`** (A → R): A is a `Company`/`Commodity`, R is a `Regulator` node. Mostly rule-generated from industry.

### The "node is also a sub-node" mechanism
There is exactly one row per entity in the node table. "P&G appears in Costco's view" and "P&G is its own center" are both just **queries against the same node and its edges** — the first is `edges WHERE target = costco`, the second is `edges WHERE source = pg OR target = pg`. No copies, no special-casing. This is the single most important design decision in the project.

### Identifier strategy (entity resolution)
- **Primary key authority:** SEC **CIK** for any SEC filer. Format IDs as `cik:0000080424`.
- **Non-filers** (private suppliers, foreign competitors, commodities): use `wikidata:Qxxxx`, else `slug:crude-oil`.
- Maintain an **alias table** so "P&G", "Procter and Gamble", "PG" all resolve to `cik:0000080424`. This is where most ingestion bugs will live — budget for it.

---

## 5. Data Sources (all free)

| Source | What it gives | Access |
|--------|---------------|--------|
| Wikipedia "List of S&P 500 companies" | Constituent list, ticker, sector, sub-industry, CIK | Scrape one HTML table; refresh occasionally |
| SEC EDGAR | 10-K filings (the relationship goldmine), company metadata, CIK | Free REST + bulk; `edgartools` Python lib wraps it well |
| 10-K Item 1 "Business" / "Competition" | Named competitors, business description | Parse from filing text |
| 10-K MD&A / Item 1A | Customer concentration (named customers >10% of revenue) | Parse from filing text |
| Wikidata (SPARQL) | Cross-IDs, parent/subsidiary, industry, country, commodity ontology | Public SPARQL endpoint |
| Industry → regulator mapping | `regulated_by` edges | Hand-authored YAML you maintain |

**SEC compliance:** EDGAR requires a descriptive `User-Agent` header (your name + email) and rate limits to ~10 requests/sec. Bake a polite rate-limiter and cache into the fetcher from line one. Cache every filing to disk — never re-fetch.

**Extraction approach:** the structured fields (CIK, sector) come from tables. The *relationships* come from free text via an LLM call: feed the "Competition" paragraph and MD&A customer section to Claude with a strict JSON-output schema (the Edge shape above), and parse the result. This is the natural place Claude does the heavy lifting, and it's also where you enforce confidence scoring and provenance.

---

## 6. Architecture

```
┌─────────────┐   ┌──────────────┐   ┌─────────────────┐   ┌──────────────┐   ┌───────────────┐
│  INGESTION  │ → │  EXTRACTION  │ → │   RESOLUTION    │ → │  GRAPH STORE │ → │   API (REST)  │
│ EDGAR/Wiki  │   │ LLM + rules  │   │ canonical IDs   │   │ SQLite +     │   │  FastAPI      │
│ fetch+cache │   │ text → edges │   │ + alias table   │   │ graphology   │   │  ego/n-hop    │
└─────────────┘   └──────────────┘   └─────────────────┘   └──────────────┘   └───────┬───────┘
                                                                                       │
                                                                              ┌────────▼────────┐
                                                                              │    FRONTEND     │
                                                                              │ Sigma.js +      │
                                                                              │ graphology      │
                                                                              │ expand/filter   │
                                                                              └─────────────────┘
```

**Layered, file-based, restartable.** Each stage writes artifacts to disk so you can re-run any stage independently. This matters enormously when building agentically — Claude Code can rebuild stage 3 without re-running the expensive stage-1 fetch.

- **Stage 1 — Ingestion:** pull the S&P list, resolve CIKs, fetch + cache 10-Ks as raw files. Output: `data/filings/{cik}/{accession}.txt` + `data/companies.jsonl`.
- **Stage 2 — Extraction:** for each filing, rule-extract structured fields and LLM-extract relationship edges. Output: `data/edges_raw.jsonl` with provenance + confidence.
- **Stage 3 — Resolution:** normalize names → canonical IDs, build alias table, de-dupe edges, drop unresolved/low-confidence. Output: `data/nodes.jsonl`, `data/edges.jsonl`.
- **Stage 4 — Graph store:** load nodes/edges into SQLite (source of truth) and build a graphology serialization for the API/front end. Output: `econgraph.db`, `graph.json`.
- **Stage 5 — API:** FastAPI reads the store, serves ego-graph and n-hop endpoints.
- **Stage 6 — Frontend:** Sigma.js renders; calls the API on node expansion.

---

## 7. Tech Stack & Tooling

| Layer | Choice | Why |
|-------|--------|-----|
| Language (pipeline) | Python 3.11+ | Best ecosystem for filings/data |
| EDGAR access | `edgartools` (or `sec-edgar-downloader`) | Handles CIK lookup, filing fetch, parsing |
| LLM extraction | Anthropic API (Claude), JSON-mode prompts | Free-text → typed edges |
| Graph store (truth) | SQLite | Zero-ops, file-based, perfect for MVP scale |
| In-memory graph | `networkx` (pipeline) / `graphology` (JS) | Traversal + serialization |
| API | FastAPI + Uvicorn | Fast, typed, trivial to stand up |
| Frontend | Vite + TypeScript + **Sigma.js** + graphology | WebGL canvas scales to thousands of nodes; graphology is the shared graph model |
| Layout | ForceAtlas2 (graphology-layout-forceatlas2) | Standard force-directed economic-graph layout |
| Validation | Pydantic (Python), Zod (TS) | Schema enforcement on both sides |
| Tests | pytest, Vitest | |

**Why Sigma.js over Cytoscape.js:** Cytoscape is richer for small graphs, but your end goal is "highlight every chain affected by a shock," which means thousands of nodes lit up at once. Sigma's WebGL renderer handles that; Cytoscape's SVG/canvas does not. Start on the renderer that scales.

**Repo layout:**
```
econgraph/
├── CLAUDE.md                 # project rules for Claude Code (see §9)
├── pyproject.toml
├── data/                     # gitignored artifacts (cached filings, jsonl)
├── config/
│   └── regulators.yaml       # industry → regulator rules
├── pipeline/
│   ├── ingest.py             # stage 1
│   ├── extract.py            # stage 2 (rules + LLM)
│   ├── resolve.py            # stage 3
│   └── build_graph.py        # stage 4
├── api/
│   └── main.py               # FastAPI
├── web/                      # Vite + TS + Sigma front end
└── tests/
```

---

## 8. API Surface (MVP)

```
GET /node/{id}                          → node detail
GET /node/{id}/ego?types=supplies,competes_with
                                        → center + filtered 1-hop neighbors
GET /subgraph?seed={id}&hops=2&types=…  → n-hop traversal
GET /search?q=procter                   → resolved node candidates
GET /edge/{id}                          → edge detail incl. provenance
```

All responses are graphology-compatible `{ nodes: [...], edges: [...] }` so the front end loads them directly.

---

## 9. Build Plan (Claude Code)

**Setup.** Install Claude Code — the native installer is the recommended path and needs no Node.js: on macOS/Linux/WSL `curl -fsSL https://claude.ai/install.sh | bash`; on Windows PowerShell `irm https://claude.ai/install.ps1 | iex`. (The npm route `npm install -g @anthropic-ai/claude-code` still works but requires Node.js 18+.) You'll need a paid Anthropic account or API key. Verify with `claude doctor`. Authoritative docs: https://docs.claude.com/en/docs/claude-code/overview

**First, write `CLAUDE.md`** at the repo root before any code. It should encode: the data model in §4 verbatim, the "single node, no duplication" rule, the layered file-based-artifacts principle, the SEC User-Agent + rate-limit requirement, and "every edge must carry provenance." Claude Code reads this on every run, so the invariants live there, not in your memory.

Work phase by phase. Each phase is a self-contained Claude Code session with a clear deliverable and an acceptance test. Do **not** let it build ahead — the schema decisions in early phases must settle before extraction.

### Phase 0 — Scaffold & schema (½ day)
- Prompt: *"Scaffold the repo per the layout in the PRD. Implement the Node and Edge schemas as Pydantic models and Zod types. Write a SQLite schema with nodes, edges, and aliases tables. No business logic yet."*
- **Done when:** models validate the example P&G/Costco records from the PRD; `pytest` passes on schema round-trips.

### Phase 1 — Ingestion (1–2 days)
- Prompt: *"Build `pipeline/ingest.py`: fetch the S&P 500 list from Wikipedia, resolve each to a CIK via EDGAR, fetch the latest 10-K, cache to `data/filings/`. Polite User-Agent + ≤10 req/s. Filter to one sector via a `--sector` flag."*
- **Done when:** running with `--sector "Consumer Staples"` produces ~35 cached 10-Ks and a `companies.jsonl` with CIK + GICS for each.

### Phase 2 — Extraction (2–3 days, the hard part)
- Prompt: *"Build `pipeline/extract.py`. (a) Rule-extract the Item 1 'Competition' section and MD&A customer-concentration section from each cached 10-K. (b) Send those sections to the Claude API with a strict JSON schema matching the Edge model; extract `supplies` and `competes_with` edges with confidence + a provenance snippet. (c) Generate `regulated_by` edges from `config/regulators.yaml` keyed on GICS industry. Output `edges_raw.jsonl`."*
- **Done when:** P&G's filing yields a `competes_with` edge to at least Kimberly-Clark/Colgate and a `customer_of`/`supplies` edge involving Walmart, each with a real provenance snippet.

### Phase 3 — Resolution (1–2 days)
- Prompt: *"Build `pipeline/resolve.py`: normalize every edge endpoint name to a canonical node ID using the company registry + an alias table; create new non-filer nodes (`wikidata:` or `slug:`) for unmatched competitors; de-dupe `competes_with` on unordered pairs; drop edges below a confidence threshold (configurable). Output `nodes.jsonl` + `edges.jsonl`."*
- **Done when:** "P&G", "Procter & Gamble", "The Procter & Gamble Company" all collapse to one node ID; no duplicate competitor edges.

### Phase 4 — Graph store (1 day)
- Prompt: *"Build `pipeline/build_graph.py`: load nodes/edges into SQLite and emit a graphology `graph.json`. Add a CLI to print graph stats (node/edge counts by type, degree distribution)."*
- **Done when:** stats show a connected consumer-staples graph; the P&G/Costco/Kimberly-Clark triangle from our diagrams is present and correct.

### Phase 5 — API (1 day)
- Prompt: *"Build `api/main.py` with the endpoints in PRD §8. `customer_of` is derived from `supplies` at query time, never stored."*
- **Done when:** `GET /node/cik:.../ego` returns P&G's four-category neighborhood; `GET /subgraph?seed=…&hops=2` returns a valid 2-hop graph.

### Phase 6 — Frontend (3–4 days)
- Prompt: *"Build the Vite+TS Sigma.js app: search box → load a node's ego-graph; click a neighbor → fetch and merge its ego-graph in place; edge-type filter toggles; color nodes by type and edges by relationship (teal supplies, purple customer, coral competitor, blue regulator — match the PRD palette); ForceAtlas2 layout; provenance on edge click."*
- **Done when:** you can reproduce, live and interactively, the three diagrams we built by hand (Costco-centered, P&G-centered, and the connected value chain) by clicking through the real graph.

### Phase 7 — Demo hardening (1–2 days)
- Tighten the consumer-staples slice, fix the worst entity-resolution misses by hand in the alias table, write a README, record a walkthrough. **This is your MVP.**

---

## 10. Path to the End Goal (post-MVP, for context)

Once the MVP graph is solid, event-driven impact mapping is an additive module, not a rewrite — which is exactly why the schema already carries `Commodity`/`Material`/`Region` nodes:

1. **Enrich the ontology:** add commodity/material nodes (crude oil, poultry, semiconductors) and `produces` / `consumes` / `located_in` edges linking companies to commodities and regions. Wikidata's commodity and supply-chain ontology is a free starting point.
2. **News ingress:** an event → entity-linking step (an LLM call) maps a headline to seed nodes ("bird flu" → `slug:poultry`; "Texas oil site" → `slug:crude-oil` + `region:US-TX`).
3. **Propagation:** a weighted BFS/DFS from the seed nodes along directed edges, with decay per hop, produces the "impacted subgraph" — which the existing Sigma front end already knows how to highlight.

The MVP intentionally builds the substrate (typed directed graph + traversal API + scalable renderer) that this module plugs into. Don't build the news module until the graph it traverses is trustworthy.

---

## 11. Key Risks & Decisions

| Risk | Mitigation |
|------|------------|
| Supplier/customer edges too sparse from free data | Accept it for MVP; lean on the high-confidence >10%-customer disclosures; mark this the boundary where paid data (Bloomberg SPLC, FactSet) would later earn its cost |
| Entity resolution errors poison the graph | Confidence thresholds + a hand-maintained alias table + provenance on every edge so errors are auditable, not silent |
| LLM extraction hallucinates relationships | Require a verbatim provenance snippet per edge; reject edges whose snippet doesn't contain the named entity; keep confidence scores |
| "Exhaustive" expectation vs. readable graph | Curate to material relationships; exhaustive ≠ useful past a few hundred visible nodes |
| Scope creep into the news module | Hard gate: news module is forbidden until Phase 7 demo passes |

---

## 12. Immediate Next Action

Create the repo, drop this file in as `docs/PRD.md`, author `CLAUDE.md` from §4 + §6 invariants, then run the Phase 0 prompt. Stop after each phase's acceptance test before moving on.
