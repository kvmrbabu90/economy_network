# So What? V2 · Phase 1 — Broad News Ingestion (Design)

**Date:** 2026-06-17
**Status:** Approved (brainstorm), pending implementation plan
**Parent:** [`2026-06-17-sowhat-v2-architecture.md`](2026-06-17-sowhat-v2-architecture.md)
**Branch:** `feat/sowhat-v2`

---

## Goal

Every 12h, pull broad multi-category news, map each distinct market-moving event to
a graph node, dedupe, rank, and record the top ~25 as `status='queued'` in a new
`events` table — ready for P2 to trace. **P1 does no impact tracing.**

This reframes the news step from V1's "curate the best 1–3 for a human brief" to
"extract *every* distinct market-moving event + its seed node, across all categories."

### Locked decisions (from the architecture brainstorm)
- **Sources:** SEC 8-K + Marketaux + Alpha Vantage (free tiers) + broad category RSS.
- **8-K and API items are pre-mapped** (filer CIK→node, ticker→node) — no LLM.
  **RSS items** get **Claude** event-extraction (entity + category), then graph-gated.
- **Cap:** rank all resolvable events, queue the **top ~25/cycle**; record the rest as
  `skipped`.
- **Only graph-resolvable events are kept** (reuse the news graph-gate resolver).

---

## Module structure

New runnable `pipeline/ingest_news.py` (`python -m pipeline.ingest_news`), composed of
small, independently testable units:

```
ingest_news.main()
├── fetch_8k()          -> list[Candidate]   # reuse pipeline/sec_8k.py; CIK→node, no LLM
├── fetch_marketaux()   -> list[Candidate]   # ticker→node; [] if MARKETAUX_KEY unset
├── fetch_alphavantage()-> list[Candidate]   # ticker→node; [] if ALPHAVANTAGE_KEY unset
├── fetch_rss_broad()   -> list[RawItem]     # broad category feeds, no mapping yet
│     └── extract_rss_events(raw)  -> list[Candidate]   # ONE Claude call: entity+category
├── resolve_and_gate(candidates, conn) -> keep only those whose seed resolves to a node
├── dedupe(candidates, conn)            -> drop ids already in `events` (any prior cycle)
├── rank(candidates, conn)              -> priority score; sort desc
└── persist(candidates, conn)           -> top CAP = 'queued', rest = 'skipped'
```

### Common `Candidate` shape
```python
{
  "id":           str,          # stable: sha1(url) or sha1(source+"|"+title)
  "headline":     str,          # normalized, ≤15 words
  "source":       str,          # "SEC 8-K" | "Marketaux" | "Alpha Vantage" | feed name
  "url":          str,
  "category":     str,          # m&a|agreement|exec|filing|commodity|health|politics|macro|company|other
  "published_at": str | None,   # ISO date
  "seed_name":    str,          # entity as named by the source / extractor
  "seed_node_id": str | None,   # set directly for 8-K (CIK) + API (ticker); None for RSS
}
```

---

## Source fetchers

- **`fetch_8k`** — reuse `pipeline/sec_8k.py` to list recent 8-Ks for graph filers.
  `seed_node_id = "cik:" + zero-padded CIK` (already a node). `category` mapped from the
  8-K item number (`2.01`→`m&a`, `1.01`→`agreement`, `5.02`→`exec`, else `filing`).
  `headline` from the filing title/item description.
- **`fetch_marketaux`** — `GET https://api.marketaux.com/v1/news/all?...&api_token=KEY`,
  filtered to items whose `entities[].symbol` maps to a graph node (via a ticker→node
  index built once from `nodes.tickers`). `seed_node_id` = that node; `category="company"`.
  Returns `[]` and logs if `MARKETAUX_KEY` unset (no hard failure).
- **`fetch_alphavantage`** — `GET https://www.alphavantage.co/query?function=NEWS_SENTIMENT&...&apikey=KEY`;
  `ticker_sentiment[].ticker` → node; `category` from `topics`. Same key-optional behavior.
- **`fetch_rss_broad`** — a broadened feed list (keep the current 7 markets feeds; add
  world/politics/health/commodity general feeds). Returns raw `{title, source, url,
  published_at}`; no mapping. Reuses the existing per-feed sampling + recency machinery.
  - **`extract_rss_events`** — one **Claude** call over the pooled RSS items: for each
    item describing a concrete market-moving event, emit `{index, headline (rewritten
    ≤15w, neutral), entity, category}`; skip opinion/aftermath/noise (reuse V1 filter
    rules, but **no top-N cap** — this is breadth, not curation). Fail-open: if Claude is
    unavailable, RSS contributes 0 this cycle (8-K + APIs still populate); never fabricate.

---

## Resolve → dedupe → rank → cap

- **Resolve & gate:** for candidates with `seed_node_id` already set (8-K/API), keep.
  For RSS, resolve `seed_name` via the all-types resolver (`api.news._entity_resolves`
  logic, returning the node id); drop unresolvable. Reject stale (`published_at` older
  than `INGEST_MAX_AGE_DAYS`, default 3).
- **Dedupe:** compute `id`; drop ids already present in `events` (any status, any prior
  cycle) so a story is never queued or traced twice. De-dupe within the cycle too.
- **Rank:** `priority = source_weight × (0.5 + 0.5·centrality_norm) × recency_weight`
  - `source_weight`: 8-K & APIs = 1.0 (precise mapping), RSS = 0.7.
  - `centrality_norm`: seed node's hub score from `data/hubs.jsonl` (Phase-K betweenness)
    if present, else its out+in edge degree, min-max normalized to 0–1. Bigger hub =
    bigger blast radius = higher priority.
  - `recency_weight = 0.5 ** (age_days / 1.5)`.
- **Cap & persist:** sort desc; top `INGEST_CAP` (default 25) → `status='queued'`; the
  remainder → `status='skipped'`. Insert all into `events` (so skipped items are visible
  and their ids block future re-queue).

### Config (env, all optional with sensible defaults)
`MARKETAUX_KEY`, `ALPHAVANTAGE_KEY` (unset → source skipped); `INGEST_CAP=25`;
`INGEST_MAX_AGE_DAYS=3`; `IMPACT_LLM_PROVIDER` reused for the RSS-extraction call.

---

## Data written

The `events` table (DDL in the architecture spec). P1 creates it if absent and inserts
this cycle's candidates. No `event_impacts` (P2). The graph tables are untouched.

---

## Testing (deterministic — mock fetchers, mock `_claude_call`, temp/in-memory SQLite)

- **8-K** candidate → event row with `seed_node_id = cik:…`, correct `category`, `queued`.
- **API** item: ticker in graph → mapped to node; ticker absent → dropped.
- **RSS**: mocked Claude extraction returns `entity`+`category` → resolves → event;
  unresolvable entity → dropped; stale `published_at` → dropped.
- **Dedupe**: same `id` twice in a cycle → one row; an `id` already in `events` (prior
  cycle) → not re-inserted / not re-queued.
- **Rank + cap**: 30 resolvable candidates → exactly the 25 highest-priority are
  `queued`, the other 5 `skipped`; a high-centrality seed outranks a low-centrality one
  at equal recency/source.
- **Key-optional**: `MARKETAUX_KEY`/`ALPHAVANTAGE_KEY` unset → those fetchers return `[]`
  with no error; the cycle still runs on 8-K + RSS.
- **Claude-unavailable**: `extract_rss_events` with empty `_claude_call` → RSS yields 0,
  cycle still persists 8-K/API events (fail-open, no fabrication).

Tests use a small temp SQLite with a handful of `nodes` (incl. tickers) + `aliases` +
`edges` rows so resolution, ticker-mapping, and degree-centrality are exercised without
the full `econgraph.db`.

---

## Files touched

| File | Change |
|---|---|
| `pipeline/ingest_news.py` | NEW — the 12h ingestion runnable + all units above |
| `schema/store.py` | Add `events` table DDL + a `connect`-time `CREATE TABLE IF NOT EXISTS` |
| `pipeline/sec_8k.py` | Reuse; add a thin `recent_8k_events()` accessor if needed |
| `.env.example` | Document `MARKETAUX_KEY`, `ALPHAVANTAGE_KEY`, `INGEST_CAP` |
| `tests/test_ingest_news.py` | NEW — the deterministic unit tests above |

No changes to the impact engine, the graph, or the V1 endpoints. `events` is additive.

---

## Out of scope (P1)
Impact tracing (P2), aggregation (P3), serving/scheduling (P4), UI (P5). P1's only
LLM use is the single RSS-extraction call; all tracing is P2.
