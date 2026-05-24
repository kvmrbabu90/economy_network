# Phase 7 runbook — multi-sector extraction (multi-session)

Phase 7 (scale to the full S&P 500) is intentionally batched across multiple
sessions. The pipeline is sector-agnostic; the only thing that spans sessions
is **Part C** (LLM extraction over ~500 filings), because ~1,000 `claude -p`
calls would bump the Max-plan weekly ceiling if done in one burst.

This file is the operating guide.

## Gates A + B (one-time, done)

Run once. Already committed.

```sh
# Gate A1 -- coverage of all 127 sub-industries (config/gics_subindustry_to_industry.yaml)
# Gate A2 -- regulator coverage check (config/regulators.yaml)
# Gate B  -- registry-first ingestion
EDGAR_USER_AGENT="Your Name your@email.com" python -m pipeline.ingest
# -> data/companies.jsonl: 500 unique CIKs, all 10-Ks cached.
```

Verify state any time:

```sh
python -c "
import json
nodes = [json.loads(l) for l in open('data/companies.jsonl', encoding='utf-8')]
print(f'Companies: {len(nodes)}')  # expect 500
"
```

## Part C — extraction batches

Process one sector group per session. The Phase 2 checkpoint
(`data/extract_checkpoint.json`) carries progress; re-running with the
same sector skips work that's already done.

Recommended order — picked to surface cross-sector edges early in resolution:

| # | Sector | Companies | Notes |
|---|---|---|---|
| 1 | Materials | 26 | chemical suppliers -> Consumer Staples names |
| 2 | Energy | 21 | oil/gas suppliers -> Industrials, Utilities |
| 3 | Communication Services | 20 | media -> consumer brands |
| 4 | Real Estate | 31 | mostly REIT corporate filings, small candidate set |
| 5 | Utilities | 31 | regional, narrow customer lists |
| 6 | Consumer Discretionary | 48 | retailers -> consumer goods supply chains |
| 7 | Health Care | 59 | pharma/biotech supply chains |
| 8 | Information Technology | 73 | semis -> everything; software -> SaaS clients |
| 9 | Industrials | 79 | the long tail; airlines, defense, logistics |
| 10 | Financials | 76 | banks tend to be lighter on supplies edges |

(Consumer Staples is already done from Phase 2 -- 36 filings.)

Each batch is a single command:

```sh
python -m pipeline.extract --sector "<sector name>" --run-llm
```

The orchestrator:
- skips any (cik, section) pairs already in `data/extract_checkpoint.json`
- writes new verified candidates to `data/_extract/llm_candidates.jsonl` as
  they're produced (crash-safe; lose nothing on interrupt)
- reassembles `data/edges_raw.jsonl` = rule candidates + every prior LLM
  candidate at the end of each run

Pace: at ~10s per `claude -p` call, expect:
- 20-30 company sectors: ~10-15 min
- 50+ company sectors: ~25-40 min

## Part D — resolution at scale

Run AFTER every Part C batch is complete. Requires the full registry.

```sh
python -m pipeline.resolve
```

Acceptance:
- single-node invariant: **PASS** (printed)
- slug count smaller (relatively) than the 123 slugs from Consumer Staples
  alone -- former slugs like `slug:pepsi`, `slug:walmart`, etc. now resolve
  to real `cik:` nodes because they're in the registry
- review_queue.jsonl has any genuinely ambiguous matches (small, optional)

## Part E — build + API + frontend flip

```sh
rm -f econgraph.db
python -m pipeline.build_graph
# -> econgraph.db, data/graph.json, full stats incl. cross-sector edge count
```

Then restart the API:

```sh
# Whatever port you're running on; default committed value is 8101
uvicorn api.main:app --port 8101
```

Frontend `OPEN_MODE` flip (one-line change):

```sh
# web/src/config.ts -- change OPEN_MODE from "full" to "search"
```

Verify:
- `/health` shows the new counts; `customer_of_rows_in_db` is still 0
- frontend opens with the search box focused; navigation by click-expand /
  double-click-recenter still works
- cross-sector value chain visible: search "Walmart" or "P&G" or pick any
  Materials supplier; click through to see edges crossing sector boundaries

## Useful spot-checks

```sh
# Edges-by-type breakdown across all extractor output to date
python -c "
import json
from collections import Counter
ce = [json.loads(l) for l in open('data/edges_raw.jsonl', encoding='utf-8')]
print(f'Total candidate edges: {len(ce)}')
print(Counter(e['type'] for e in ce))
"

# Checkpoint progress (how many (cik, section) pairs done)
python -c "
import json
ck = json.load(open('data/extract_checkpoint.json', encoding='utf-8'))
print(f'Checkpointed pairs: {len(ck[\"done\"])}')
"
```
