# Cross-time Story-signature Dedup — Design

**Date:** 2026-07-04
**Status:** Approved (user asked to build autonomously)
**Branch:** `main`

## Problem

Today's dedup catches a re-ingested story only two ways, neither of which handles
"a *different source* covered the same story N days ago":

1. `event_exists()` — cross-time, but keyed on `ev:sha1(url)`, so a different
   outlet (different URL) never matches.
2. `_collapse_key` = `(seed, published-date, first-6-words)` — collapses cross-feed
   copies, but only *within one cycle* (an in-memory set) and only for the *same
   published date*.

So a story ingested today whose different-source copy entered 6 days ago is
double-counted (both events feed the 7-day impact window).

## Fix

Persist a **date-independent story signature** per event and check it across
cycles within a bounded window — an `event_exists`-style layer, but on the story,
not the URL.

- **Signature** (`store.story_signature(seed_node_id, headline)`):
  `sig:` + sha1(`<seed_node_id>|<first-6 normalized headline words>`)[:16]. `None`
  when there's no seed, no headline, or **fewer than 4 words** (short/generic
  headlines like "Apple beats earnings" are too broad to collapse across time —
  they fall back to URL-exact dedup only). Deterministic; same normalization is
  used at insert, lookup, and backfill so they always agree.
- **Persist**: new `events.story_sig TEXT` column + index. Fresh DBs get it via
  the CREATE TABLE DDL; existing DBs get an idempotent `ALTER TABLE` migration in
  `init_db` that also **backfills** signatures for existing rows (so a story
  already 6 days old is matchable immediately).
- **Cross-time lookup** (`store.story_sig_seen(conn, sig, within_days)`): is there
  an event with this signature whose `COALESCE(published_at, ingested_at)` is
  within the last `within_days` days? Windowed (default `DEDUP_STORY_DAYS=7`,
  matching the impact window) so a story that has fully *faded* (>7d, ~0 weight)
  can legitimately re-enter, while a still-active recurrence is deduped.
- **Wire into `dedupe()`**: after the URL-exact `event_exists` check and before the
  in-cycle collapse key, drop a candidate whose signature matches (a) an already-
  seen candidate this cycle, or (b) a stored event within the window. Honors
  `_no_collapse` (GKG title-less rows never story-dedup — their synthetic
  URL-slug headlines are not real titles). The surviving candidate's `story_sig`
  is stashed on the dict so `insert_event` persists it.

**Which copy wins:** the *original* (older) event is kept and the re-report is
dropped. This is correct — impact decays from when the **event** happened, and a
re-report doesn't reset that clock. No double-count; no clock reset.

## Config
`INGEST_STORY_DEDUP` (default on; `0` disables), `DEDUP_STORY_DAYS` (7).

## Non-goals (v1)
Reworded-headline matching (needs simhash/minhash or GDELT's Global Similarity
Graph — noted for later; v1 is exact-first-6-words). Merging an already-traced old
event's impacts into a fresh one.

## Testing
- `story_signature`: determinism, None for short/seedless/headline-less, same sig
  for same inputs, differs for distinct stories.
- migration: adds column+index idempotently on an old-shape DB and backfills.
- `story_sig_seen`: matches within window, misses outside it, misses unknown sig.
- `insert_event` persists story_sig.
- `dedupe()`: THE cross-time case (candidate dropped vs a 6-day-old stored event);
  NOT dropped vs a 10-day-old one; distinct-headline same-seed both survive;
  `_no_collapse` candidates never story-dedup; in-cycle story dedup.
- `run_ingest` integration: a fresh candidate matching a stored 6-day-old event by
  signature is not re-queued.
