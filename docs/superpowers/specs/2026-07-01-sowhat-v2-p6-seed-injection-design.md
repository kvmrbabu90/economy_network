# So What? V2 · Phase 6 — Seed Injection for Batch Precompute (Design)

**Date:** 2026-07-01
**Status:** Approved autonomously (user: "spec and build it autonomously")
**Parent:** [`2026-06-17-sowhat-v2-architecture.md`](2026-06-17-sowhat-v2-architecture.md)
**Branch:** `feat/sowhat-v2`

---

## Problem (observed in the first automated cycle)

The first `run_cycle` traced **1 of 25** queued events; the other 24 were marked
`failed` and produced no impacts, so the warm map reflected a single event
(`aggregate events_in_window: 1`). Diagnosis from the run log: Claude was healthy
(54/54 CLI calls exit 0, no throttling) and ingestion was strong (90 fetched → 25
queued). All 25 events ran seed extraction, but only one reached a BFS hop.

**Root cause:** `precompute` calls `run_impact(ev["headline"], …)` passing **only the
headline**, discarding the `seed_node_id` that ingestion already resolved. The engine
then re-derives seeds from the bare headline via `_extract_named_entities` +
`_resolve_entity`, and `_resolve_entity` is hard-filtered to `type = 'Company'` (with
a macro-entity blocklist). Ingestion, by contrast, resolves across **all node types**
(name/alias/ticker/LIKE via `pipeline/ingest_news._resolve_to_node_id`). So an event
whose seed is a Commodity/Region/Regulator — or a company the LLM phrases differently
than the node name — resolves at ingest but **loses its seed in the engine** → no
seeds → no impacts → silently `failed`.

## Goal

Let `precompute` hand the engine the seed ingestion already resolved, so each event
traces from a **guaranteed, known-good seed** instead of re-guessing. Expected effect:
batch trace success rate rises from ~4% toward most-of-25. On-demand behavior is
unchanged.

## Locked / judgment-call decisions

- **New optional param `seed_hint_id`** on `run_impact_stream` and `run_impact`
  (default `None`). When `None`, behavior is **byte-for-byte identical** to today —
  the on-demand path passes nothing. Only `precompute` passes it.
- **Augment, don't replace.** The engine still runs its normal extraction (named
  entities + commodity seed) — those can find *additional* valid seeds. The hint only
  **guarantees** the ingest-resolved node is also seeded. Richest trace wins.
- **Inject before the "no seeds" bail-out.** The injection happens right after
  `all_seeds` is assembled (Step 5) and *before* the `if not all_seeds:` early return,
  so a hint can rescue an event the LLM couldn't seed at all.
- **Skip if already seeded.** If the hint id is already in `seen_ids`/`all_seeds` (the
  LLM resolved it too), do nothing — no duplicate.
- **Score the hint for a real verdict.** A hop-0 seed needs a direction + magnitude.
  When the hint isn't already seeded, obtain them with **one focused LLM call** via a
  new `_score_seed_node(text, name, type)` (reuses `_llm_call` + `_parse_llm_json`).
  This is ~1 extra call per rescued event — cheap next to a full trace, and Claude is
  healthy in batch.
- **Hint bypasses the seed-directness verify gate.** `_verify_seed_directness` exists
  to reject *speculative* commodity reaches. The hint is an ingestion-resolved,
  authoritative seed, so it is injected unconditionally (not subject to that gate).
- **Fail-open fallback.** If `_score_seed_node` returns nothing usable (empty/garbled
  LLM), **skip the injection** for that event — behavior is then exactly today's (the
  event may still fail, but never worse, and never fabricated). Log it.
- **`_node_summary` resolves the hint.** If `seed_hint_id` doesn't resolve to a real
  node (`_node_summary` returns None), skip injection (defensive; shouldn't happen for
  ingest-written ids).
- **`precompute` passes `ev["seed_node_id"]`.** Events carry `seed_node_id` from P1
  ingestion. When it's null/empty, pass `None` (fall back to extraction).

## `_score_seed_node` (new helper in `api/impact.py`)

```python
def _score_seed_node(text: str, name: str, node_type: str) -> Optional[dict[str, Any]]:
    """One focused LLM call: given the news text and a specific known entity, return
    {"direction": positive|negative|no_effect, "magnitude": 0-1, "reasoning": str}.
    Returns None if the call fails or parse yields no usable direction."""
```
Prompt is a short, single-entity scoring instruction; tolerant JSON parse; clamp
magnitude to [0,1]; default reasoning to "". Returns None on failure (→ fail-open).

## Injection flow (in `run_impact_stream`, after Step 5)

```
if seed_hint_id and seed_hint_id not in seen_ids:
    summ = _node_summary(conn, seed_hint_id)
    if summ:
        scored = _score_seed_node(text, summ["name"], summ["type"])
        if scored:
            all_seeds.append({node_id, name, type, direction, magnitude, reasoning,
                              sector, country, is_named_entity: False})
            seen_ids.add(seed_hint_id)
            debug_log.append("seed_hint: injected …")
        else:
            debug_log.append("seed_hint: scoring failed — skipped")
    else:
        debug_log.append("seed_hint: id did not resolve — skipped")
# then the existing `if not all_seeds:` check, hop-0 init, BFS …
```

`run_impact` forwards `seed_hint_id` to `run_impact_stream`.

## `precompute` change (`pipeline/precompute_impacts.py`)

```python
r = _impact.run_impact(ev["headline"], conn=conn, provider=prov,
                       max_hops=BATCH_MAX_HOPS, refine=False, verify=False,
                       seed_hint_id=ev.get("seed_node_id"))
```

## Testing (deterministic — monkeypatched LLM, temp SQLite; no network)

- **Injection rescues an unseedable event:** seed a graph with a node the engine's
  extraction won't find; monkeypatch `_extract_named_entities` → `[]` and the commodity
  seed → none (so `all_seeds` would be empty → today: "no seeds" error); monkeypatch
  `_score_seed_node` → a real verdict; call `run_impact_stream(text, conn=…,
  seed_hint_id="<node>")` → assert a `seeds` event fires with that node at hop 0 and the
  done result has non-empty impacts (seed at minimum).
- **Backward compat:** with `seed_hint_id=None`, the seed set and result are unchanged
  vs. the current behavior (existing `test_impact_stream.py` cases stay green).
- **Already-seeded hint → no duplicate:** if extraction already resolves the hint id,
  passing the same `seed_hint_id` doesn't create a second seed row.
- **Scoring fail-open:** `_score_seed_node` → None and no LLM seeds → the engine still
  returns the "no seeds" result (event fails, unchanged), not a crash.
- **`precompute` passes the hint:** monkeypatch `pc._impact.run_impact` to capture
  kwargs; assert `seed_hint_id == ev["seed_node_id"]` for a queued event.

## Files touched

| File | Change |
|---|---|
| `api/impact.py` | `_score_seed_node` helper; `seed_hint_id` param on `run_impact_stream` + `run_impact`; injection block after Step 5 |
| `pipeline/precompute_impacts.py` | pass `seed_hint_id=ev.get("seed_node_id")` |
| `tests/test_impact_stream.py` | injection / backward-compat / fail-open tests |
| `tests/test_precompute_impacts.py` | asserts the hint is passed through |

No change to P1 ingestion, P3 aggregation, P4 serving, or P5 frontend. The on-demand
path is untouched (it never passes `seed_hint_id`).

## Out of scope
Generalizing `_resolve_entity` to all node types for the on-demand path (the hint
sidesteps it for batch); re-tracing already-`failed` events (the operator can
`--retry-failed` after this lands, and they'll now seed correctly).
