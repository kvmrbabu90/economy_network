# GDELT Targeted Fetcher — Design

**Date:** 2026-07-03
**Status:** Approved (design presented + user approved "go ahead and build it")
**Branch:** `feat/sowhat-v2`

## Goal

Add GDELT as a news source to the So What? V2 ingestion cycle — but as a
**targeted, per-node query source**, not a blind global firehose. This fills the
coverage gap the current fetchers can't reach (non-CIK / non-tickered entities:
commodities, materials, regions, foreign/private firms) using a free,
key-less, unrestricted API, while reusing the existing noise-control machinery.

## Why per-node, not firehose

GDELT DOC 2.0 is **high-recall / low-precision** with heavy syndication
duplication and an entity extractor that has **no ticker/CIK mapping** and skews
to famous names. A global firehose would maximize those weaknesses and force
lossy fuzzy name-resolution. Instead we **query GDELT for each high-centrality
graph node by its own name**, so the seed node id is *known* — GDELT's entity
noise never touches the seed, and we skip `_resolve_to_node_id` entirely for
this source. This is also the invariant-compliant reading of CLAUDE.md
("curate to material relationships; exhaustive is out of scope").

## Components

### `pipeline/gdelt.py` — DOC 2.0 client (new)
A polite, rate-limited article-search client, structurally analogous to
`pipeline/sec.py`:
- `GDELT_DOC_URL = https://api.gdeltproject.org/api/v2/doc/doc`
- `gdelt_user_agent()` — reuse `GDELT_USER_AGENT` → `EDGAR_USER_AGENT` → static
  default (mirrors how Wikidata reuses the EDGAR UA). GDELT now *requires* a UA.
- `_Throttle` — process-wide min-interval limiter (mirrors
  `pipeline.sec._GlobalRateLimiter`); `GDELT_MIN_INTERVAL_S` default `1.0`
  (GDELT throttles at a fraction of a QPS). `0` disables (tests).
- `gdelt_search(entity, *, timespan, maxrecords, extra_query)` → list of raw
  article dicts `{url, title, domain, seendate, language, sourcecountry,
  published_at}`. Query = `"<name>" sourcelang:english`, `mode=artlist`,
  `format=json`, `sort=datedesc`, `maxrecords` clamped to [1, 250]. Empty name →
  `[]`. Non-JSON / empty 200 body (GDELT's no-result shape) → `[]`. Transport /
  HTTP errors propagate to the caller's per-query guard.
- `seendate_to_date("YYYYMMDDTHHMMSSZ")` → `"YYYY-MM-DD"` (None if unparseable).

### `pipeline/ingest_news.py` — `fetch_gdelt(conn, *, deadline=None)` (new)
- Disabled when `INGEST_GDELT='0'` (default on).
- Rank all named nodes by `_centrality(conn)`; take the top `GDELT_TOP_NODES`
  (default 15).
- **Self-budgeting wall clock:** if `deadline` is None, compute
  `time.monotonic() + GDELT_WALLCLOCK_S` (default 120) *at call time* so a slow
  upstream 8-K crawl can't starve it; stop starting new node-queries past the
  deadline (mirrors `fetch_8k`).
- Per-node query isolation: one failing entity is logged and skipped, never
  aborts the batch.
- Each article → candidate dict with **known** `seed_node_id`/`seed_entity`,
  `source="GDELT"`, `category` by node type
  (`Company→company, Commodity/Material→commodity, Region→macro,
  Regulator→politics, else other`), `published_at` from `seendate`, id via
  `_event_id` (sha1 of url).
- Wired into the `run_ingest` fetcher tuple **after** 8-K/Marketaux/AlphaVantage
  and **before** RSS. Order matters for the first-wins cross-feed collapse:
  higher-provenance sources must win a tie, so `source="GDELT"` (title-only)
  keeps the default `_SOURCE_WEIGHT` of 0.7 and loses collapse ties to the
  authoritative 8-K.

## Downstream (unchanged) does the noise control
Candidates flow through the existing pipeline untouched: seed already resolved →
`dedupe` (url-hash + cross-feed collapse) → `_materiality_filter` (LLM gate) →
`rank` (centrality/recency/source-weight) → `cap` (25/cycle). This is exactly
the "deduplication + noise-reduction filtering you must add in your backend"
that GDELT's own docs prescribe.

## Provenance decision (invariant #4)
DOC 2.0 artlist returns **title + URL**, not article body. v1 uses the **title**
as both headline and grounding basis (`source="GDELT"`, weight 0.7 reflects the
thinner provenance). Fetching article bodies for stronger grounding is a
deliberate future enhancement, not in v1 (avoids scraping/paywall/politeness
complexity).

## Config (env)
`INGEST_GDELT` (default `1`), `GDELT_TOP_NODES` (15), `GDELT_TIMESPAN` (`1h`),
`GDELT_MAXRECORDS` (5), `GDELT_WALLCLOCK_S` (180), `GDELT_MIN_INTERVAL_S` (5.0 —
GDELT's stated 1-request-per-5-seconds limit), `GDELT_BACKOFF_S` (5, on 429),
`GDELT_TIMEOUT_S` (15), `GDELT_USER_AGENT` (falls back to `EDGAR_USER_AGENT`).

**Rate limit (confirmed live):** GDELT's 429 body reads *"limit requests to one
every 5 seconds."* Hence the 5s default min-interval and a single 5s backoff-retry
on 429. 15 nodes × 5s ≈ 75–90s per cycle, within the 180s self-budget.

## Testing
- `gdelt_search`: URL/param/UA construction; artlist JSON → dicts; empty-name →
  []; non-JSON 200 → []; maxrecords clamp; seendate parse.
- `fetch_gdelt`: top-N by centrality; known-seed mapping; category-by-type;
  deadline stop; `INGEST_GDELT=0` → []; per-node failure isolation.
- Update the 3 `run_ingest` end-to-end tests to stub `fetch_gdelt` (new tuple
  member must not hit the network in hermetic tests).
- Live smoke: real `gdelt_search("Apple")` returns articles; `fetch_gdelt`
  against the real DB queues real events.

## Non-goals
Not replacing SEC 8-K (authoritative primary stays). Not article-body fetching.
Not GKG/Events CSV feeds. Not global firehose.
