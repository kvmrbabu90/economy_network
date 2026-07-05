# GEG evaluation → GKG-native salience (decision record)

**Date:** 2026-07-05
**Status:** decided + implemented

## Question

Should we add GDELT's **Global Entity Graph (GEG)** — Google Cloud NLP entity
extraction with per-entity **salience** scores — to improve seed selection and the
grounding capsule's org ranking (both currently driven by hand-rolled heuristics)?

## Investigation (empirical)

Probed the public GEG `gcnlapi` file feed directly:

- `MASTERFILELIST.TXT` lists 1,137,658 files but the **newest is `2026-06-18 22:31Z`**
  — 17 days stale (today 2026-07-05), despite advertising "updates every minute".
- The final files were already collapsing (1–2 records each vs. the usual dozens)
  before publishing stopped.
- Direct GETs of recent minute URLs (`20260705…`, `20260704…`, `20260701…`, even
  `20260618223200`) all return **HTTP 404**; there is no `lastupdate.txt` pointer.

**Conclusion:** the public GEG file feed is not producing fresh data. We ingest only
news ≤ `INGEST_MAX_AGE_DAYS` (3d) old, so a GEG file-ingestion would surface **zero**
usable rows today. The GEG BigQuery table is fresher (hourly) but requires a GCP
project + auth + billing and violates our local, file-based, no-cloud-dep pipeline.

Schema captured for if/when the feed returns: each record is
`{score, url, polarity, magnitude, lang, date, entities:[{name, type, mid,
wikipediaUrl, avgSalience, numMentions}]}`.

## Decision

Do **not** build a GEG ingestion. Instead, deliver the same "which org is the
article actually about" win from data we already parse in GKG — a **GKG-native
salience score**.

## Design (implemented)

`_org_salience_score(k, offsets, title_norm)` in `ingest_news.py` — a monotonic,
unnormalized proxy for GEG's `avgSalience`:

- **+3.0** if the org's tokens form a contiguous run in the (normalized) title.
- **+1.5 · (1 − earliest_offset / GKG_LEDE_CHARS)** for lede proximity (earlier = higher).
- **+0.3 · min(mentions, 5)** for mention frequency.

Only the relative order matters. Wired into `_gkg_candidate`:

- The existing `_org_salience` boolean gate still drops incidental mentions.
- Among the survivors, the **seed is the most-salient org** (centrality as tiebreak),
  replacing the old "highest-centrality matched org" — which picked the biggest name
  mentioned rather than the article's actual subject.
- The remaining matched orgs, **salience-ranked**, become the grounding capsule's
  `involves:` list.

`build_gkg_context` was simplified to pure formatting: it now receives the pre-ranked
other-org names from `_gkg_candidate` (which holds the offsets/title needed to score
salience) instead of re-matching against the node index.

## Validation

- 335 backend tests pass (1 pre-existing unrelated failure). New tests cover the
  score ordering and a multi-org case where salience overrides centrality
  (`test_gkg_candidate_seed_is_most_salient_not_most_central`).
- Live GKG slice: 84 candidates, 75 with sensible salience-ranked capsules.

## Revisit criteria

If the GEG file feed resumes (poll `MASTERFILELIST.TXT` for a timestamp within a few
hours of now), reconsider joining GEG's `avgSalience`/entity `type` on the article
URL to replace the heuristic score. Until then, the GKG-native score stands.
