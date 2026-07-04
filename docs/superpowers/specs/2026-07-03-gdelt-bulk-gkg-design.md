# GDELT Bulk GKG Ingestion — Design

**Date:** 2026-07-03
**Status:** Approved (research + adversarial verification + live calibration against the real 5,334-node DB)
**Branch:** `feat/sowhat-v2`

## Goal

Replace the per-node GDELT **DOC 2.0 API** fetcher (rate-limited, top-15 nodes only,
title-only) with a **bulk GKG** fetcher that downloads the 15-minute Global Knowledge
Graph update files, matches GDELT's pre-extracted organizations against **all ~5,300
graph nodes at once**, and feeds a cheap pre-LLM filter cascade so only material,
node-matched, de-duplicated candidates reach the existing pipeline.

## Why (verified)

- **Un-rate-limited**: GKG files are static GCS objects (poll `lastupdate.txt`, download
  the slice). One 15-min file is **3–6 MB zipped / ~600–1,400 records**; parse+match
  measured at **~72 ms/slice** on this box. GDELT itself steers volume users off the API.
- **Full coverage**: bulk matches every node (companies + commodities + regions),
  including non-filers (slug:/wikidata:), not just the top-15 the DOC path reached.
- **Structured**: each record ships organizations, themes, tone, amounts, sharing-image,
  and char offsets — the raw material for relevance/noise/dedup **before** any LLM call.
- **Live calibration** (real DB, two slices): **11–13 %** of English-web records match a
  node; matches are high-precision (Walmart/NVIDIA/Boeing/Lockheed/Goldman/Meta…).

## Corrections the research forced (these shape the design)

1. **Entity-match is the primary relevance filter, NOT themes.** A ≥3-business-theme gate
   still passed 61 % of records (GKG's theme taxonomy is governance/health-heavy). The
   org→node inner-join is what cuts the firehose (1385→~180). Themes/tone/amounts are
   **ranking boosters**, not gates.
2. **Ambiguity is a curated common-word set, not a length rule.** Sony/IBM/Intel/FedEx are
   fine; shell/gap/apple/target/delta/oracle need an **offset-proximity gate** (org offset
   near a business-theme or currency-amount offset) to avoid false-firing on everyday use.
3. **GKG has no title column.** The title lives in `Extras` as an HTML-escaped
   `<PAGE_TITLE>` (since 2019, partial). Parse it out; synthesize a fallback when absent.
4. **The LLM materiality gate stays.** ~50–70 % of cascade survivors are still immaterial;
   the pre-filter's job is volume reduction, not final precision. Therefore we **pre-cap**
   GKG candidates by a materiality-prior score before the shared (single-call) gate, so the
   batch stays small.
5. **Tickers must never be text-matched** (ALL/CAR/IT/KEY collide with words) — tickers are
   a post-match disambiguator only. Bulk matches on **names + aliases**.

## Components

### `pipeline/gkg.py` (new) — client + parser + pure matching helpers
- `latest_gkg_url()` / `gkg_slice_url(ts)` / `previous_slice_ts(ts)` — poll
  `http://data.gdeltproject.org/gdeltv2/lastupdate.txt` (plain **http**; the host serves a
  `*.storage.googleapis.com` cert so an https-upgrading client fails), and construct the
  deterministic 15-min URLs.
- `download_slice(url, cache_dir)` — cache-first (never re-fetch; invariant #6), UA reused
  from `EDGAR_USER_AGENT`.
- `parse_gkg(lines) -> Iterator[GkgRecord]` — tab-delimited 27-col rows → a `GkgRecord`
  with: record_id, published_at, is_translingual, source_collection, domain, url,
  themes(list), v2themes(list[(code,offset)]), orgs(list), v2orgs(list[(name,offset)]),
  tone, polarity, amounts(list[(amt,obj,offset)]), sharing_image, title.
- helpers: `normalize_name`, `is_common_word`, `is_business_theme`, `_parse_offset_list`,
  `_parse_tone`, `_parse_amounts`, `_extract_title`.

### `pipeline/ingest_news.py` — `fetch_gkg_bulk(conn)` (new) + wiring
- `build_gkg_node_index(conn)` — `{normalized_name -> (node_id, name, type, ambiguous)}`
  from node **names + aliases** (never tickers-as-tokens). Drop normalized keys that map to
  >1 node (precision over recall). `ambiguous = single-token name in COMMON_WORDS`.
- `fetch_gkg_bulk(conn)`:
  1. Fetch the last `GKG_SLICES` (default 4 = one hour) slice files, cache-first.
  2. Per record cascade: web-only (`SourceCollectionIdentifier==1`), English (drop `-T`
     record ids), URL not seen in-cycle → **org-match** (exact normalized; ambiguous names
     require the offset-proximity gate) → pick the **highest-centrality** matched node as
     the seed → compute a **materiality-prior score** (business-theme count, currency
     Amount, |tone|/polarity, hard-event bonus, centrality).
  3. In-cycle dedup: exact URL, then exact non-empty `SharingImage`.
  4. **Pre-cap** to top `GKG_PRECAP` (default 40) by materiality-prior, so the downstream
     single-call materiality gate stays small.
  5. Emit standard candidate dicts (`headline`=title|fallback, `source="GDELT-GKG"`,
     `url`=DocumentIdentifier, `category` by node type, `published_at`, `seed_entity`=node
     name, `seed_node_id`=matched node, `id`=`_event_id`).
- Wired into the `run_ingest` tuple; `INGEST_GDELT` default flips to `0` (DOC becomes an
  optional on-demand tool), `INGEST_GKG` default `1`. Candidates then flow through the
  existing dedupe → materiality gate → rank → cap → events, unchanged.

## Config (env)
`INGEST_GKG`(1), `INGEST_GDELT`(now 0), `GKG_SLICES`(4), `GKG_PRECAP`(40),
`GKG_ENGLISH_ONLY`(1), `GKG_PROXIMITY_CHARS`(400), `GKG_CACHE_DIR` (default local, beside
the relocated DB — never OneDrive), `GKG_TIMEOUT_S`(60), `GKG_WALLCLOCK_S`(240).

## Provenance / invariants
Event `url` = the exact article URL (`DocumentIdentifier`); the org surface string + its
char offset are available for grounding at extraction time (invariant #4). One canonical
node per entity preserved (bulk matches *to* existing nodes, never creates them). Files
cached to a local path, layered/restartable stages (#5/#6). English-only aligns with the
US-filer universe.

## Testing
Unit (synthetic GKG rows): 27-col parse + tolerant of short/malformed lines; offset-list /
tone / amounts / PAGE_TITLE parse; translingual + web-only filters; `normalize_name` +
suffix-strip; ambiguous common-word detection; `is_business_theme`; proximity gate
(ambiguous org kept only with a nearby business theme/amount); node-index build (collision
drop, alias inclusion); `fetch_gkg_bulk` (mock download → candidates, URL+image dedup,
pre-cap, category-by-type); run_ingest integration (bulk candidate flows end-to-end).
Integration smoke: run against a real cached slice + the real DB.

## Non-goals (v1)
Cross-slice persisted seen-URL set (rely on cache + `event_exists`); Global Similarity
Graph; GEG canonicalization join; historical backfill; the export/mentions files.
