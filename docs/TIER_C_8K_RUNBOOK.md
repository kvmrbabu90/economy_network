# Tier C — 8-K customer announcement scraper

The 10-K extraction misses real customer/supplier edges because 10-Ks only
disclose >10%-customer-concentration relationships. 8-K Item 1.01 ("Entry
into a Material Definitive Agreement") and Item 8.01 ("Other Events") are
where companies announce contract wins, partnerships, and supply deals
throughout the year.

This is the operating guide for running the scraper.

## One-time setup

Same as Phase 1 -- you need `EDGAR_USER_AGENT` set:

```sh
$env:EDGAR_USER_AGENT = "Your Name your@email.com"   # PowerShell
export EDGAR_USER_AGENT="Your Name your@email.com"   # bash
```

## Run

By sector. Same checkpoint discipline as Phase 7 -- `data/8k_checkpoint.json`
remembers (cik, accession) pairs processed.

```sh
python -m pipeline.sec_8k --sector "Information Technology" --since-days 180
python -m pipeline.sec_8k --sector "Industrials"            --since-days 180
python -m pipeline.sec_8k --sector "Consumer Discretionary" --since-days 180
# ... and so on across all 11 sectors
```

Tunables:
- `--since-days`         lookback window (default 180; 365 if you want a full year)
- `--limit-per-cik`      cap on 8-Ks per company per run (default 8)
- `--limit N`            smoke-test on first N companies
- `--sector X`           restrict to one GICS sector (recommended)

Pace: ~1 SEC HTTP call/company/run + ~1 `claude -p` call/relevant-8-K. The
EDGAR throttle covers SEC; expect 10-20 LLM calls/sector for a 180-day
window. **Much** cheaper than the Phase 2 10-K extraction (which was
~1000 LLM calls).

## What it surfaces

Item 1.01 sections capture material definitive agreements. The LLM is
prompted to:

* Extract supplies/customer_of edges from the section text
* Use verbatim snippets that name the target company
* **Skip M&A / underwriting / credit / debt offering / governance** filings
  (those use the same "entered into ... Agreement with" language but
  aren't value-chain relationships)

Live smoke test result (10 IT companies, 6 8-Ks each window):

```
AMD --supplies--> Meta Platforms, Inc.
  snippet: "governing the purchase of AMD Instinct™ GPU products by Meta"
  source : AMD 8-K Item 1.01 filing 0000002488-26-000045
```

That's a real IT→Comm Services edge the 10-K corpus never had.

## Integration with the rest of the pipeline

The 8-K candidates land in `data/_extract/sec_8k_candidates.jsonl`. The
existing `python -m pipeline.extract` orchestrator picks them up
automatically and folds them into `edges_raw.jsonl` on the next run.
Phase 3 resolution + Phase 4 build then expose them via the API just
like any other LLM-extracted edge -- same provenance, same verify gate,
same single-node invariant.

After running 8-K extraction for a sector:

```sh
# Reassemble edges_raw.jsonl with the new 8-K candidates folded in.
python -m pipeline.extract --sector "Information Technology" --run-llm
# Re-resolve (deterministic; just integrates the new candidates)
python -m pipeline.resolve
# Rebuild SQLite + graph.json
rm econgraph.db
python -m pipeline.build_graph
# Restart the API on the rebuilt DB
uvicorn api.main:app --port 8101 --reload
```

## Why it's incremental, not a full replacement for 10-K extraction

10-K extraction finds the >10%-customer-concentration disclosures (Walmart
as P&G's biggest customer, etc.) -- those are the **stable** annual
disclosures. 8-K scraping finds the **dynamic** announcements -- a new
partnership signed last month, a customer win disclosed last quarter.
You want both; they cover different slices of the relationship landscape.

The 8-K source itself is also more biased toward large deals (the SEC
"material" threshold is what triggers an 8-K), so it skews toward
high-value B2B relationships. That's actually a feature -- the noise
filters itself.
