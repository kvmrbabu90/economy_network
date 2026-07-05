# GKG context grounding — design

**Date:** 2026-07-05
**Status:** approved (design), pending implementation

## Problem

Impact traces for GDELT-GKG events are seeded and propagated from the **headline
alone**. Headlines routinely omit the very organizations the article connects.

Concrete failure (live): the event *"Google Tops $1 Billion in Africa
Investments"* seeds on `slug:google`, whose graph neighborhood is **0
above-threshold edges** (only inferred US-tech competitors). A "Sharpen with
Claude" re-trace therefore wanders into US tech and never reaches Alexbank
(`wikidata:Q4856020`) — even though Alexbank shows a precomputed POSITIVE 0.42,
because the *precompute's* longer run reached it via the African consumer market.
The headline never names Alexbank, so on-demand seed extraction can't find it.

GDELT-GKG already extracts, per article: the organizations (`v2orgs`, with text
offsets), theme codes, a tone score, and dollar amounts. `_gkg_candidate` parses
all of it, uses it to pick the single seed, then **discards the rest**. That
discarded structure is exactly the missing grounding — and it is already compact
(structured metadata, not prose), so surfacing it costs tens of tokens, not the
thousands a full article body would.

## Goal

Persist a compact, code-built "context capsule" per GKG event and feed it into
the **seed-selection** step of the trace, so the trace anchors on the right
organizations. Minimize token cost: the capsule is one short line, injected only
into the two Step-2 seed prompts — never into hop/refine/verify.

Non-goal: anchoring the clicked node by its known precomputed verdict (a separate
idea, deferred). This change is purely about grounding seed selection.

## The capsule

Pure builder `build_gkg_context(rec, index, seed_id) -> Optional[str]` in
`pipeline/gkg.py`:

- **Other orgs** — match every `rec.v2orgs` surface against the node index
  (reusing `normalize_name` + the same index `_gkg_candidate` uses), collect the
  graph-matched node **names**, drop the seed, dedupe preserving order, cap at 5.
  Graph-matched only (traceable, no noise).
- **Money** — largest `rec.amounts` entry where `amount_is_currency(obj)`,
  formatted compactly: `$1B` / `$500M` / `$12K` (fall through to `$<int>` under 1k).
- **Tone** — sign of `rec.tone`: `positive tone` if `> 1.0`, `negative tone` if
  `< -1.0`, else `neutral tone`.
- **Format** — `[involves: A, B, C | $1B | positive tone]`, omitting any empty
  section. If there are no other orgs AND no amount AND neutral tone → return
  `None` (nothing worth adding).
- Hard length cap (e.g. 240 chars) so a pathological record can never bloat tokens.

Example:
```
Google Tops $1 Billion in Africa Investments
[involves: Alexbank, Safaricom, MTN Group | $1B | positive tone]
```

## Storage

- New nullable column `events.gkg_context TEXT`.
- Idempotent migration `_migrate_gkg_context(conn)` (ALTER if missing), mirroring
  the `story_sig` migration. No index needed.
- `_gkg_candidate` sets `cand["gkg_context"] = build_gkg_context(rec, index, nid)`
  (collecting the matched-org names during its existing surface loop).
- `insert_event` persists `gkg_context` (sentinel-guarded like `story_sig`, so an
  explicit `None` is honored and non-GKG events store `NULL`).

## Trace injection (token-lean)

`run_impact` / `run_impact_stream` gain `context: Optional[str] = None`
(default None ⇒ current behavior byte-for-byte). Inside `run_impact_stream`,
Step 2 only:

```python
seed_text = text if not context else f"{text}\n{context}"
f_entities = pool.submit(_extract_named_entities, seed_text)     # was text
f_seed_raw = pool.submit(_llm_call, _SEED_PROMPT_TEMPLATE.format(
    news=seed_text, candidates="\n".join(candidate_lines)))       # was text
```

Hop scoring, refinement, verification, and `_score_seed_node` continue to use the
bare `text`. This confines the capsule's cost to the two seed calls.

With `involves: Alexbank` in `seed_text`, `_extract_named_entities` surfaces
Alexbank, `_resolve_entity` (Company-only) resolves it, and it becomes a hop-0
seed — the direct fix for the reported miss.

## Flow to both trace paths

- **Precompute** (`precompute_impacts.py`): read the event's `gkg_context` and
  pass `context=ev.get("gkg_context")` alongside the existing
  `seed_hint_id=ev.get("seed_node_id")`.
- **Sharpen** (on-demand): `/node/{id}/impact` already enriches each `top_events`
  row with url/source from the `events` table at query time; add `gkg_context`
  there. The frontend `sharpenWithClaude` forwards the (first non-empty) context
  via a new `context` field on the `/impact/stream` POST body; `runImpactStream`
  passes it through; `/impact/stream` reads it into `run_impact_stream(context=…)`.

## Backfill

Forward-only. Existing events keep `gkg_context = NULL`; they gain a capsule when
re-ingested. Cached GKG slices are pruned, so no retro-backfill. The next hourly
cycle populates fresh events. Acceptable — the always-on layer self-heals.

## Testing

- `build_gkg_context`: seed excluded from orgs; money picks the largest currency
  and formats B/M/K; tone thresholds; None when nothing useful; org cap = 5;
  length cap; ignores non-currency amounts.
- Store: migration adds column on an old DB; `insert_event` persists and defaults
  `NULL` for non-GKG events.
- `_gkg_candidate`: attaches `gkg_context` with other matched orgs, excludes seed.
- Engine: `run_impact_stream(context=…)` puts the capsule into the entity-extract
  + commodity-seed prompts and NOT into the hop prompt (assert via a captured
  prompt spy); `context=None` leaves prompts unchanged.
- API: `/node/{id}/impact` top_events include `gkg_context`; `/impact/stream`
  forwards `context` to the engine.
- Frontend: `runImpactStream` includes `context` in the body when provided;
  `sharpenWithClaude` forwards the event context.

## Files

- `pipeline/gkg.py` — `build_gkg_context` (+ money/tone helpers).
- `pipeline/ingest_news.py` — wire capsule into `_gkg_candidate`.
- `schema/store.py` — DDL column, migration, `insert_event`.
- `api/impact.py` — `context` param, seed-only injection.
- `pipeline/precompute_impacts.py` — pass `context`.
- `api/main.py` — `/node/{id}/impact` enrichment; `/impact/stream` `context` param.
- `web/src/api.ts`, `web/src/main.ts` — forward context on sharpen.
- Tests across the above.
