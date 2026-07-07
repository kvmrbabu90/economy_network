# Minimize LLM usage — deterministic scaffolding (design)

**Date:** 2026-07-05
**Status:** approved (design), pending spec review

## Goal

Cut LLM (Claude CLI) call volume in the hourly cycle by making the *mechanical
scaffolding* of a trace deterministic, while leaving the one step that is genuine
economic reasoning — **per-hop impact scoring** — untouched. Driver: "LLM only
where it earns its place."

## Where the calls go today (batch precompute, per GKG event)

`run_impact_stream` Step 2 always runs, even when the seed is already known:

1. `_extract_named_entities(seed_text)` — LLM (find company seeds) 
2. commodity/region seed prompt (`_SEED_PROMPT_TEMPLATE`) — LLM (parallel with 1)
3. `_score_seed_node(hint)` — LLM (seed direction), only when `seed_hint_id` given
4. per-hop ring scoring × `BATCH_MAX_HOPS` (2) — LLM (**the reasoning — keep**)

≈ **5 calls/event**. Batch skips refine/verify (on-demand only). The materiality
gate adds ~1 batched LLM call per ~60 candidates AND gates how many events reach
tracing at all.

## The three cuts

### Cut A — Deterministic seeds for GKG/8-K events (skip extraction)

For GKG events we already resolved the seed set deterministically in
`_gkg_candidate` (`matched` = every graph-matched org, salience-sorted, seed first).
8-K events know their filer. So the LLM extraction in steps 1–2 re-discovers what we
already know.

**Storage:** new event column `seed_ids TEXT` (JSON array of node-ids, most-salient
first, cap 5). Populated in `_gkg_candidate` from `[m[2] for m in matched[:5]]`; for
8-K, `[node_id]`. Idempotent migration (mirrors `gkg_context`). NULL for free-text
events.

**Engine:** `run_impact_stream` / `run_impact` gain `known_seed_ids: Optional[list[str]]`.
When present and non-empty (and `TRUST_KNOWN_SEEDS` on, default on):
- **Skip** `_extract_named_entities` AND the commodity-seed prompt (steps 1–2).
- Resolve each id via `_node_summary`; drop unresolved.
- **One** batched call `_score_seed_set(text, summaries)` returns per-node
  `{direction, magnitude}` for the whole set (replaces N× `_score_seed_node`).
  Fail-open: if it returns nothing usable, fall back to the existing extraction path
  (never produce a seedless trace).
- Proceed to hop-scoring unchanged.

**Result:** ≈ **1 (batch seed-score) + hops** vs 5. The P14 context capsule's
"involves" orgs become *actual seeds* here (strictly better than injecting them as
prompt text); the capsule injection stays for on-demand sharpen, which still extracts.

**Precompute** passes `known_seed_ids=json.loads(ev["seed_ids"])` (falling back to
`[ev["seed_node_id"]]`). On-demand/sharpen is unchanged (no known set → extracts).

### Cut B — Rule-based materiality pre-filter

`_gkg_materiality_prior(rec, centrality)` already scores themes + $ amount + tone +
salience. Use it to decide most events without the LLM:

- **Auto-keep** when prior ≥ `INGEST_MATERIALITY_KEEP` (default tuned on live data).
- **Auto-drop** when prior < `INGEST_MATERIALITY_DROP`.
- **LLM-judge** only the ambiguous middle band.
- **8-K** events: auto-keep (an SEC filing is material by construction).
- **Free-text RSS** (no prior signals): unchanged — full LLM gate.

The prior is computed in `fetch_gkg_bulk`; carry it on the candidate (`_prior`) so the
gate in `run_ingest` can read it. This cuts gate calls AND removes whole downstream
traces (each ~1–3 calls) for auto-dropped events — the biggest compounding save.
Thresholds are config so we can tune precision/recall without code changes.

### Cut C — Gate the commodity-seed prompt on themes

Even outside the trusted-seed path, the commodity/region seed call only matters when a
commodity/macro theme is present. Within Cut A's trusted-seed path it is already
skipped; additionally, pass a `commodity_hint: bool` (from
`any(is_business_theme/commodity theme)` at ingest) so the engine makes the
commodity-seed call **only** when the hint is set. Saves ~1 call/event on the many
company-only stories that reach the extraction path.

## Expected effect

Dominant GKG path: **~5 → ~2 calls/event** (batch seed-score + hops). Materiality
auto-decides the large majority of candidates with **no** LLM call, and auto-drops
remove whole traces. Realistic **50–60%** reduction in cycle LLM volume, with the
impact-reasoning step byte-for-byte unchanged.

## Deferred (explicitly out of scope)

- Seed *direction* from GKG tone (too noisy — we keep the 1 batch-score call).
- Any hop-scoring cache/heuristic (protects the reasoning step, per decision).

## Data flow

```
ingest (_gkg_candidate): matched → seed_ids (JSON) + _prior on candidate
run_ingest: prior ≥ keep → queue; < drop → skip; middle/RSS → LLM gate; 8-K → keep
precompute: run_impact(known_seed_ids=seed_ids, commodity_hint=…, context=…)
engine: known set → skip extract+commodity → 1 batch seed-score → hops (unchanged)
```

## Testing

- **Cut A:** with `known_seed_ids`, `_extract_named_entities` is NOT called and the
  commodity prompt is NOT issued (assert via spies); `_score_seed_set` called once;
  seeds appear at hop 0. Empty/unresolved set → falls back to extraction. On-demand
  path (no known set) unchanged.
- **Cut B:** prior ≥ keep kept without an LLM call; < drop dropped; middle band hits
  the LLM gate; 8-K auto-kept; RSS still gated. (Spy on `_claude_call`.)
- **Cut C:** commodity-seed prompt issued iff `commodity_hint`; skipped otherwise.
- **Store:** `seed_ids` persists + migration adds the column on an old DB.
- Guardrail: full-suite green; a batch-precompute integration test shows reduced
  `_llm_call` count for a known-seed GKG event vs the extraction path.

## Files

- `schema/store.py` — `seed_ids` column + migration + `insert_event`.
- `pipeline/ingest_news.py` — populate `seed_ids` + `_prior`; materiality pre-filter
  in `run_ingest`; `commodity_hint`.
- `pipeline/gkg.py` — (helpers as needed for theme→commodity_hint).
- `api/impact.py` — `known_seed_ids` + `commodity_hint` params; trusted-seed path;
  `_score_seed_set` batch scorer; skip extraction/commodity.
- `pipeline/precompute_impacts.py` — pass `known_seed_ids`, `commodity_hint`.
- Tests across the above.

## Rollout / safety

Each cut behind a default-on flag (`TRUST_KNOWN_SEEDS`, `INGEST_MATERIALITY_RULES`)
so any regression can be reverted by env without a deploy. Fail-open everywhere:
a broken shortcut degrades to today's LLM path, never to a seedless/empty trace.
