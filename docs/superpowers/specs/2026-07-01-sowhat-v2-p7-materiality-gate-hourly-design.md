# So What? V2 · Phase 7 — Materiality Gate + Hourly Cadence (Design)

**Date:** 2026-07-01
**Status:** Approved autonomously (user: "25 news every hour, with an LLM pre-evaluating if a news is actually a deterministic market-moving/business-impact one")
**Parent:** [`2026-06-17-sowhat-v2-architecture.md`](2026-06-17-sowhat-v2-architecture.md)
**Branch:** `feat/sowhat-v2`

---

## Goal

Two coupled changes:
1. **Materiality gate** — before an event is queued for the (expensive) impact trace,
   an LLM judges whether it is a *concrete, deterministic market-moving / business-
   impact* event. Non-events (opinion, analysis, routine, price-move-only, rumor,
   listicles) are dropped. This raises signal and stops the batch spending Claude
   calls on noise.
2. **Hourly cadence** — run the cycle every hour instead of every 12h, so the warm
   map reflects the last hour's real events. The materiality gate is what makes hourly
   affordable: on a quiet hour few items pass, so volume tracks *actual* news flow
   rather than a fixed 25×24.

## Cost note (explicit)

Hourly × up to `INGEST_CAP` traced events × ~6–7 Claude calls each (incl. P6's seed
score) is **many times** the 12h design's ~250–300 calls/day. The gate bounds this to
*material* events (usually far fewer than 25/hour from free feeds), and every volume
knob is configurable (`INGEST_CAP`, `PRECOMPUTE_MAX_EVENTS`, task interval,
`PRECOMPUTE_WALLCLOCK_S`). Users on a tight Max plan can raise the interval or lower
the cap. This trade-off is the user's to own; the design makes it safe and tunable.

## Locked / judgment-call decisions

- **Single batched classification call.** The gate is **one** `_claude_call` over all
  fresh candidates in a cycle (numbered headlines → JSON list of kept indices), not one
  call per item. ~1 call/cycle — cheap. Mirrors the existing `_RSS_EXTRACT_PROMPT`
  batch pattern.
- **Gate placement: after `dedupe`, before `rank`/`cap`.** So the cap selects the top-N
  of the *material* set, and unresolvable/duplicate items are already gone (fewer
  headlines to classify).
- **Applies to ALL sources uniformly.** 8-K / Marketaux / Alpha Vantage / RSS
  candidates all pass the gate. RSS already went through `extract_rss_events` (a looser
  extraction); a second materiality pass is harmless (belt-and-suspenders) and keeps
  one consistent quality bar.
- **Deterministic-materiality criteria (in the prompt):** KEEP concrete events with a
  clear directional business effect — M&A, contracts won/lost, output/production cuts,
  regulatory approval/ban/recall, tariffs/sanctions, earnings/guidance surprises, supply
  disruptions, plant/mine closures, major exec departures, defaults, large capex/JV.
  DROP opinion/analysis/"how/why" explainers, price-move-only ("stock rises 3%"),
  ratings/PT changes, rumor/"could/may/reportedly", routine product launches, and
  celebrity/sports/lifestyle.
- **Fail-open.** If the classification call fails or returns unparseable output, **keep
  all** fresh candidates (log a warning) — coverage is preserved, behavior is no worse
  than today. (Rationale: losing a cycle of real events is worse than tracing a few
  extra; Claude is healthy in batch.)
- **Toggle + default on.** `INGEST_MATERIALITY_GATE` (default `"1"`). Set to `"0"` to
  skip the gate entirely (no call; all fresh candidates proceed) — useful for debugging
  or provider-off runs.
- **Reuse the LLM provider abstraction.** The gate uses `_claude_call` +
  `_parse_llm_json` (same as `extract_rss_events`), so it honors `IMPACT_LLM_PROVIDER`.
- **Hourly is an ops/config change, not code.** The Windows Scheduled Task trigger
  changes from twice-daily to hourly; `PRECOMPUTE_WALLCLOCK_S` is lowered (≈3000s /
  50 min) so a cycle can't bleed into the next hour (the task already uses
  `MultipleInstances = IgnoreNew` as a backstop). No code change needed for cadence.

## `_materiality_filter` (new, `pipeline/ingest_news.py`)

```python
def _materiality_filter(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only candidates the LLM judges to be concrete, deterministic
    market-moving / business-impact events. One batched call. Fail-open: on
    empty/garbled output, return `cands` unchanged. Skipped entirely when
    INGEST_MATERIALITY_GATE='0'."""
```
- Prompt: numbered `headline — seed_entity` lines → `[{"index": n, "material": true/false, "reason": "<short>"}]` (or a bare kept-index array). Keep candidates whose index is marked material.
- Robust index handling (ignore out-of-range / non-int indices), like `extract_rss_events`.
- Returns the kept subset **preserving order**.

## `run_ingest` change

```python
        fresh = dedupe(resolved, conn)
        material = _materiality_filter(fresh)          # NEW gate
        ranked = cap(rank(material, conn))
        ...
        summary = {"fetched": len(cands), "resolved": len(resolved), "fresh": len(fresh),
                   "material": len(material),          # NEW counter
                   "queued": ..., "skipped": ...}
```

## Testing (deterministic — monkeypatched `_claude_call`; no network)

- **Gate keeps only material:** 3 fresh candidates; monkeypatch `_claude_call` → keep
  indices {1,3} → `_materiality_filter` returns those 2 in order; candidate 2 dropped.
- **Fail-open:** `_claude_call` → `""` (or non-JSON) → returns all candidates unchanged.
- **Toggle off:** `INGEST_MATERIALITY_GATE="0"` (monkeypatch env) → no `_claude_call`
  made (assert a sentinel that would raise if called), all candidates returned.
- **Empty input:** `[]` → `[]`, no call.
- **`run_ingest` wiring:** monkeypatch fetchers to yield resolvable candidates and
  `_materiality_filter` to drop one → the dropped one is never `insert_event`'d and the
  summary's `material` < `fresh`. (Reuse the existing ingest test harness/fixtures.)

## Files touched

| File | Change |
|---|---|
| `pipeline/ingest_news.py` | `_MATERIALITY_PROMPT` + `_materiality_filter`; call it in `run_ingest`; `material` in summary; `INGEST_MATERIALITY_GATE` config |
| `tests/test_ingest_news.py` (or `test_ingest_fetchers.py`) | gate keep/fail-open/toggle/empty + run_ingest wiring |
| `.env.example` | `INGEST_MATERIALITY_GATE=1`; note hourly cadence + `PRECOMPUTE_WALLCLOCK_S` for hourly |

Ops (post-build, not code): reconfigure the "EconGraph So What V2 cycle" task to hourly
and set `PRECOMPUTE_WALLCLOCK_S≈3000` as a user env var.

No change to P2–P6 code, the API, or the frontend.

## Out of scope
Per-item (non-batched) classification; a materiality *score* used in ranking (the gate
is binary keep/drop; `rank` still orders by source/centrality/recency); unifying the
Morning-Brief headline source with ingest (separate potential P8).
