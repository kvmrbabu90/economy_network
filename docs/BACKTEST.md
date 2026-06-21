# Directional-Expectation Eval ("backtest")

`scripts/backtest.py` measures how often the "So What?" engine's verdict
**direction** matches an expert-labelled expectation on a curated set of news
events (`scripts/backtest_cases.jsonl`).

## What this is — and isn't
- **Is:** a repeatable directional sanity/regression score. Each case labels a few
  named entities with the direction a domain expert expects (commodity direction =
  price direction, matching the engine). The harness runs the event through
  `run_impact` and counts agreement.
- **Is NOT:** a market-price backtest. It does not check whether the predicted
  winners/losers actually moved in the market. That needs a historical price feed
  and event-date→return matching — a future extension (see below).

## Running it
```bash
python -B scripts/backtest.py            # all cases, claude provider
python -B scripts/backtest.py --limit 3  # first 3 cases (each ~1-3 min LLM run)
python -B scripts/backtest.py --runs 3   # 3 runs/case, majority-vote direction (dampens LLM noise)
python -B scripts/backtest.py --provider ollama
```
Writes `data/backtest_report.md` and `data/backtest_report.json`, and prints the
headline accuracy. Requires `econgraph.db` and a working LLM provider.

## Reading the report
Per expected entity, the outcome is one of: `correct`, `opposite`, `no_effect`,
`unscored`, `not_in_trace` (node exists but the BFS didn't reach it), or
`not_in_graph` (the entity name didn't resolve to a node). **Accuracy =
correct / (total − not_in_graph)** — curation gaps are excluded from the
denominator so a missing node doesn't read as a model error. The report lists
`not_in_graph` entities separately so you can fix labels or add nodes.

## Adding cases
Append one JSON object per line to `scripts/backtest_cases.jsonl`:
`{"id": "...", "headline": "...", "expect": [{"entity": "<name as it appears in the graph>", "direction": "positive|negative"}], "note": "why"}`.
Prefer unambiguous events and entities you can confirm exist (run a search first).

## Future extension: real price backtesting
Replace the expert label with a realized return: for each case, record the event
date, pull each entity's N-day forward return from a price feed, and treat
`sign(return)` as the ground-truth direction. The scorer (`classify`/`score_case`)
is already direction-based, so only the label source changes.
