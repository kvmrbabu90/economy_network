# So What? V2 · Phase 10 — Cap-overflow no longer permanently dropped (Design)

**Date:** 2026-07-02
**Status:** Approved autonomously (user: "spec and fix it autonomously")
**Parent:** [`2026-06-17-sowhat-v2-architecture.md`](2026-06-17-sowhat-v2-architecture.md)
**Branch:** `feat/sowhat-v2`

---

## Problem

An audit-follow-up trace found a real defect in ingestion. In `run_ingest`
(`pipeline/ingest_news.py`), after `rank()` + `cap()`, **all** ranked candidates
are persisted: the top `INGEST_CAP` (25) as `status='queued'`, the rest as
`status='skipped'`. Because `dedupe()` skips any candidate whose id already exists
in `events` via `event_exists()` — which has **no status predicate** — and because
**nothing ever re-queues a `skipped` row** (only `--retry-failed` re-queues
`failed`), a fresh, material story that merely lost the per-cycle cap race is
recorded as `skipped` and then **matched-and-skipped forever**. It is never traced,
even though it's genuinely material and still within the fetch window.

At the deployed hourly cadence with a 25-cap, any hour with >25 material events
permanently sheds the overflow. That silently undercounts impact.

## Root cause (verified)

- `cap()` marks over-cap items `status='skipped'` (`ingest_news.py:165-169`).
- `run_ingest` persists **all** ranked incl. skipped (`ingest_news.py:448-449`).
- `event_exists` = `SELECT 1 FROM events WHERE id=?` — status-agnostic
  (`store.py:196-197`), so a stored `skipped` id is skipped by `dedupe()` every
  future cycle.
- No code promotes `skipped` → `queued`; `precompute` only traces `queued`.

## Fix (chosen: Option A — don't persist the overflow)

`run_ingest` persists **only** the `queued` (top-cap) candidates. Over-cap
material candidates are **not written** to `events`. Consequences:

- They are **not** recorded as `skipped`, so `event_exists()` won't match them.
- The next cycle re-fetches them (fetchers look back `INGEST_MAX_AGE_DAYS=3`, far
  inside the 30-day prune horizon), re-dedupes (not in DB → pass), re-gates
  (materiality), re-ranks, and queues them **if the queue now has room**. So an
  over-cap event gets another shot each cycle until it's either traced or ages out
  of the 3-day fetch window — instead of being dropped after one cycle.

**Why Option A over alternatives:**
- *Persist `skipped` + promote back to `queued` when capacity frees up* (a real
  backlog): more code, accumulates rows, and needs re-queue ordering — deferred as
  unnecessary for the reported bug.
- *Remove the ingest cap; let precompute's `PRECOMPUTE_MAX_EVENTS` budget drain a
  growing `queued` backlog*: cleaner in theory but `queued_events` is oldest-first,
  so a backlog would starve **fresh** news behind stale items — worse for a
  freshness-oriented map. Rejected.
- Option A preserves the existing matched flow (≈25 queued in, ≈25 traced out per
  cycle) and the recency-ranked prioritization, changing only that overflow is
  *deferred* (re-eligible) rather than *dropped* (permanent).

**Accepted trade-off:** under sustained load where material volume consistently
exceeds the cap, the lowest-priority (by source/centrality/recency) overflow may
keep losing the cap race and never be traced before it ages out of the 3-day
window. That is correct prioritization for a bounded-throughput system — and,
crucially, it is *not* the previous "dropped permanently after a single cycle" bug.
Each overflow event stays in contention for ~72 hourly cycles (its 3-day fetch
lifetime). A minor re-cost: overflow is re-fetched + re-materiality-gated each
cycle it reappears (the gate is one batched LLM call, so negligible).

## Change

`pipeline/ingest_news.py::run_ingest` — persist only `queued`; report the rest as
`deferred` (re-eligible), not `skipped` (dropped):

```python
        ranked = cap(rank(material, conn))
        queued = [c for c in ranked if c["status"] == "queued"]
        for c in queued:
            insert_event(conn, {**c, "status": "queued"})
        summary = {"fetched": len(cands), "resolved": len(resolved), "fresh": len(fresh),
                   "material": len(material), "queued": len(queued),
                   "deferred": len(ranked) - len(queued)}
```

`cap()` is unchanged (still marks queued/skipped in-memory; only the persistence
filter changes). The `skipped` event status becomes unused by ingestion (no
consumer relies on it — `precompute` reads `queued`, `aggregate` reads `traced`).

## Testing

- **Overflow not persisted:** feed >cap resolvable material candidates (monkeypatch
  fetchers + identity materiality gate + a cap of 1); assert exactly the queued
  count is written to `events` and the over-cap one is absent; summary has
  `queued=1, deferred=N`.
- **Re-eligibility (the fix's point):** run a second cycle re-fetching the same
  candidates; the already-`queued` one is skipped by `event_exists`, the previously
  deferred one is now fresh → gets queued. Assert the events table grew to include
  it — i.e. the overflow was *not* dropped forever.
- Existing `tests/test_ingest_news.py` / `test_ingest_fetchers.py` stay green
  (update any assertion referencing the old `skipped` summary key → `deferred`).

## Files touched
| File | Change |
|---|---|
| `pipeline/ingest_news.py` | `run_ingest` persists only `queued`; summary `skipped`→`deferred` |
| `tests/test_ingest_fetchers.py` | overflow-not-persisted + re-eligibility tests |
| `tests/test_ingest_news.py` | update summary-key assertion if present |

No change to precompute, aggregate, serving, or the frontend. `cap()` signature
unchanged.

## Out of scope
A persisted, promotable backlog queue; newest-first precompute draining; per-source
caps. All deferred — Option A resolves the reported permanent-drop with the
smallest, lowest-risk change.
