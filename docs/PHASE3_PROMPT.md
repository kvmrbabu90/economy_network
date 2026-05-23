# Phase 3 — Resolution (Claude Code prompt)

The phase where raw target strings collapse onto canonical nodes and the single-node invariant becomes real. Run from the repo root in a **fresh** Claude Code session. Phases 0–2 must be green. This phase is deterministic — **no LLM calls, no `claude -p`, no API.** It's string matching, an alias table, and graph assembly.

**Decisions locked with the user (build to these exactly):**
1. **Unmatched competitor names → mint a provisional `slug:` node and keep the edge**, flagged low-confidence/unverified-identity. Don't drop, don't queue.
2. **Auto-match high-confidence; queue only genuine ambiguities** to `data/review_queue.jsonl`. The graph builds from confident matches without waiting on review.
3. **Strict confidence threshold** — only high-confidence edges enter the graph. But the first run prints the confidence histogram and the would-be-dropped list before locking the exact cutoff value (strict is the direction; the data sets the number).

---

```
Read CLAUDE.md, docs/PRD.md, and the Phase 3 notes fully before doing anything. This is Phase 3 (resolution). It is DETERMINISTIC — do NOT use claude -p, the Agent SDK, Ollama, or any LLM. Do NOT build API or frontend (later phases). The job: turn data/edges_raw.jsonl (CandidateEdges with raw string targets) into validated Edge records with canonical source+target ids, enforcing the single-node invariant.

Inputs: data/edges_raw.jsonl (405 CandidateEdges), data/companies.jsonl (36 Company Nodes), data/regulator_nodes.jsonl (7 Regulator Nodes), data/filings/<cik>/<accession>.txt (available if needed for disambiguation).
Outputs: data/nodes.jsonl (all canonical Nodes: companies + regulators + newly-minted non-filer nodes), data/edges.jsonl (validated Edge records), data/aliases.jsonl (the alias table), data/review_queue.jsonl (ambiguous matches for human review), and a printed resolution report.

=== PART A — The canonical registry + alias table ===

A1. Build the registry from known nodes. Load the 36 Company Nodes and 7 Regulator Nodes as the seed canonical registry. For each, seed alias entries: the registered name, every ticker, and obvious normalized variants (lowercased, punctuation-stripped, "Inc"/"Corporation"/"Company"/"Co"/"Ltd"/"S.A."/"GmbH" suffixes removed, "&"/"and" normalized, "Wal-Mart"→"walmart"). Persist to data/aliases.jsonl as {alias_normalized, canonical_id, source:"seed"}.

A2. Normalization function. Write one shared normalize(name) used everywhere: lowercase, strip legal suffixes, collapse punctuation/whitespace, normalize unicode (Nestlé→nestle). This function is the backbone — every match goes through it. Unit-test it on the known Walmart fragments ("Walmart", "Walmart Inc.", "Walmart, Inc.", "Wal-Mart Stores, Inc.", "Walmart Stores, Inc.") all normalizing equal.

=== PART B — Resolve targets ===

For every CandidateEdge target_raw, resolve to a canonical id in this order:

B1. regulated_by edges: target is already a canonical regulator: id — pass through unchanged.

B2. Exact/normalized match against the registry+alias table → high-confidence auto-match. Record the match and (if new) append the alias to aliases.jsonl. The Walmart fragments must all resolve to the single cik for Walmart here.

B3. Fuzzy match for near-misses (e.g. token-set ratio above a high bar, ~90+). If exactly one registry candidate clears the bar → auto-match. If TWO OR MORE clear it, or the best is in an ambiguous band → DO NOT GUESS: write the candidate to data/review_queue.jsonl with the top suggestions and their scores, and skip it for now (the edge waits on review, it does not enter the graph mis-merged).

B4. No registry match → mint a provisional non-filer node:
   - id = slug:<normalized-kebab> (e.g. slug:red-bull, slug:nestle). NOT a cik.
   - type=Company, name=cleaned original string, metadata.provisional=true, metadata.identity_unverified=true, metadata.first_seen_filing=<accession>.
   - Reuse the same slug for repeat names (so two filers naming "Red Bull" point at ONE slug node — single-node invariant holds for non-filers too).
   - Keep the edge, but mark its confidence as capped/low and record metadata noting the target is provisional.
   - Append the slug+alias to aliases.jsonl so future runs resolve consistently.

Single-node invariant check: after resolution, assert no two distinct canonical ids share a normalized alias, and that every Walmart fragment maps to one id. Fail loudly if violated.

=== PART C — Build edges, dedupe, threshold ===

C1. Construct validated Edge records (the real Edge model from schema/models.py, not CandidateEdge) with canonical source_id + target_id, carrying provenance through from the candidate. supplies stays directed source→target. regulated_by stays directed company→regulator.

C2. competes_with dedupe on the UNORDERED pair. This can only happen now, post-resolution: if Kraft Heinz's filing named Kellanova AND Kellanova's named Kraft Heinz, that is ONE undirected competes_with edge, not two. Collapse {A,B} duplicates, keeping the highest confidence and merging provenance (list both source snippets). Store one row per unordered pair.

C3. Confidence threshold — STRICT, but report before locking. On this first run:
   - Print a histogram of edge confidence across all resolved edges (bands of 0.1).
   - Apply a strict default cutoff (start at 0.75) and print exactly which edges fall below it (source, target, type, confidence) so the user can confirm or adjust the number.
   - Edges below the cutoff are written to data/edges_below_threshold.jsonl (not discarded — auditable), NOT to edges.jsonl.
   - Make the cutoff a config value so it's a one-line change after the user sees the distribution.
   - Note: provisional slug-target edges are capped low by B4, so strict will mostly exclude them from the main graph — that's the intended effect (keep the high-confidence core clean, retain the rest for audit).

=== PART D — Emit + report ===

D1. Write data/nodes.jsonl (all canonical nodes: 36 companies + 7 regulators + N minted slug nodes), data/edges.jsonl (validated, deduped, above-threshold Edges), data/aliases.jsonl, data/review_queue.jsonl, data/edges_below_threshold.jsonl.

D2. Print a resolution report:
   - candidates in → edges out (with drop reasons: queued, below-threshold, deduped)
   - count auto-matched vs queued vs provisional-slug
   - the Walmart collapse: N raw fragments → 1 canonical id (show it explicitly)
   - competes_with pairs before/after dedupe
   - confidence histogram + below-cutoff list
   - node counts by type
   - assert + print PASS on the single-node invariant check

Acceptance test (must pass before you stop):
   - All Walmart fragments collapse to one canonical id (printed explicitly).
   - No two canonical ids share a normalized alias (invariant check passes).
   - At least one provisional slug node exists for a real non-filer competitor (e.g. Nestlé/Red Bull/Danone) and repeat mentions reuse the same slug.
   - competes_with edges are deduped on unordered pairs (before/after counts shown).
   - edges.jsonl validates as real Edge records with canonical ids on both ends; no raw strings remain as targets.
   - The confidence histogram + below-threshold list are printed.
   - review_queue.jsonl contains only genuine ambiguities (may be empty or small — that's fine).
   - Re-running is stable (same inputs → same canonical assignments; alias table makes it deterministic).
   - Print the full report and STOP. Summarize what Phase 4 (graph build) will consume.
```

---

## Notes for you (not part of the prompt)

- **This is the phase that makes "one node, many viewpoints" real.** Up to now it's been a schema rule; here the Walmart fragments actually collapse and the invariant gets asserted in code. The printed invariant check (no two canonical ids sharing a normalized alias) is your proof it held.
- **On the strict threshold:** the run applies strict but shows you the histogram and the exact edges it would drop at 0.75. Look at that list once — if the gate is cutting real edges you'd want, nudge the config number; if it's cleanly separating the provisional/low-confidence junk from the solid core, leave it. One look, then it's locked.
- **Provisional slug nodes are a feature, not debt.** They record what the filings actually said about non-US competitors (Nestlé, Danone, Red Bull) without pretending you've verified their identity. Post-MVP Wikidata enrichment can upgrade `slug:nestle` to a real `wikidata:` id, and because every filer that named Nestlé already points at that one slug, the upgrade is a single relink — the single-node design pays off again.
- **The review queue shouldn't block you.** If it's empty or tiny, great — the consumer-staples names are mostly clean. It exists so that the day a genuinely ambiguous name appears (a bare surname, a name matching two filers), the resolver flags it instead of silently picking wrong.
- **Why no LLM here:** resolution is string matching and registry lookup — deterministic, reproducible, auditable. Adding a model would make it non-reproducible for no gain. Keep it pure code.

When this gate is green, Phase 4 is the graph build — load nodes/edges into SQLite, emit `graph.json`, print graph stats, and (per our earlier note) drop a throwaway `scripts/preview.html` so you get your first static look at the actual consumer-staples network two phases before the real renderer. That's the moment you'll see whether the shape matches what's in your head.
