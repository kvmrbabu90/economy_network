# CLAUDE.md — EconGraph project rules

You are building EconGraph: a single, queryable, directed, typed graph of the economy. Read this file fully before every task. These are invariants, not suggestions. When a request conflicts with them, stop and flag it.

## What this project is
One graph. Nodes are economic entities (companies, commodities, materials, regions, regulators). Edges are typed and directed: `supplies`, `competes_with`, `regulated_by`. A node viewed as the center of its own "four players" diagram and the same node appearing inside another node's diagram are **the same node** — they are just two different queries. There is never a second copy.

## Hard invariants (do not violate)
1. **One row per entity.** Every entity has exactly one canonical node. "P&G as a center" and "P&G inside Costco's view" are queries against the same node + its edges. Never duplicate a node to represent a different vantage point.
2. **`customer_of` is derived, never stored.** Store only `supplies` (A → B). The customer view (B buys from A) is computed at query time by reversing `supplies`. Storing both would create the duplication invariant #1 forbids.
3. **Every edge carries provenance.** No edge enters the graph without `provenance` (filing accession + URL + a verbatim snippet locator) and an `extracted_by` value (`llm` | `rule` | `manual`). An edge with no source is a bug.
4. **LLM-extracted edges must be grounded.** Reject any extracted edge whose provenance snippet does not literally contain the named target entity. Keep the confidence score. Hallucinated relationships are the top failure mode — guard against them at extraction time.
5. **Layered, file-based, restartable artifacts.** Each pipeline stage reads the previous stage's files from `data/` and writes its own. Never collapse stages into one monolith. I must be able to re-run stage 3 without re-running stage 1.
6. **Never re-fetch what is cached.** All EDGAR filings are cached to disk on first fetch. Always read from cache if present.
7. **SEC politeness is mandatory.** Every EDGAR request sends a descriptive `User-Agent` (name + email, from config) and the fetcher rate-limits to ≤10 requests/second. No exceptions.
8. **Canonical ID format.** SEC filers: `cik:0000080424`. Non-filers: `wikidata:Qxxxx`, else `slug:crude-oil`. Regulators: `regulator:<slug>`. IDs are stable and lowercase-prefixed.
9. **Schema is fixed in code.** Node and Edge shapes are defined as Pydantic models (Python) and Zod types (TS) and validated at every boundary. Don't pass loose dicts across stages.
10. **Schema headroom is intentional.** `Commodity`, `Material`, `Region` node types exist in the schema even though the MVP only densely populates `Company`. Do not remove them as "unused" — the post-MVP news module depends on them.

## Node types
`Company | Commodity | Material | Region | Regulator`

## Edge types
`supplies` (A→B, directed) · `competes_with` (treat as undirected, de-dupe on unordered pair) · `regulated_by` (A→Regulator, mostly rule-generated)

## Pipeline stages (each is a separate runnable)
1. `pipeline/ingest.py` — S&P list + CIK resolution + 10-K fetch/cache
2. `pipeline/extract.py` — rule + LLM extraction → `data/edges_raw.jsonl`
3. `pipeline/resolve.py` — canonicalize, alias-resolve, de-dupe, threshold → `data/nodes.jsonl` + `data/edges.jsonl`
4. `pipeline/build_graph.py` — load SQLite + emit `graph.json`
5. `api/main.py` — FastAPI ego/n-hop/search endpoints
6. `web/` — Vite + TS + Sigma.js renderer

## Build discipline
- Work one phase at a time. Do not build ahead of the current phase. Schema and extraction decisions must settle before scaling.
- Each phase has an acceptance test (see PRD §9). Do not declare a phase done until its test passes.
- The news/impact-propagation module is **forbidden** until the Phase 7 MVP demo passes.
- Curate to **material** relationships. "Exhaustive" is explicitly out of scope; do not attempt to enumerate every supplier.

## Color palette (frontend, keep consistent with hand-built diagrams)
- `supplies` edges → teal · `customer_of` (derived view) → purple · `competes_with` → coral · `regulated_by` → blue · neutral/center node → gray

## When unsure
Stop and ask. A wrong schema or a duplicated node is expensive to unwind later; a clarifying question is cheap.
