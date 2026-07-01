# So What? V2 · Phase 2 — Batch Impact Precompute (Design)

**Date:** 2026-06-17
**Status:** Approved autonomously (user delegated P2+P3 build)
**Parent:** [`2026-06-17-sowhat-v2-architecture.md`](2026-06-17-sowhat-v2-architecture.md)
**Branch:** `feat/sowhat-v2`

---

## Goal

For each `status='queued'` event in the `events` table (from P1), run the impact
engine on a **lighter Claude config**, write per-node verdicts to a new
`event_impacts` table, and mark the event `traced` (or `failed`). Bounded by a
per-cycle call + wall-clock budget; idempotent and restartable.

## Locked / judgment-call decisions
- **Engine = Claude CLI, lighter config:** 2 hops, refinement OFF, verification OFF,
  seed-verify OFF (~5–6 `claude -p` calls/event). The full engine stays the on-demand
  "sharpen" path.
- **Parameterize the engine, don't fork it.** Add optional params to
  `run_impact_stream`/`run_impact` — `max_hops`, `refine`, `verify` — with defaults
  that preserve today's on-demand behavior exactly. P2 calls
  `run_impact(text, conn=…, provider="claude", max_hops=2, refine=False, verify=False)`.
- **Budgets:** stop the cycle after `PRECOMPUTE_MAX_EVENTS` (default 25, matches P1's
  cap) OR `PRECOMPUTE_WALLCLOCK_S` (default 6h) — whichever first; remaining queued
  events are left `queued` for the next cycle.
- **Failure = defer, never fabricate.** If a trace errors, returns no seeds, or the
  provider is unavailable (401/throttle), mark the event `failed` and continue; a
  `failed` event is re-attempted on a later cycle (see status flow). No partial/empty
  impacts are written as if real.
- **Idempotent/restartable:** only `queued` events are processed; on success the event
  flips to `traced` and its `event_impacts` rows are written in one transaction, so a
  crash mid-cycle leaves already-traced events done and the rest still `queued`.

---

## Engine change (`api/impact.py`)

Add three optional keyword params (defaults = current behavior):

```python
def run_impact_stream(text, *, conn, provider=None,
                      max_hops=None, refine=True, verify=True): ...
def run_impact(text, *, conn, provider=None,
               max_hops=None, refine=True, verify=True): ...  # passes through
```
- `max_hops`: local override of the module `MAX_HOPS` (the `for hop in range(1, (max_hops or MAX_HOPS)+1)` bound).
- `refine=False`: skip the `_refinement_pass` call (still emit a zero refinement summary/event for contract stability).
- `verify=False`: skip BOTH `_verification_pass` AND the seed-directness `_verify_seed_directness` (both currently gated on `VERIFY_ENABLED`; new gate is `verify and VERIFY_ENABLED`).

This is additive; `/impact`, `/impact/stream`, `run_multi_impact`, and all existing
tests keep their behavior (they don't pass the new params).

## New table (`schema/store.py`)

```sql
CREATE TABLE IF NOT EXISTS event_impacts (
    event_id   TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    direction  TEXT NOT NULL,     -- positive|negative|no_effect|unscored
    magnitude  REAL NOT NULL,
    hop        INTEGER NOT NULL,
    reasoning  TEXT,
    PRIMARY KEY (event_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_event_impacts_node ON event_impacts(node_id);
```
Plus store helpers: `write_event_impacts(conn, event_id, impacts)` (delete-then-insert
for the event, so a re-trace is clean) and `set_event_status(conn, event_id, status)`.

## Runnable (`pipeline/precompute_impacts.py`)

`python -B -m pipeline.precompute_impacts`:
1. Load `queued_events(conn)` (from P1's helper), newest/priority order.
2. For each, until a budget is hit: `run_impact(headline, conn, provider="claude",
   max_hops=2, refine=False, verify=False)`.
   - On result with seeds → `write_event_impacts` (skip `is_seed` rows? **keep seeds**
     — a seed IS an impact on that node) + `set_event_status(traced)`, in one txn.
   - On error / no seeds / provider-unavailable → `set_event_status(failed)`.
3. Track and print a summary: `{processed, traced, failed, impacts_written, elapsed_s}`.
- Config: `PRECOMPUTE_MAX_EVENTS` (25), `PRECOMPUTE_WALLCLOCK_S` (21600), provider
  override via `IMPACT_LLM_PROVIDER` (so Gemma can be swapped for an unmetered run).

## Status flow
`queued → traced` (success) | `queued → failed` (error). A separate cycle may reset
`failed → queued` for retry — P2 provides `--retry-failed` to re-queue `failed` events
(bounded, so a permanently-bad event doesn't loop forever: only retried if
`failed` and not already retried this cap).

## Testing (deterministic — monkeypatch `run_impact`, temp SQLite)
- Engine params: `run_impact(..., max_hops=2, refine=False, verify=False)` runs 2 hops,
  calls neither `_refinement_pass` nor `_verification_pass` nor `_verify_seed_directness`
  (assert via spies); defaults still run 3 hops + all passes.
- A queued event whose (mocked) `run_impact` returns seeds+impacts → `event_impacts`
  rows written (incl. the seed), event → `traced`.
- A queued event whose `run_impact` returns `{error, impacts:[]}` (no seeds) → no rows,
  event → `failed`.
- `run_impact` raising → event `failed`, cycle continues to the next event.
- Budget: `PRECOMPUTE_MAX_EVENTS=1` with 2 queued → only 1 processed, other stays `queued`.
- Idempotent: re-run after all `traced` → no-op (0 processed).
- `write_event_impacts` is delete-then-insert (re-trace replaces, no dup PK error).

## Files touched
| File | Change |
|---|---|
| `api/impact.py` | `max_hops`/`refine`/`verify` params on `run_impact_stream`+`run_impact` |
| `schema/store.py` | `event_impacts` DDL + `write_event_impacts`/`set_event_status` |
| `pipeline/precompute_impacts.py` | NEW — the batch runnable |
| `tests/test_precompute_impacts.py` | NEW — deterministic tests above |
| `tests/test_impact_stream.py` | add param-gating tests (lighter config skips passes) |
| `.env.example` | `PRECOMPUTE_MAX_EVENTS`, `PRECOMPUTE_WALLCLOCK_S` |

No change to P1, the graph, or V1 endpoints. `event_impacts` is additive.

## Out of scope
Aggregation into `node_impact` (P3), serving/scheduling (P4), UI (P5).
