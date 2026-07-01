# So What? V2 — Precomputed Always-On Impact Layer (Architecture)

**Date:** 2026-06-17
**Status:** Architecture approved (brainstorm). This is the ROADMAP spec; each phase
gets its own design → plan → build cycle.
**Branch:** `feat/sowhat-v2` (stacked on the impact-engine work)

---

## Vision

Move the "So What?" impact analysis from **on-demand** (the user types a headline
and waits ~3 minutes for a Claude trace) to a **precomputed, always-on layer**:

1. Every 12 hours, ingest broad global news — political, commodity, healthcare,
   disease, filings, M&A, JVs, anything that could move any of the ~5,000+ graph
   nodes.
2. Each event is auto-mapped to seed(s) and traced 3 hops; results are cached.
3. Opening "So What?" shows the same experience, but nothing runs at request time —
   the impact map is already warm.
4. Selecting any company / commodity / region shows the **combined impact of every
   recent event that touched it or its ecosystem**, netted into a single verdict
   plus the contributing events.

V1 (on-demand streaming trace) is **not removed** — it becomes the "sharpen this
with Claude" action layered on top of the precomputed baseline.

---

## Architecture

The LLM moves off the request path and into a background batch. The UI reads
precomputed tables.

```
every 12h ─► P1 ingest broad news ─► events               (event + candidate entities)
                                          │
              P2 batch precompute ◄───────┘   (LOCAL model, all gated events, 3 hops)
                    │  one impact trace per event
                    ▼
              event_impacts   (event_id, node_id, direction, magnitude, hop, reasoning)
                    │
              P3 aggregate ─► node_impact   (node_id → net dir/mag, decayed, top events, mixed flag)
                    │
              P4 API serves node_impact + events instantly (no LLM at request time)
                    ▼
              P5 frontend: graph pre-tinted by node_impact; click node → combined
                 verdict + timeline of contributing events + "re-run with Claude"
```

### Cross-cutting decisions (locked)

- **Hybrid compute engine.** The 12h batch runs on the **free local model**
  (Ollama/Gemma, already wired via `IMPACT_LLM_PROVIDER=ollama`) so breadth is
  unlimited and unmetered. **Claude** is reserved for (a) a user-triggered "sharpen"
  re-run of a single event/node, and (b) optionally re-scoring the top movers each
  cycle. Rationale: precomputing hundreds of traces/cycle with Claude is infeasible
  on a personal Max plan (rate limits, ~3 min/trace, the 401 risk on unattended
  jobs); the local model has none of those constraints.
- **7-day rolling window + recency decay.** A node's combined impact aggregates
  events from the last 7 days, each weighted by recency (`weight = 0.5 ** (age_days /
  HALFLIFE_DAYS)`, HALFLIFE ≈ 3 days). Events older than the window roll off. Keeps
  the score fresh and bounded while still combining multiple recent events.
- **"Ecosystem" = any hop.** A node's combined impact includes every event where it
  appears *anywhere* in the trace (seed or hop-N). The engine already attenuates
  magnitude by hop distance, so "touched it or its ecosystem" falls out for free.
- **Netting reuses `_merge_impact_results`.** The existing multi-event merge (net
  positive − negative mass, `mixed_signals` flag) is the aggregation primitive;
  P3 extends it with the recency weight.
- **Ingestion pre-gate bounds volume.** Only events whose candidate entity resolves
  to a graph node (the graph-gate built in the news work) enter the trace queue — we
  never spend compute tracing un-seedable noise.
- **File/DB-based, restartable stages** (project invariant): P1→P2→P3 each read the
  prior stage's rows and write their own; P2 is idempotent (skips already-traced
  events) so a crashed batch resumes.

---

## Data model (new SQLite tables in `econgraph.db`)

```sql
CREATE TABLE events (
  id           TEXT PRIMARY KEY,      -- stable hash of (url) or (source+title)
  headline     TEXT NOT NULL,         -- normalized ≤15-word headline
  source       TEXT,
  url          TEXT,
  category     TEXT,                  -- politics|commodity|health|filing|m&a|jv|macro|other
  published_at TEXT,                  -- ISO date
  ingested_at  TEXT NOT NULL,
  seed_entity  TEXT,                  -- primary entity name (from ingestion)
  status       TEXT NOT NULL          -- 'queued' | 'traced' | 'failed' | 'skipped'
);

CREATE TABLE event_impacts (          -- one row per (event, affected node)
  event_id   TEXT NOT NULL,
  node_id    TEXT NOT NULL,
  direction  TEXT NOT NULL,           -- positive|negative|no_effect|unscored
  magnitude  REAL NOT NULL,
  hop        INTEGER NOT NULL,
  reasoning  TEXT,
  PRIMARY KEY (event_id, node_id)
);

CREATE TABLE node_impact (            -- P3 rollup, one row per affected node
  node_id       TEXT PRIMARY KEY,
  direction     TEXT NOT NULL,        -- net
  magnitude     REAL NOT NULL,        -- net, decayed, clamped 0-1
  mixed_signals INTEGER NOT NULL,
  event_count   INTEGER NOT NULL,
  top_events    TEXT NOT NULL,        -- JSON: top contributing events w/ their verdicts
  computed_at   TEXT NOT NULL
);
```

`node_impact` is a derived cache — fully rebuildable from `event_impacts` + the decay
function. The graph itself (`nodes`/`edges`) is unchanged.

---

## Phases (each is a separate spec → plan → build)

### P1 — Broad news ingestion
Extend the news pipeline to pull wide, multi-category news every 12h (SEC 8-K filings
— already scaffolded in `pipeline/sec_8k.py`; free entity-tagged APIs — Marketaux /
Alpha Vantage free tiers; category RSS), normalize each to an `events` row with a
resolved `seed_entity`, dedupe by URL/id, and queue only graph-resolvable events.
**Done when:** a 12h run populates `events` with ~dozens of deduped, entity-resolved,
multi-category items; no tracing yet.

### P2 — Batch impact precompute
A runnable that, for each `status='queued'` event, calls `run_impact` on the LOCAL
provider and writes `event_impacts`, then marks the event `traced`. Idempotent,
restartable, bounded by a per-cycle cap. Reuses the entire existing engine (streaming
not needed here — the non-streaming `run_impact` wrapper is fine).
**Done when:** every queued event has an `event_impacts` set (or is marked `failed`),
re-running is a no-op, and a mid-batch crash resumes cleanly.

### P3 — Impact aggregation
A rollup that recomputes `node_impact` from `event_impacts` over the 7-day decayed
window, reusing `_merge_impact_results` semantics plus the recency weight.
**Done when:** each affected node has a netted, decayed combined verdict + its top
contributing events; rebuild is deterministic from `event_impacts`.

### P4 — Serving + scheduler
Read-only endpoints: `GET /impact/live` (the full precomputed node→verdict map for
graph tinting) and `GET /node/{id}/impact` (combined verdict + contributing events).
A scheduler runs P1→P2→P3 every 12h. The existing `/impact/stream` stays as the
Claude "sharpen" action. Freshness metadata (`computed_at`) exposed.
**Done when:** the API serves the precomputed map with zero request-time LLM calls,
and the 12h job refreshes it unattended.

### P5 — Frontend V2
The graph loads pre-tinted from `/impact/live`; clicking a node shows its combined
verdict + a timeline of contributing events (each links to its source) + a "re-run
with Claude" button that invokes the V1 streaming trace for that node/event.
**Done when:** opening the app shows a warm impact map with no user action, and any
node reveals its combined-impact story.

---

## Build order & rationale

Strictly P1 → P2 → P3 → P4 → P5. Each phase is independently testable and delivers a
verifiable artifact (a populated table / a working endpoint) before the next depends
on it — matching the project's layered-restartable invariant. P1–P3 are backend/data
and can be validated with the local model at zero cost; P4–P5 are the user-facing
payoff.

## Key risks & mitigations

| Risk | Mitigation |
|---|---|
| Local-model verdict quality lower than Claude | Hybrid: Claude "sharpen" on demand; optionally Claude re-scores top-N movers/cycle in a later iteration |
| Local batch throughput (Gemma is slow: minutes/trace × dozens of events) | Per-cycle cap; 3-hop stays but frontier caps already bound ring size; batch runs in background, not on the request path |
| Stale/unbounded accumulation | 7-day rolling window + recency decay; `node_impact` fully rebuildable |
| Ingestion noise (opinion/micro-cap) | Reuse the news filter + graph-gate; only resolvable events are queued |
| Unattended job reliability (the 401 we hit) | Local model has no auth; scheduler logs + status column make failures visible and re-runnable |

## Out of scope (for V2 as specified)
- Real-time (sub-12h) updates / streaming ingestion.
- Quantified $ / %-EPS impact (that's the separate stream-3 "quantify" work).
- Removing the V1 on-demand path (it's retained as "sharpen").

---

## Next step
Brainstorm **P1 (Broad news ingestion)** in detail as its own spec, then plan + build
it. The remaining phases follow in order, each with its own cycle.
