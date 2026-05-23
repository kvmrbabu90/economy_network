# Phase 5 — API (Claude Code prompt)

The graph becomes queryable. A FastAPI service over `econgraph.db` exposing the PRD §8 endpoints, whose one piece of real logic is the invariant we've built toward from the first diagrams: **`customer_of` is derived at query time by reversing `supplies`, never stored.** Run from the repo root in a **fresh** session. Phases 0–4 green. **No LLM, no network egress** — it's a read API over local SQLite.

---

```
Read CLAUDE.md, docs/PRD.md (esp. §8), and the Phase 5 notes fully before doing anything. This is Phase 5 (API). It is a READ-ONLY service over econgraph.db. Do NOT build the Sigma.js frontend (Phase 6). Do NOT use any LLM/claude -p/API. Do NOT mutate the graph. The one non-trivial rule: customer_of is NEVER stored and ALWAYS derived on read (invariant #2).

Stack: FastAPI + Uvicorn + Pydantic. Read econgraph.db via the existing schema/store.py connection. All responses graphology-compatible: { "nodes": [{key, attributes}], "edges": [{key, source, target, attributes}] } so Phase 6 loads them directly.

=== PART A — Store read layer + audit completeness ===

A1. The edges table currently holds only the 204 above-threshold core edges; the 156 below-threshold audit edges (provisional-slug competitors etc.) live only in data/edges_below_threshold.jsonl. To make a "show provisional" toggle possible, add a `below_threshold` BOOLEAN column to the edges table (default 0), and load the audit edges from edges_below_threshold.jsonl flagged below_threshold=1. Core edges stay below_threshold=0. Referential integrity must still hold (their slug targets are already nodes). Keep this idempotent. After: edges table = 204 core + 156 audit = 360 rows, flagged.

A2. Add read helpers to a query module (read-only, parameterized SQL — no string interpolation): get_node(id), get_node_edges(id, types, include_provisional), search(q), get_edge(id), bfs_subgraph(seed, hops, types, include_provisional). These do the SQLite work; the API layer is thin over them.

=== PART B — The customer_of derivation (the heart of this phase) ===

B1. Invariant: the DB contains ONLY edge types supplies | competes_with | regulated_by. customer_of is synthesized on read:
   - For a stored supplies edge (source=A, target=B) — "A supplies B" — the derived edge is (source=B, target=A, type=customer_of) — "B is a customer of A".
   - When a query's type filter includes customer_of, reverse the relevant supplies edges and emit them as customer_of edges with a synthetic key like "derived:customer_of:<underlying_supplies_edge_id>".
   - Add an assertion/health check the test can call: zero rows in the edges table have type=customer_of. If any exist, fail loudly.

B2. Direction semantics for an ego query on node N (document these in code):
   - suppliers of N  = sources of stored supplies edges where target=N (incoming supplies)
   - customers of N  = targets of stored supplies edges where source=N (outgoing supplies); exposed as derived customer_of edges
   - competitors of N = competes_with neighbors (undirected)
   - regulators of N = targets of regulated_by where source=N

=== PART C — Endpoints (PRD §8) ===

GET /node/{id}
   → node detail (attributes from nodes table). 404 if unknown. If id is an alias not a canonical id, resolve via the alias table and return the canonical node (or 404).

GET /node/{id}/ego?types=supplies,customer_of,competes_with,regulated_by&include_provisional=false
   → center node + 1-hop neighbors along the requested types. Default types = all four. customer_of is derived per Part B. Default include_provisional=false (clean high-confidence core only); =true also returns below_threshold edges and the provisional slug nodes they reach. Returns graphology {nodes, edges}.

GET /subgraph?seed={id}&hops=2&types=...&include_provisional=false
   → BFS from seed up to `hops` (CAP at 3; reject higher with 400) along requested types, customer_of derived. Cap total returned nodes (e.g. 500) to prevent blowups; if capped, set a "truncated": true flag in the response.

GET /search?q=procter
   → resolve against the alias table (normalized) + node names; return ranked candidate nodes (id, name, type, score). This is where the 392-entry alias table earns its keep — "procter", "p&g", "PG" all resolve to the one canonical P&G node.

GET /edge/{id}
   → edge detail incl. full provenance (filing, url, snippet, extracted_by, additional_provenance for merged competes_with). For a derived customer_of key, resolve to the underlying supplies edge and return ITS provenance, noting the edge is a derived view.

C1. Enable CORS for localhost origins (Phase 6's browser app runs on a different port and must call this API). Allow GET from localhost:*.

C2. Add GET /health returning {status, node_count, edge_count, customer_of_rows_in_db (must be 0)}.

=== PART D — Tests + run ===

D1. pytest via FastAPI TestClient covering every endpoint. Required tests:
   - /node/{P&G cik} returns P&G; /node/procter (alias) resolves to the same canonical node.
   - /node/{P&G}/ego returns P&G's Walmart supplies edge, its regulated_by edges, and its competes_with neighbors.
   - THE WALMART TEST: /node/{Walmart cik}/ego?types=customer_of returns derived customer_of edges equal in count to Walmart's incoming supplies edges, with NO customer_of rows existing in the DB. Assert the derivation matches the reverse of stored supplies exactly.
   - /search?q=procter → P&G top hit.
   - /subgraph?seed={P&G}&hops=2 returns a valid bounded 2-hop graph; hops=4 → 400.
   - include_provisional=false excludes slug nodes; =true includes Nestlé/Unilever and their edges.
   - /health reports customer_of_rows_in_db = 0.
   - /edge/{a competes_with id} returns provenance; /edge/{derived customer_of key} resolves to the underlying supplies provenance.

D2. Provide the run command (uvicorn ... --reload) and a couple of example curl calls in the output.

Acceptance test (must pass before you stop):
   - All endpoints respond with valid graphology-shaped JSON.
   - The Walmart customer_of derivation works and the DB has zero stored customer_of rows (invariant #2 proven live).
   - Alias search resolves "procter"/"p&g" to the canonical P&G node.
   - include_provisional toggle changes results as expected.
   - CORS headers present for localhost.
   - All tests green. Print the test output, the run command, and example curls, then STOP. Summarize what Phase 6 (the Sigma.js frontend) will call.
```

---

## Notes for you (not part of the prompt)

- **This is the phase that makes "supplier and customer are one edge seen from two ends" real.** It's been a schema rule (no `customer_of` in the enum), then a resolution constraint; now it's a live query. Reverse Walmart's inbound `supplies` and you've reconstructed "who supplies Walmart" — and equivalently, each of those suppliers can ask "am I a supplier to Walmart?" — from the *same* stored edges, zero duplication. That's the whole idea executing.
- **Why I added the audit layer to the DB here (Part A):** Phase 4 left the 156 below-threshold edges out of SQLite (only in `graph.json`). To let the API honor a `include_provisional` toggle — which Phase 6 will want as the "show the grey halo" control you saw in the preview — those edges need to be queryable. Flagging them in the edges table (rather than a separate store) keeps one source of truth. Default stays clean-core; provisional is opt-in.
- **The default is the trustworthy graph.** With `include_provisional=false`, the API serves only high-confidence, grounded, above-threshold edges between real or confidently-resolved entities. That's the graph you can stand behind. The provisional layer is there when you want completeness over certainty — explicitly, never by accident.
- **CORS matters more than it looks.** Phase 6's browser app will run on a different localhost port than the API; without CORS the fetches silently fail and you'll waste time debugging the frontend when the problem is the API. It's a one-liner; the prompt makes it non-optional.
- **Read-only by design.** The API never writes to the graph. Refreshes happen by re-running the pipeline (Phases 1–4), not through the API. Keeping it read-only means you can't corrupt the graph by querying it.

When Phase 5 is green, Phase 6 is the payoff: the real Sigma.js frontend — search a node, see its ego-graph, click a neighbor to expand it in place, toggle edge types and the provisional layer, click an edge to read its provenance. The acceptance test for Phase 6 is the one you've been waiting for since the start: reproduce, live and by clicking, the three diagrams we drew by hand — Costco-centered, P&G-centered, and the connected value chain — from the real graph. Send me the Phase 5 results, especially the Walmart `customer_of` test and the zero-stored-rows health check, and I'll write it.
