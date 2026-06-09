# EconGraph — Financial Grounding Design
*Created 2026-06-08. Approved for implementation.*

## Problem

Two related weaknesses make EconGraph less useful than it could be:

1. **Impact scores are arbitrary.** "magnitude 0.30" is a free LLM guess with no anchor. The same query run twice may return different scores, and the number has no financial interpretation.
2. **Graph edges have no weight.** All `supplies` edges are equal — a relationship representing 40% of a company's revenue looks identical to one representing 0.5%.

## Goal

Ground impact scores in real financial data. "magnitude 0.22" should mean "approximately 22% of this company's revenue is exposed through this relationship."

## Approach: Hub-First Financial Grounding

Focus enrichment on the ~50 highest-centrality nodes (Apple, TSMC, Nvidia, CATL, Samsung, etc.). These nodes appear in the traversal path of most real impact traces. Making their edges financially meaningful immediately improves the tool for the majority of use cases, without requiring a full re-parse of all 500+ 10-K filings.

---

## Schema Changes

### `schema/models.py` — Edge additions

```python
weight: Optional[float] = None
# Financial exposure fraction (0.0–1.0). Null = not yet scored.
# For a supplies edge A→B: "what fraction of A's revenue comes from B,
# or what fraction of B's supply of this input comes from A."
# Source: explicit % disclosure in SEC 10-K filing.

confidence: Optional[float] = None
# Edge source quality (0.0–1.0). Null = not yet scored.
# sec_explicit ≈ 0.90, sec_inferred ≈ 0.65, manual ≈ 0.85,
# wikidata ≈ 0.50, wikipedia ≈ 0.40

source_tier: Optional[Literal[
    "sec_explicit",   # named % in SEC filing
    "sec_inferred",   # mentioned in SEC filing, no explicit %
    "manual",         # hand-curated
    "wikidata",       # Wikidata SPARQL / P1830
    "wikipedia",      # Wikipedia LLM extraction
]] = None
```

### `schema/store.py` — SQLite DDL additions

```sql
weight      REAL,
confidence  REAL,
source_tier TEXT CHECK(source_tier IN (
    'sec_explicit','sec_inferred','manual','wikidata','wikipedia'
))
```

All new columns nullable. Existing rows remain valid.

---

## New Pipeline Stages

### `pipeline/identify_hubs.py`

**Input**: `data/edges.jsonl`
**Output**: `data/hubs.jsonl` (one JSON object per line: `{id, name, centrality}`)

Algorithm:
1. Load all edges into a `networkx.DiGraph`
2. Compute approximate betweenness centrality (k=200 samples for speed)
3. Sort descending, take top 50
4. Filter to nodes that have a CIK (SEC filer) — only those have cached 10-K text to mine

### `pipeline/score_confidence.py`

**Input**: `data/edges.jsonl`
**Output**: updated `data/edges.jsonl`

Rule-based, no LLM. Assign `confidence` and `source_tier` to every edge based on `extracted_by` and `provenance.filing`:

| extracted_by | provenance.filing | source_tier | confidence |
|---|---|---|---|
| `"rule"` | any | `sec_inferred` | 0.75 |
| `"llm"` | starts with `cik:` | `sec_inferred` | 0.65 |
| `"llm"` | contains `wikipedia.org` | `wikipedia` | 0.40 |
| `"llm"` | contains `wikidata.org` or `Q\d+` | `wikidata` | 0.50 |
| `"manual:curation"` | any | `manual` | 0.85 |

Edges already enriched with `weight` by `extract_weights.py` get `source_tier = "sec_explicit"`, `confidence = 0.90` (overrides the above).

### `pipeline/extract_weights.py`

**Input**: `data/hubs.jsonl`, cached 10-K files in `data/filings/`
**Output**: updated `data/edges.jsonl`

For each hub with a CIK:
1. Find the most recent cached 10-K text file
2. Extract three sections: Item 1A (Risk Factors), Item 7 (MD&A), Notes to Financial Statements
3. Send each section to Claude CLI with this prompt:

```
You are extracting financial concentration data from an SEC 10-K filing.
Find all explicitly stated customer or supplier concentration percentages.
Return a JSON array only — no other text:
[{"entity": "<company name as written>", "type": "customer|supplier", "pct": <float>, "quote": "<verbatim excerpt ≤30 words>"}]
Rules:
- Only include percentages explicitly stated in the text.
- Do not estimate, infer, or combine numbers.
- If nothing qualifies, return [].
```

4. For each extracted `{entity, pct}`:
   - Use `rapidfuzz` alias resolution (score threshold ≥ 85) to match `entity` → node ID
   - Find the `supplies` edge between the hub and matched node in `edges.jsonl`
   - Set `weight = pct / 100.0`
   - Set `source_tier = "sec_explicit"`, `confidence = 0.90`
   - Add provenance `snippet = quote`

5. Write all updated edges back to `data/edges.jsonl`

**Rate limiting**: 1 Claude CLI call per section, sections processed sequentially per hub, hubs parallelized up to `WEIGHT_PARALLELISM=4`.

---

## Impact Formula Changes (`api/impact.py`)

### Candidate list enrichment

When building the per-ring candidate string, append weight/confidence if present:

```
- TSMC | Company | hop=1 | weight=0.25 (sec_explicit, conf=0.90)
- Wolfspeed | Company | hop=2 | (unweighted, conf=0.40)
```

### Scoring prompt addition

Append this block to the existing ring-scoring prompt:

```
FINANCIAL GROUNDING:
When a candidate shows weight=X:
  anchor = parent_magnitude × X
  Score that candidate in the range [anchor × 0.5, min(1.0, anchor × 1.5)]
  - Score ABOVE ceiling only if this supplier/customer has no substitute (monopoly)
  - Score BELOW floor only if the relationship is highly diversified/substitutable
When no weight is shown: score freely based on context as before.
```

### Result object change

Add `is_estimated: bool` to the `ImpactNode` in the API response:
- `is_estimated = False` when the dominant incoming edge has `weight` set
- `is_estimated = True` otherwise

---

## API Response Changes (`api/main.py`)

`ImpactNode` response model gets two new optional fields:

```python
weight: Optional[float] = None        # financial exposure fraction of dominant edge
confidence: Optional[float] = None    # source quality of dominant edge
is_estimated: bool = True             # false when weight is financially grounded
```

---

## UI Changes

### Inspector — impact box (`web/src/ui/inspector.ts`)

Replace "magnitude 0.30" with:
- **Weighted**: "~22% revenue exposure · SEC filing"
- **Estimated**: "est. impact 0.30 · Wikipedia"

Confidence badge on edge detail row:
- `sec_explicit` → green "SEC %" badge
- `sec_inferred` → blue "SEC" badge
- `manual` → yellow "curated" badge
- `wikidata` / `wikipedia` → gray "inferred" badge

### Impact overlay (`web/src/impact.ts`)

Add `isEstimated` to the `ImpactNode` TypeScript type. In the Sigma node reducer:
- Weighted nodes: full opacity tint (unchanged)
- Estimated nodes: tint at 70% opacity; if `forceLabel` active, append " (est.)" to label

---

## Build Order

1. Schema changes — `schema/models.py`, `schema/store.py`
2. `pipeline/score_confidence.py` — immediate value, no LLM required
3. `pipeline/identify_hubs.py` — prerequisite for extraction
4. `pipeline/extract_weights.py` — core weight extraction
5. `pipeline/build_graph.py` — read new fields into SQLite
6. `api/impact.py` + `api/main.py` — weight-anchored scoring
7. Frontend — inspector + impact overlay display

---

## Out of Scope

- Tier-2 supplier mapping (suppliers' suppliers) — deferred to next phase
- Time-series edge weights (how relationships change year-over-year) — deferred
- Dependency_weight (customer's perspective on supplier concentration) — deferred; customer_weight from supplier's 10-K is sufficient for MVP
- Non-hub nodes (<50 centrality rank) — processed in a follow-up pass using the same pipeline

---

## Acceptance Criteria

- `data/hubs.jsonl` contains ≥ 40 nodes
- At least 20 of the top 50 hubs have ≥ 1 weighted edge after `extract_weights.py`
- Every edge in `edges.jsonl` has `confidence` and `source_tier` set after `score_confidence.py`
- Impact trace on "Nvidia announces 50% capacity expansion" shows TSMC with "~25% revenue exposure · SEC filing" (not "est. 0.X")
- The same trace run twice returns magnitude within ±0.05 of each other (anchoring reduces variance)
